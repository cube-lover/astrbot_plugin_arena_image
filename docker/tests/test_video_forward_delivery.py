"""Offline installed-NapCat forward contract and fail-closed lifecycle tests.

No network/server/QQ calls. Reuse media/component fixtures, not legacy mixed-send
assumptions. The only business send is send_*_forward_msg with two custom nodes.
"""
import asyncio
import base64
import copy
import json
from unittest.mock import AsyncMock, patch
import unittest

from astrbot_plugin_arena_image import video_delivery as delivery
from . import test_video_delivery as fixtures


class ForwardDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.DeliveryTest.setUp(self)
        # Distinct lengths and bytes expose lane/payload confusion.
        self.second = fixtures.MP4[:-4] + b"BBBB-DIFFERENT-SIZE"
        self.paths[1].write_bytes(self.second)
        self.host.config.update(video_send_timeout=1, qq_video_send_timeout=0.2)

    async def send(self, receipt_callback=None):
        return await delivery.deliver_complete_video_battle(
            self.host, self.event, self.result, receipt_callback=receipt_callback)

    def assert_failure(self, failure, native_id="42", reads=2, attempts=1):
        self.assertIsInstance(failure, delivery.VideoDeliveryFailure)
        self.assertEqual(failure.code, "video_verification_uncertain")
        self.assertTrue(failure.uncertain)
        self.assertTrue(failure.confirmed)
        self.assertEqual(failure.message_id, native_id)
        self.assertEqual(failure.attempts, attempts)
        self.assertEqual(len(self.api.calls), attempts)
        self.assertEqual(len(self.api.readbacks), reads)
        self.assertEqual(self.event.bookkeeping, 0)
        self.event.fallback.assert_not_awaited()
        self.assertNotIn(fixtures.SIGNED_URL, failure.message)

    def nodes(self):
        nodes = fixtures.forward_nodes(fixtures.expected_forward(second=self.second))
        for lane, node in zip(("a", "b"), nodes):
            node["message"][1]["data"]["file"] = "native-file-" + lane
        return nodes

    async def test_installed_direct_api_contract_one_card_two_single_video_nodes(self):
        self.api.options["receipt"] = {"message_id": -52, "res_id": "native-forward-resource", "forward_id": "native-forward-resource"}
        original_chain = self.result.chain
        original_state = copy.deepcopy([vars(item) for item in original_chain])
        self.api.forward_options["result"] = {"messages": self.nodes()}
        receipt = await self.send()
        self.assertEqual(type(receipt).__name__, "BattleMessageReceipt")
        self.assertEqual(receipt.message_id, "-52")
        action, params, scoped_timeout = self.api.calls[0]
        self.assertEqual(action, "send_group_forward_msg")
        self.assertEqual(params, {
            "group_id": 123, "self_id": "456", "timeout": 200,
            "message": fixtures.expected_forward(second=self.second),
            "messages": fixtures.expected_forward(second=self.second),
        })
        self.assertIsNot(params["message"], params["messages"])
        self.assertIsNot(params["message"][0]["data"]["content"], params["messages"][0]["data"]["content"])
        self.assertEqual(scoped_timeout, 15.2)
        self.assertEqual(self.api._timeout_sec, 0.01)
        self.assertEqual(self.api.readbacks, [
            ("get_msg", {"message_id": -52, "self_id": "456"}, 15.2),
            ("get_forward_msg", {"message_id": "native-forward-resource", "self_id": "456"}, 15.2),
        ])
        self.assertIs(self.result.chain, original_chain)
        self.assertEqual([vars(item) for item in original_chain], original_state)
        self.assertEqual(len(self.event.parsed_chains), 1)
        self.assertEqual(self.event.bookkeeping, 1)
        for node, expected in zip(params["messages"], (fixtures.MP4, self.second)):
            self.assertEqual(set(node), {"type", "data"})
            self.assertEqual(set(node["data"]), {"user_id", "nickname", "content"})
            content = node["data"]["content"]
            self.assertEqual([segment["type"] for segment in content], ["text", "video"])
            self.assertEqual(base64.b64decode(content[1]["data"]["file"][9:]), expected)
        self.assertNotIn("file:", str(params))
        self.assertNotIn("https:", str(params))
        self.assertNotIn("secret", str(params))
        self.event.fallback.assert_not_awaited()

    def json_outer(self, resid="fixture-resid", native_id=42):
        # Synthetic fields mirror the captured Tencent multiMsg card only.
        return {"message_id": native_id, "message": [{"type": "json", "data": {"data": json.dumps({
            "app": "com.tencent.multimsg", "meta": {"detail": {
                "news": [{"text": "视频 Battle A"}, {"text": "视频 Battle B"}],
                "resid": resid, "source": "视频 Battle", "summary": "2条转发消息",
                "uniseq": "fixture-uniseq",
            }},
        }, ensure_ascii=False)}}]}

    async def test_native_json_card_uses_receipt_resid_not_outer_numeric_id(self):
        # Match the captured declared sizes with synthetic local bytes only.
        self.paths[0].write_bytes(fixtures.MP4 + b"A" * (9324824 - len(fixtures.MP4)))
        self.second = fixtures.MP4 + b"B" * (7292073 - len(fixtures.MP4))
        self.paths[1].write_bytes(self.second)
        self.api.options["receipt"] = {"message_id": 42, "res_id": "fixture-resid", "forward_id": "fixture-resid"}
        self.api.readback_options["result"] = self.json_outer()
        nodes = fixtures.forward_nodes(fixtures.expected_forward(first=self.paths[0].read_bytes(), second=self.second))
        self.assertEqual(nodes[0]["message"][1]["data"]["file_size"], "9324824")
        self.assertEqual(nodes[1]["message"][1]["data"]["file_size"], "7292073")
        self.api.forward_options["result"] = {"messages": nodes}
        callback = AsyncMock()
        receipt = await self.send(callback)
        self.assertEqual(receipt.message_id, "42")
        callback.assert_awaited_once_with("42")
        self.assertEqual(self.api.readbacks, [
            ("get_msg", {"message_id": 42, "self_id": "456"}, 15.2),
            ("get_forward_msg", {"message_id": "fixture-resid", "self_id": "456"}, 15.2),
        ])
        self.event.fallback.assert_not_awaited()

    async def test_native_json_each_resource_alias_and_partial_nested_readback(self):
        for key in ("res_id", "forward_id"):
            self.api.options["receipt"] = {"message_id": 42, key: "fixture-resid"}
            self.api.readback_options["result"] = self.json_outer()
            self.assertEqual((await self.send()).message_id, "42")
            self.assertEqual(self.api.readbacks[-1][1]["message_id"], "fixture-resid")
        self.event.bookkeeping = 0
        partial = self.nodes()[:1]
        nested = self.nodes()
        nested[1]["message"].append({"type": "node", "data": {"content": self.nodes()}})
        for nodes in (partial, nested, list(reversed(self.nodes()))):
            self.api.calls.clear()
            self.api.readbacks.clear()
            self.api.forward_options["result"] = {"messages": nodes}
            callback = AsyncMock()
            self.assert_failure(await self.send(callback))
            callback.assert_awaited_once_with("42")
            self.assertEqual(self.api.readbacks[-1][1]["message_id"], "fixture-resid")

    async def test_json_resources_missing_invalid_conflicting_or_mismatched_fail_closed(self):
        for receipt in ({"message_id": 42},
                        {"message_id": 42, "res_id": "fixture-resid", "forward_id": "different"},
                        {"message_id": 42, "res_id": "different"},
                        {"message_id": 42, "forward_id": "different"},
                        *({"message_id": 42, "res_id": value} for value in (None, "", True, 42, " x ", "https://fixture.invalid/res"))):
            self.api.calls.clear()
            self.api.readbacks.clear()
            self.api.options["receipt"] = receipt
            self.api.readback_options["result"] = self.json_outer()
            callback = AsyncMock()
            self.assert_failure(await self.send(callback), reads=1)
            callback.assert_awaited_once_with("42")

    async def test_json_exact_app_and_meta_detail_path_no_recursive_guessing(self):
        cards = [None, [], {"app": "other", "meta": {"detail": {"resid": "fixture-resid"}}},
                 {"app": "com.tencent.multimsg", "resid": "fixture-resid"},
                 {"app": "com.tencent.multimsg", "meta": {"resid": "fixture-resid"}},
                 {"app": "com.tencent.multimsg", "meta": {"detail": {"nested": {"resid": "fixture-resid"}}}},
                 {"app": "com.tencent.multimsg", "meta": {"detail": {"resid": "different"}}},
                 {"app": "com.tencent.multimsg", "meta": {"detail": {"resid": True}}},
                 {"app": "com.tencent.multimsg", "meta": {"detail": []}}]
        outers = []
        for card in cards:
            outer = self.json_outer()
            outer["message"][0]["data"]["data"] = json.dumps(card)
            outers.append(outer)
        for value in ("bad json", "x" * (64 * 1024 + 1), {"app": "com.tencent.multimsg"}):
            outer = self.json_outer()
            outer["message"][0]["data"]["data"] = value
            outers.append(outer)
        outers.extend([self.json_outer(native_id=99), {"message_id": 42, "message": self.json_outer()["message"] * 2}])
        self.api.options["receipt"] = {"message_id": 42, "res_id": "fixture-resid"}
        for outer in outers:
            self.api.calls.clear()
            self.api.readbacks.clear()
            self.api.readback_options["result"] = outer
            self.assert_failure(await self.send(), reads=1)

    async def test_classic_forward_id_must_match_any_receipt_resource(self):
        self.api.options["receipt"] = {"message_id": 42, "res_id": "different"}
        self.assert_failure(await self.send(), reads=1)


    async def test_callback_is_awaited_once_before_both_reads_and_bookkeeping(self):
        milestones = []

        async def save(native_id):
            self.assertEqual(native_id, "42")
            self.assertEqual(len(self.api.calls), 1)
            self.assertEqual(self.api.readbacks, [])
            self.assertEqual(self.event.bookkeeping, 0)
            await asyncio.sleep(0)
            milestones.append(native_id)

        original = fixtures.LeafApi.call_action

        async def api_call(api, action, **params):
            if action in ("get_msg", "get_forward_msg"):
                self.assertEqual(milestones, ["42"])
            return await original(api, action, **params)

        with patch.object(fixtures.LeafApi, "call_action", api_call):
            receipt = await self.send(save)
        self.assertEqual(receipt.message_id, "42")
        self.assertEqual(milestones, ["42"])

    async def test_host_hook_and_explicit_callback_precedence(self):
        self.host._video_native_receipt = AsyncMock()
        explicit = AsyncMock()
        await self.send(explicit)
        explicit.assert_awaited_once_with("42")
        self.host._video_native_receipt.assert_not_awaited()
        await self.send()
        self.host._video_native_receipt.assert_awaited_once_with("42")

    async def test_callback_persists_id_before_readback_error(self):
        for action in ("get_msg", "get_forward_msg"):
            with self.subTest(action=action):
                self.api.calls.clear()
                self.api.readbacks.clear()
                self.api.readback_options.update(error=None)
                self.api.forward_options.update(error=None)
                options = self.api.readback_options if action == "get_msg" else self.api.forward_options
                options["error"] = RuntimeError(fixtures.SIGNED_URL)
                callback = AsyncMock()
                failure = await self.send(callback)
                callback.assert_awaited_once_with("42")
                self.assert_failure(failure, reads=1 if action == "get_msg" else 2)

    async def test_callback_error_timeout_or_cancellation_never_resends(self):
        for mode in ("error", "timeout", "cancel"):
            with self.subTest(mode=mode):
                self.api.calls.clear()
                self.api.readbacks.clear()
                saved = []
                self.host.config.update(video_send_timeout=0.05, qq_video_send_timeout=0.05)

                async def callback(native_id):
                    saved.append(native_id)
                    if mode == "cancel":
                        raise asyncio.CancelledError()
                    if mode == "timeout":
                        await asyncio.sleep(10)
                    raise RuntimeError(fixtures.SIGNED_URL)

                inline = delivery._inline_videos(self.paths)
                with patch.object(delivery.asyncio, "to_thread", AsyncMock(return_value=inline)):
                    if mode == "cancel":
                        with self.assertRaises(asyncio.CancelledError):
                            await self.send(callback)
                    else:
                        failure = await asyncio.wait_for(self.send(callback), 0.5)
                        self.assert_failure(failure, reads=0)
                self.assertEqual(saved, ["42"])
                self.assertEqual(len(self.api.calls), 1)
                self.assertEqual(self.api.readbacks, [])
                self.event.fallback.assert_not_awaited()

    async def test_unknown_receipts_forward_resources_are_not_native_quote_ids(self):
        for receipt in (None, {}, {"forward_id": "42", "res_id": "42"},
                        {"message_id": fixtures.SIGNED_URL}, {"message_id": True},
                        {"data": {"message_id": 42}}, {"message_id": 42, "status": "async"},
                        {"message_id": 42, "retcode": 1200}, {"message_id": 42, "retcode": "0"}):
            with self.subTest(receipt_type=type(receipt).__name__):
                self.api.calls.clear()
                self.api.options["receipt"] = receipt
                callback = AsyncMock()
                failure = await self.send(callback)
                self.assertEqual(failure.code, "video_send_uncertain")
                self.assertEqual(failure.message_id, "")
                self.assertFalse(failure.confirmed)
                self.assertEqual(len(self.api.calls), 1)
                self.assertEqual(self.api.readbacks, [])
                callback.assert_not_awaited()
        self.event.fallback.assert_not_awaited()

    async def test_invalid_outer_does_not_guess_preview_or_call_forward_readback(self):
        forward = {"type": "forward", "data": {"id": "resid", "content": self.nodes()}}
        for outer in (None, {}, {"data": {"message_id": 42, "message": [forward]}},
                      {"message_id": 99, "message": [forward]},
                      {"message_id": True, "message": [forward]},
                      {"message_id": 42, "status": "async", "message": [forward]},
                      {"message_id": 42, "retcode": True, "message": [forward]},
                      *({"message_id": 42, "message": segments} for segments in (
                          [], [forward, forward], self.nodes(), "[CQ:forward,id=resid]",
                          [{"type": "forward", "data": {}}],
                          [{"type": "forward", "data": {"id": " "}}],
                          [{"type": "node", "data": {"content": [forward]}}]))):
            self.api.calls.clear()
            self.api.readbacks.clear()
            self.api.readback_options["result"] = outer
            callback = AsyncMock()
            failure = await self.send(callback)
            self.assert_failure(failure, reads=1)
            callback.assert_awaited_once_with("42")

    async def test_exact_two_nodes_unknown_partial_swapped_nested_and_identity_fail(self):
        complete = self.nodes()
        variants = [None, {}, {"data": {"messages": complete}}, {"messages": "CQ:string"},
                    {"messages": []}, {"messages": complete[:1]}, {"messages": complete * 2},
                    {"messages": list(reversed(complete))},
                    {"status": "failed", "messages": complete}, {"retcode": True, "messages": complete},
                    {"messages": fixtures.expected_forward(second=self.second)}]
        for mutate in (
            lambda nodes: nodes[0]["sender"].update(user_id=789),
            lambda nodes: nodes[0]["sender"].update(user_id=True),
            lambda nodes: nodes[1]["sender"].update(nickname="private-model"),
            lambda nodes: nodes[1].pop("sender"),
            lambda nodes: nodes[0]["message"].pop(),
            lambda nodes: nodes[1]["message"][0]["data"].update(text="\nB\n"),
            lambda nodes: nodes[0]["message"][0]["data"].update(text="\nA\n"),
            lambda nodes: nodes[0]["message"][1]["data"].clear(),
            lambda nodes: nodes[0]["message"].append({"type": "video", "data": {"file": "extra"}}),
            lambda nodes: nodes[1]["message"].append({"type": "text", "data": {"text": "secret"}}),
            lambda nodes: nodes[0]["message"].append({"type": "node", "data": {"content": complete}}),
            lambda nodes: nodes[0]["message"].append({"type": "forward", "data": {"id": "nested"}}),
            lambda nodes: nodes[1]["message"].append({"type": "image", "data": {"file": "extra"}}),
            lambda nodes: nodes[0].update(message=[None]),
            lambda nodes: nodes[0].update(message=nodes[0]["message"] * 9),
        ):
            nodes = copy.deepcopy(complete)
            mutate(nodes)
            variants.append({"messages": nodes})
        for readback in variants:
            self.api.calls.clear()
            self.api.readbacks.clear()
            self.api.forward_options["result"] = readback
            self.assert_failure(await self.send())

    async def test_native_single_video_text_reorder_and_coalescing_is_safe(self):
        nodes = self.nodes()
        nodes[0]["sender"]["user_id"] = "456"
        nodes[0]["message"].reverse()
        nodes[1]["message"] = [
            {"type": "text", "data": {"text": "\nB\n"}},
            {"type": "video", "data": {"file": "native-file-b", "file_size": str(len(self.second))}},
            {"type": "text", "data": {"text": delivery.VIDEO_VOTE_FOOTER}},
        ]
        self.api.forward_options["result"] = {"messages": nodes}
        self.api.readback_options["result"] = {"message_id": 42, "message": [
            {"type": "forward", "data": {"id": "resid", "content": nodes}}]}
        self.assertEqual((await self.send()).message_id, "42")
        self.assertEqual(len(self.api.calls), 1)
        self.event.fallback.assert_not_awaited()

    async def test_frozen_retry_deep_transport_mutation_no_reencode_or_reserialize(self):
        expected = fixtures.expected_forward(second=self.second)
        observed = []
        original = fixtures.LeafApi.call_action

        async def api_call(api, action, **params):
            if action == "send_group_forward_msg":
                observed.append(copy.deepcopy(params["messages"]))
                self.assertEqual(params["message"], params["messages"])
                if len(observed) == 1:
                    params["message"][0]["data"]["content"].clear()
                    params["messages"][0]["data"]["content"][1]["data"]["file"] = "file:///unsafe"
                    params["messages"][1]["data"]["content"].clear()
                    self.paths[1].write_bytes(b"changed after prepare")
                    self.host.config["battle_allow_reveal_without_vote"] = True
                    self.event.drop_video = True
            return await original(api, action, **params)

        self.api.outcomes = [fixtures.REJECTION, {"message_id": 43}]
        callback = AsyncMock()
        with patch.object(fixtures.LeafApi, "call_action", api_call), \
                patch.object(delivery, "_inline_videos", wraps=delivery._inline_videos) as encode:
            receipt = await self.send(callback)
        self.assertEqual(receipt.message_id, "43")
        self.assertEqual(observed, [expected, expected])
        self.assertEqual(self.api.calls[1][1]["message"], expected)
        self.assertEqual(self.api.calls[1][1]["messages"], expected)
        self.assertIsNot(self.api.calls[0][1]["messages"], self.api.calls[1][1]["messages"])
        self.assertEqual(len(self.event.parsed_chains), 1)
        encode.assert_called_once()
        callback.assert_awaited_once_with("43")

    async def test_rejection_then_positive_partial_readback_never_third_send(self):
        self.api.outcomes = [fixtures.REJECTION, {"message_id": 43}, {"message_id": 99}]
        self.api.forward_options["result"] = {"messages": self.nodes()[:1]}
        callback = AsyncMock()
        self.assert_failure(await self.send(callback), native_id="43", attempts=2)
        callback.assert_awaited_once_with("43")
        self.assertEqual(self.api.outcomes, [{"message_id": 99}])

    async def test_forward_read_error_timeout_and_cancellation_never_resend(self):
        for error in (RuntimeError(fixtures.SIGNED_URL), TimeoutError(), ConnectionError(), fixtures.REJECTION):
            self.api.calls.clear()
            self.api.readbacks.clear()
            self.api.forward_options["error"] = error
            self.assert_failure(await self.send())
        self.api.calls.clear()
        self.api.readbacks.clear()
        self.api.forward_options.update(error=None, delay=10)
        self.assert_failure(await asyncio.wait_for(self.send(), 0.5))
        self.api.calls.clear()
        self.api.readbacks.clear()
        self.api.forward_options.update(error=asyncio.CancelledError(), delay=0)
        saved = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await self.send(saved)
        saved.assert_awaited_once_with("42")
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(len(self.api.readbacks), 2)

    async def test_native_video_sizes_swapped_wrong_missing_and_duplicate_handles_fail_closed(self):
        variants = []
        swapped = self.nodes()
        a, b = (node["message"][1]["data"] for node in swapped)
        a["file_size"], b["file_size"] = b["file_size"], a["file_size"]
        variants.append(swapped)
        duplicate = self.nodes()
        duplicate[1]["message"][1]["data"]["file"] = duplicate[0]["message"][1]["data"]["file"]
        variants.append(duplicate)
        for value in (None, True, False, 0, -1, 36.0, "36.0", " 36", "bad", "", "1" * 20,
                      str(delivery.MAX_INLINE_VIDEO_BYTES + 1), str(len(self.second))):
            nodes = self.nodes()
            nodes[0]["message"][1]["data"]["file_size"] = value
            variants.append(nodes)
        missing = self.nodes()
        missing[0]["message"][1]["data"].pop("file_size")
        variants.append(missing)
        for nodes in variants:
            self.api.calls.clear()
            self.api.readbacks.clear()
            self.api.forward_options["result"] = {"messages": nodes}
            callback = AsyncMock()
            self.assert_failure(await self.send(callback))
            callback.assert_awaited_once_with("42")

    async def test_equal_size_distinct_source_bytes_cannot_establish_lane_identity(self):
        self.second = fixtures.MP4[:-4] + b"BBBB"
        self.paths[1].write_bytes(self.second)
        self.api.forward_options["result"] = {"messages": self.nodes()}
        callback = AsyncMock()
        self.assert_failure(await self.send(callback))
        callback.assert_awaited_once_with("42")
        facts = delivery._inline_video_facts(delivery._inline_videos(self.paths))
        self.assertEqual(facts[0][0], facts[1][0])
        self.assertNotEqual(facts[0][1], facts[1][1])
        # Private digest/byte counts are never added to the public forward card.
        self.assertEqual(self.api.calls[0][1]["messages"], fixtures.expected_forward(second=self.second))
        for _, digest in facts:
            self.assertNotIn(digest, str(self.api.calls[0][1]))

    async def test_identical_sources_require_distinct_native_handles(self):
        self.second = fixtures.MP4
        self.paths[1].write_bytes(self.second)
        self.api.forward_options["result"] = {"messages": self.nodes()}
        self.assertEqual((await self.send()).message_id, "42")
        self.event.bookkeeping = 0
        self.api.calls.clear()
        self.api.readbacks.clear()
        nodes = self.nodes()
        nodes[1]["message"][1]["data"]["file"] = nodes[0]["message"][1]["data"]["file"]
        self.api.forward_options["result"] = {"messages": nodes}
        self.assert_failure(await self.send())

    async def test_private_facts_validate_bounded_frozen_inline_bytes(self):
        for inline in (["base64://%%%", "base64://QQ=="], ["base64://", "base64://QQ=="],
                       ["https://fixture.invalid/a", "base64://QQ=="], [], None):
            with self.assertRaises((ValueError, TypeError)):
                delivery._inline_video_facts(inline)
        with patch.object(delivery, "MAX_INLINE_VIDEO_BYTES", 1):
            with self.assertRaises(ValueError):
                delivery._inline_video_facts(["base64://QQ==", "base64://Qg=="])


    async def test_failure_id_sanitization_and_missing_bot_identity_fail_closed(self):
        for value in (fixtures.SIGNED_URL, True, {}, "0", "1" * 21):
            failure = delivery._delivery_failure("video_verification_uncertain", 1, True,
                                                 confirmed=True, message_id=value)
            self.assertEqual(failure.message_id, "")
        for value in (None, "", True, 0, "invalid", fixtures.SIGNED_URL):
            self.event.message_obj.raw_message = {"self_id": value}
            failure = await self.send()
            self.assertEqual(failure.code, "video_transport_unavailable")
            self.assertEqual(failure.attempts, 0)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.api.readbacks, [])
        self.event.fallback.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
