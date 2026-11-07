"""Focused offline video lifecycle tests: fake transport, local fixture bytes only."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# Reuse the installed local AstrBot mocks, never modify conftest or image tests.
from .test_battle_commands import (
    SESSION_FIELDS,
    Client,
    Event,
    battle_module,
    collect,
    text,
)
from .test_battle_commands import (
    Host as ImageHost,
)
from .test_battle_reply import QuoteEvent

components = importlib.import_module("astrbot.api.message_components")


class FakeVideo:
    def __init__(self, file):
        self.file = file

    @classmethod
    def fromFileSystem(cls, path):  # noqa: N802 - real AstrBot API
        return cls(path)


# Import needs Video in the old stub surface; restore module storage immediately
# so other tests do not inherit this augmentation. Patch only our module later.
with patch.object(components, "Video", FakeVideo, create=True):
    video_module = importlib.import_module("astrbot_plugin_arena_image.video_commands")
VideoBattleController = video_module.VideoBattleController
BattleMessageReceipt = video_module.BattleMessageReceipt
ACK = "已收到，正在生成 A/B 对战视频。"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42fixture-only"
EVAL = "11111111-1111-4111-8111-111111111111"


class VideoClient(Client):
    def __init__(self):
        super().__init__()
        self.response = {
            "evaluation_id": EVAL,
            "context": {
                "evaluationSessionId": EVAL,
                "messageAId": "22222222-2222-4222-8222-222222222222",
                "messageBId": "33333333-3333-4333-8333-333333333333",
                "browser_scope": "offline-browser",
                "mode": "battle",
                "modality": "video",
                "prompt": "PRIVATE-PROMPT",
                "attachments": ["https://private.invalid/input"],
            },
            "sides": {
                side: {
                    "videos": [
                        {
                            "url": f"https://media.invalid/{side}.mp4?signature=PRIVATE",
                            "contentType": "video/mp4",
                        }
                    ],
                    "finished": True,
                    "error": False,
                    "text": "SECRET-PROVIDER",
                }
                for side in ("a", "b")
            },
            "models": {"a": "SECRET-A", "b": "SECRET-B"},
        }


class Host(ImageHost):
    def __init__(self, directory, config=None):
        super().__init__(directory, config)
        self.video_client = VideoClient()
        self.downloads = []
        self.native_results = []
        self.video_prunes = []
        self.bad_videos = set()
        self.receipt = BattleMessageReceipt("123")
        self.delivery_error = None
        self.delivery_started = asyncio.Event()
        self.delivery_release = None
        self.download_started = asyncio.Event()
        self.download_release = None
        self._video_browser_lock = asyncio.Lock()

    def _transport_mode(self):
        return self.config.get("transport_mode", "bridge")

    def _video_battle_client(self):
        return self.video_client

    async def _materialize_video_output(self, candidate, *, output_dir):
        self.downloads.append(copy.deepcopy(candidate))
        self.download_started.set()
        if self.download_release:
            await self.download_release.wait()
        if candidate["url"] in self.bad_videos:
            raise RuntimeError("PRIVATE-PROVIDER https://media.invalid/signed?token=PRIVATE")
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / ("a.mp4" if "/a.mp4" in candidate["url"] else "b.mp4")
        path.write_bytes(MP4)
        return path

    async def _deliver_video_battle_message(self, _event, result):
        self.native_results.append(result)
        self.delivery_started.set()
        if self.delivery_release:
            await self.delivery_release.wait()
        if self.delivery_error:
            raise self.delivery_error
        return self.receipt

    def _prune_video_outputs(self, *, output_dir, pending):
        self.video_prunes.append((output_dir, pending))


def videos(results):
    return [c for r in results for c in r.chain if isinstance(c, FakeVideo)]


class VideoCommandsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            dir=video_module.Path(__file__).resolve().parents[2]
        )
        self.addCleanup(self.temp.cleanup)
        self.host = Host(self.temp.name)
        self.controller = VideoBattleController(self.host)
        self.event = Event()
        self.addCleanup(patch.stopall)
        patch.object(video_module, "Video", FakeVideo).start()

    async def begin(self, event=None):
        results = await collect(self.controller.video_battle(event or self.event, "PRIVATE-PROMPT"))
        code = next(reversed(self.controller._battle_sessions), None)
        return code, results

    def disk(self):
        return json.loads(self.controller._battle_sessions_file.read_text(encoding="utf-8"))

    def votes(self):
        return [c for c in self.host.video_client.calls if c[0] == "vote"]

    def assert_released(self):
        self.assertEqual(self.controller._active_generations, 0)
        self.assertFalse(self.controller._battle_busy)
        self.assertFalse(self.controller._battle_pending_outputs)
        self.assertFalse(self.controller._generation_lock.locked())
        self.assertEqual(self.host.pruned, 0)

    async def test_native_pair_only_after_ack_with_minimal_metadata(self):
        generator = self.controller.video_battle(self.event, "PRIVATE-PROMPT")
        self.assertEqual((await anext(generator)).text, ACK)
        self.assertEqual(self.controller._active_generations, 1)
        self.assertFalse(self.host.video_client.calls)
        self.assertFalse(self.controller._battle_sessions)
        self.assertEqual(await collect(generator), [])
        self.assertEqual(len(self.host.native_results), 1)
        result = self.host.native_results[0]
        self.assertEqual(
            [type(c) for c in result.chain],
            [video_module.Plain, FakeVideo, video_module.Plain, FakeVideo, video_module.Plain],
        )
        self.assertEqual(result.chain[0].text, "\nA\n")
        self.assertEqual(result.chain[2].text, "\nB\n")
        self.assertEqual(result.chain[-1].text, "\n引用本条回复 A / B 投票并揭晓。")
        self.assertEqual(len(videos([result])), 2)
        for component in videos([result]):
            video_module.Path(component.file).relative_to(self.controller.output_dir)
        code, entry = next(iter(self.disk().items()))
        self.assertEqual(set(entry), SESSION_FIELDS | {"delivery_attempted"})
        self.assertIs(entry["delivery_attempted"], True)
        self.assertEqual(entry["vote"], "")
        self.assertEqual(entry["platform_message_id"], "123")
        self.assertEqual(entry["owner"], "alice")
        self.assertEqual(entry["chat"], self.event.unified_msg_origin)
        self.assertEqual(entry["transport_mode"], "bridge")
        self.assertIsNone(entry["names"])
        saved = self.controller._battle_sessions_file.read_text(encoding="utf-8")
        for private in (
            "PRIVATE",
            "SECRET",
            ".mp4",
            "https://",
            "attachments",
            "prompt",
            "outputs",
        ):
            self.assertNotIn(private, saved)
            self.assertNotIn(private, text([result]))
        self.assertNotIn(code, text([result]))
        self.assertFalse(self.votes())
        self.assert_released()

    async def test_separate_all_state_and_forwarded_host_hooks(self):
        for name in (
            "_generation_lock",
            "_battle_sessions",
            "_battle_latest",
            "_battle_locks",
            "_battle_busy",
            "_battle_config_lock",
            "_battle_pending_outputs",
        ):
            self.assertIsNot(getattr(self.controller, name), getattr(self.host, name))
        self.assertIs(self.controller.config, self.host.config)
        self.assertEqual(self.controller.data_dir, self.host.data_dir / "video_battles")
        self.assertNotEqual(self.controller._battle_sessions_file, self.host._battle_sessions_file)
        self.assertIs(self.controller._battle_client(), self.host.video_client)
        self.host._active_generations = 42
        self.assertEqual(self.controller._active_generations, 0)
        async with self.host._generation_lock:
            await asyncio.wait_for(self.begin(), 1)
        self.assertFalse(self.host._battle_sessions)
        self.assertEqual(self.host._active_generations, 42)
        self.assert_released()

    async def test_ack_close_and_task_cancel_release_queue_without_network(self):
        generator = self.controller.video_battle(self.event, "offline")
        self.assertEqual((await anext(generator)).text, ACK)
        await generator.aclose()
        self.assert_released()
        self.assertFalse(self.host.video_client.calls)
        self.assertFalse(self.controller._battle_sessions)
        self.assertFalse(self.controller._battle_sessions_file.exists())
        started = asyncio.Event()

        async def consume():
            generator = self.controller.video_battle(self.event, "offline")
            try:
                await anext(generator)
                started.set()
                await asyncio.Event().wait()
            finally:
                await generator.aclose()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_released()
        self.assertFalse(self.host.video_client.calls)

    async def test_video_queue_default_one_ignores_image_queue_and_caps_independently(self):
        self.host.config["max_queue_depth"] = 50
        first = self.controller.video_battle(self.event, "offline")
        await anext(first)
        results = await collect(self.controller.video_battle(self.event, "second"))
        self.assertIn("视频排队已满（1/1）", text(results))
        self.assertFalse(self.host.video_client.calls)
        self.host.config["video_queue_depth"] = 2
        second = self.controller.video_battle(self.event, "second")
        self.assertEqual((await anext(second)).text, ACK)
        self.assertEqual(self.controller._active_generations, 2)
        await second.aclose()
        await first.aclose()
        self.assert_released()

    async def test_strict_terminal_mp4_and_no_text_or_image_fallback(self):
        original = copy.deepcopy(self.host.video_client.response["sides"]["b"])
        invalid = [
            None,
            {},
            {"text": "some useful text", "finished": True, "error": False},
            {"images": ["image"], "finished": True, "error": False},
            {**original, "finished": False},
            {**original, "finished": 1},
            {**original, "error": True},
            {**original, "error": "upstream failed"},
            {**original, "error": None},
            {**original, "videos": [{"url": "x", "contentType": "text/html"}]},
            {**original, "videos": [{"url": "x"}]},
            {**original, "videos": ["https://media.invalid/b.mp4"]},
        ]
        for index, side in enumerate(invalid):
            with self.subTest(side=side):
                self.host.receipt = BattleMessageReceipt(str(1000 + index))
                self.host.video_client.response["sides"]["b"] = side
                code, results = await self.begin()
                self.assertEqual([r.text for r in results], [ACK])
                self.assertIsNone(self.disk()[code]["vote"])
                self.assertEqual(len(videos(self.host.native_results[-1:])), 1)
                self.assertIn("本场禁止投票", text(self.host.native_results[-1:]))
                self.assertIn(
                    "禁止投票",
                    text(await collect(self.controller.battle_vote(self.event, f"A {code}"))),
                )
        self.assertFalse(self.votes())
        self.assert_released()

    async def test_stable_string_error_and_normalized_bool_error_are_compatible(self):
        for side in self.host.video_client.response["sides"].values():
            side["error"] = ""
            side["videos"][0]["mimeType"] = "video/mp4"
        code, _ = await self.begin()
        self.assertEqual(self.disk()[code]["vote"], "")
        self.assertEqual(len(videos(self.host.native_results)), 2)
        self.assertFalse(self.votes())

    async def test_download_failure_and_wrong_file_bytes_or_scope_never_eligible(self):
        bad_url = self.host.video_client.response["sides"]["b"]["videos"][0]["url"]
        self.host.bad_videos.add(bad_url)
        code, _ = await self.begin()
        self.assertIsNone(self.disk()[code]["vote"])
        self.assertEqual(len(videos(self.host.native_results[-1:])), 1)
        self.host.bad_videos.clear()
        original = self.host._materialize_video_output
        for mode in ("html", "outside", "missing", "extension"):

            async def invalid(candidate, *, output_dir, mode=mode):
                path = await original(candidate, output_dir=output_dir)
                if mode == "html":
                    path.write_bytes(b"<html>not a video</html>")
                if mode == "outside":
                    path = self.host.data_dir / "image.mp4"
                    path.write_bytes(MP4)
                if mode == "missing":
                    path.unlink()
                if mode == "extension":
                    path = path.rename(path.with_suffix(".png"))
                return path

            with (
                self.subTest(mode=mode),
                patch.object(self.host, "_materialize_video_output", invalid),
            ):
                self.host.receipt = BattleMessageReceipt(str(2000 + len(self.host.native_results)))
                code, _ = await self.begin()
                self.assertIsNone(self.disk()[code]["vote"])
                self.assertFalse(videos(self.host.native_results[-1:]))
        self.assertFalse(self.votes())
        self.assert_released()

    async def test_unknown_invalid_rejected_receipts_or_exception_never_resend_or_yield_video(self):
        outcomes = [
            None,
            "",
            True,
            {"message_id": "123"},
            object(),
            SimpleNamespace(uncertain=False),
            BattleMessageReceipt("https://private.invalid"),
            BattleMessageReceipt(0),
            BattleMessageReceipt(True),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                self.host.receipt = outcome
                before = len(self.host.native_results)
                code, results = await self.begin()
                self.assertEqual(len(self.host.native_results), before + 1)
                self.assertFalse(videos(results))
                self.assertIn("不再重发", text(results))
                self.assertIsNone(self.disk()[code]["vote"])
                self.assertEqual(self.disk()[code]["platform_message_id"], "")
        self.host.delivery_error = TimeoutError("PRIVATE signed URL")
        before = len(self.host.native_results)
        code, results = await self.begin()
        self.assertEqual(len(self.host.native_results), before + 1)
        self.assertNotIn("PRIVATE", text(results))
        self.assertIsNone(self.disk()[code]["vote"])
        self.assert_released()

    async def test_missing_native_hook_has_no_image_or_framework_fallback(self):
        with patch.object(self.host, "_deliver_video_battle_message", None):
            code, results = await self.begin()
        self.assertIsNone(self.disk()[code]["vote"])
        self.assertFalse(self.host.native_results)
        self.assertFalse(videos(results))
        self.assertIn("不再重发", text(results))
        self.assertEqual(self.host.pruned, 0)

    async def test_cancel_during_download_or_uncertain_send_preserves_ineligible_reservation(self):
        for phase in ("download", "delivery"):
            with self.subTest(phase=phase):
                host = Host(self.temp.name + "/" + phase)
                controller = VideoBattleController(host)
                if phase == "download":
                    host.download_release = asyncio.Event()
                    started = host.download_started
                else:
                    host.delivery_release = asyncio.Event()
                    started = host.delivery_started
                task = asyncio.create_task(collect(controller.video_battle(self.event, "offline")))
                await asyncio.wait_for(started.wait(), 1)
                entry = next(iter(controller._battle_sessions.values()))
                self.assertIsNone(entry["vote"])
                self.assertEqual(entry["platform_message_id"], "")
                if phase == "delivery":
                    self.assertFalse(controller._generation_lock.locked())
                    self.assertEqual(len(controller._battle_pending_outputs), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                saved = json.loads(controller._battle_sessions_file.read_text())
                self.assertIsNone(next(iter(saved.values()))["vote"])
                self.assertEqual(controller._active_generations, 0)
                self.assertFalse(controller._battle_busy)
                self.assertFalse(controller._battle_pending_outputs)
                self.assertEqual(len(host.native_results), int(phase == "delivery"))

    async def test_no_binding_before_receipt_and_old_exact_quote_not_latest_after_reload(self):
        self.host.delivery_release = asyncio.Event()
        task = asyncio.create_task(self.begin())
        await asyncio.wait_for(self.host.delivery_started.wait(), 1)
        self.assertIn("失败", text(await collect(self.controller.battle_reply(QuoteEvent()))))
        self.assertFalse(self.votes())
        self.host.delivery_release.set()
        old, _ = await asyncio.wait_for(task, 1)
        self.host.receipt = BattleMessageReceipt("456")
        latest, _ = await self.begin()
        restarted = VideoBattleController(self.host)
        result = await collect(restarted.battle_reply(QuoteEvent("a", "123")))
        self.assertIn("已投票 A", text(result))
        self.assertEqual(restarted._battle_sessions[old]["vote"], "a")
        self.assertEqual(restarted._battle_sessions[latest]["vote"], "")
        self.assertEqual(len(self.votes()), 1)
        self.assertFalse([c for c in self.host.client.calls if c[0] == "vote"])
        await collect(restarted.battle_reply(QuoteEvent("A", "123")))
        self.assertEqual(len(self.votes()), 1)

    async def test_owner_chat_unknown_quote_and_explicit_quote_mismatch_fail_closed(self):
        old, _ = await self.begin()
        self.host.receipt = BattleMessageReceipt("456")
        latest, _ = await self.begin()
        for event in (
            QuoteEvent(owner="bob"),
            QuoteEvent(chat="other-chat"),
            QuoteEvent(native_id="999"),
            QuoteEvent(native_id=0),
        ):
            self.assertIn("失败", text(await collect(self.controller.battle_reply(event))))
        self.assertIn(
            "不一致", text(await collect(self.controller.battle_vote(QuoteEvent(), f"A {latest}")))
        )
        for event in (Event(owner="bob"), Event(chat="other-chat")):
            self.assertIn(
                "失败", text(await collect(self.controller.battle_vote(event, f"A {old}")))
            )
        self.assertFalse(self.votes())

    async def test_cross_image_codes_and_native_receipt_collisions_never_fallback(self):
        self.host._battle_sessions["ABCDEF"] = {
            "chat": self.event.unified_msg_origin,
            "platform_message_id": "999",
        }
        choices = iter(("abcdef", "123abc"))

        def token_hex(size):
            return next(choices) if size == 3 else "temporary-file"

        with patch.object(battle_module.secrets, "token_hex", side_effect=token_hex):
            code, _ = await self.begin()
        self.assertEqual(code, "123ABC")
        self.assertIn(
            "图片", text(await collect(self.controller.battle_reply(QuoteEvent(native_id="999"))))
        )
        self.host._battle_sessions[code] = {
            "chat": self.event.unified_msg_origin,
            "platform_message_id": "888",
        }
        self.assertIn(
            "冲突", text(await collect(self.controller.battle_vote(self.event, f"A {code}")))
        )
        self.assertIn("冲突", text(await collect(self.controller.battle_reply(QuoteEvent()))))
        self.host.receipt = BattleMessageReceipt("999")
        code, results = await self.begin()
        self.assertIsNone(self.disk()[code]["vote"])
        self.assertEqual(self.disk()[code]["platform_message_id"], "")
        self.assertIn("不再重发", text(results))
        self.assertFalse(self.votes())
        self.assertFalse([c for c in self.host.client.calls if c[0] == "vote"])

    async def test_late_image_native_collision_blocks_even_explicit_video_vote(self):
        code, _ = await self.begin()
        self.host._battle_sessions["FEDCBA"] = {
            "chat": self.event.unified_msg_origin,
            "platform_message_id": "123",
        }
        result = await collect(self.controller.battle_vote(self.event, f"A {code}"))
        self.assertIn("回执冲突", text(result))
        self.assertFalse(self.votes())

    async def test_duplicate_video_receipt_is_not_bound_to_second_pair(self):
        old, _ = await self.begin()
        new, results = await self.begin()
        self.assertEqual(self.disk()[old]["vote"], "")
        self.assertIsNone(self.disk()[new]["vote"])
        self.assertEqual(self.disk()[new]["platform_message_id"], "")
        self.assertIn("不再重发", text(results))

    async def test_disclosed_random_reveal_switch_unchanged_and_sticky_retry(self):
        code, _ = await self.begin()
        result = await collect(self.controller.battle_reveal(QuoteEvent("不投票")))
        self.assertIn("先投票", text(result))
        self.assertFalse(self.votes())
        await collect(self.controller.battle_reveal_switch(self.event, "开"))
        self.assertTrue(self.host.config["battle_allow_reveal_without_vote"])
        self.host.receipt = BattleMessageReceipt("456")
        random_code, _ = await self.begin()
        self.assertIn("随机投票", text(self.host.native_results[-1:]))
        self.host.video_client.vote_error = TimeoutError("offline bounded")
        with patch.object(battle_module.secrets, "choice", return_value="b"):
            result = await collect(self.controller.battle_reply(QuoteEvent("不投票", "456")))
        self.assertIn("保持不变", text(result))
        self.assertEqual(self.disk()[random_code]["random_choice"], "b")
        self.host.video_client.vote_error = None
        restarted = VideoBattleController(self.host)
        with patch.object(
            battle_module.secrets, "choice", side_effect=AssertionError("must reuse")
        ):
            result = await collect(restarted.battle_reply(QuoteEvent("不投票", "456")))
        self.assertIn("已随机投票 B", text(result))
        self.assertEqual([c[2] for c in self.votes()], ["b", "b"])
        self.assertEqual(restarted._battle_sessions[code]["vote"], "")

    async def test_transport_switch_ttl_and_userscript_rejected(self):
        code, _ = await self.begin()
        self.host.config["transport_mode"] = "direct"
        self.assertIn("通道", text(await collect(self.controller.battle_reply(QuoteEvent()))))
        self.host.config["transport_mode"] = "userscript"
        before = len(self.host.video_client.calls)
        results = await collect(self.controller.video_battle(self.event, "offline"))
        self.assertIn("失败", text(results))
        self.assertEqual(len(self.host.video_client.calls), before)
        self.host.config["transport_mode"] = "bridge"
        self.host.config["battle_session_ttl"] = 1
        self.controller._battle_sessions[code]["timestamp"] -= 2
        self.assertIn("失败", text(await collect(self.controller.battle_reply(QuoteEvent()))))
        self.assertFalse(self.votes())

    async def test_timeout_is_video_specific_single_begin_no_resubmit(self):
        self.host.config.update(request_timeout=9999, video_request_timeout=1)
        real_timeout = asyncio.timeout
        observed = []

        def fast_timeout(seconds):
            observed.append(seconds)
            return real_timeout(0.01)

        async def blocked_begin(**kwargs):
            self.host.video_client.calls.append(("begin", kwargs))
            await asyncio.Event().wait()

        with (
            patch.object(video_module.asyncio, "timeout", fast_timeout),
            patch.object(self.host.video_client, "begin_battle", blocked_begin),
        ):
            results = await collect(self.controller.video_battle(self.event, "offline"))
        self.assertEqual(observed, [1.0])
        self.assertIn("超时", text(results))
        self.assertEqual(len(self.host.video_client.calls), 1)
        self.assertFalse(self.host.native_results)
        self.assert_released()

    async def test_delivery_timeout_single_attempt_and_separate_deadline(self):
        self.host.config.update(
            request_timeout=9999, video_request_timeout=90, video_send_timeout=2
        )
        self.host.delivery_release = asyncio.Event()
        real_timeout = asyncio.timeout
        observed = []

        def fast_timeout(seconds):
            observed.append(seconds)
            return real_timeout(0.01 if seconds == 2 else 1)

        with patch.object(video_module.asyncio, "timeout", fast_timeout):
            code, results = await self.begin()
        self.assertEqual(observed, [90.0, 2.0])
        self.assertIsNone(self.disk()[code]["vote"])
        self.assertEqual(len(self.host.native_results), 1)
        self.assertIn("不再重发", text(results))
        self.assert_released()

    async def test_binding_save_failure_rolls_back_memory_and_disk_eligibility(self):
        original = self.controller._battle_save

        def fail_binding():
            if any(e["platform_message_id"] for e in self.controller._battle_sessions.values()):
                raise OSError("PRIVATE disk")
            original()

        with patch.object(self.controller, "_battle_save", fail_binding):
            code, results = await self.begin()
        self.assertIsNone(self.controller._battle_sessions[code]["vote"])
        self.assertEqual(self.controller._battle_sessions[code]["platform_message_id"], "")
        self.assertIsNone(self.disk()[code]["vote"])
        self.assertEqual(self.disk()[code]["platform_message_id"], "")
        self.assertEqual(len(self.host.native_results), 1)
        self.assertIn("不会自动重发", text(results))
        self.assert_released()

    async def test_bad_response_no_session_or_rebinding_and_unreceipted_restart_not_votable(self):
        original = copy.deepcopy(self.host.video_client.response)
        for response in (
            None,
            {**original, "evaluation_id": "different-session"},
            {**original, "context": {"opaque": "invalid"}},
        ):
            self.host.video_client.response = response
            results = await collect(self.controller.video_battle(self.event, "offline"))
            self.assertIn("失败", text(results))
            self.assertFalse(self.controller._battle_sessions)
        self.host.video_client.response = original
        code, _ = await self.begin()
        data = self.disk()
        data[code]["platform_message_id"] = ""
        self.controller._battle_sessions_file.write_text(json.dumps(data), encoding="utf-8")
        restarted = VideoBattleController(self.host)
        self.assertFalse(restarted._battle_sessions)
        self.assertIn("失败", text(await collect(restarted.battle_vote(self.event, f"A {code}"))))
        self.assertFalse(self.votes())

    async def pending(self):
        async def interrupted(**_kwargs):
            self.host.video_client.calls.append(("begin",))
            await self.host.video_client._video_context_callback(
                copy.deepcopy(self.host.video_client.response["context"])
            )
            self.host.video_client.calls.append(("create",))
            raise TimeoutError("PRIVATE https://private.invalid/?token=PRIVATE")

        with patch.object(self.host.video_client, "begin_battle", interrupted):
            return await self.begin()

    async def test_callback_saves_before_create_retains_pending_failure_and_restores_hook(self):
        observed = []
        previous = object()
        self.host.video_client._video_context_callback = previous

        async def interrupted(**_kwargs):
            callback = self.host.video_client._video_context_callback
            await callback(copy.deepcopy(self.host.video_client.response["context"]))
            saved = self.disk()
            code, entry = next(iter(saved.items()))
            observed.append(code)
            self.assertIsNone(entry["vote"])
            self.assertIs(entry["delivery_attempted"], False)
            self.assertGreater(self.controller._battle_busy[code], 0)
            self.assertEqual(entry["evaluation_id"], EVAL)
            self.host.video_client.calls.append(("create",))
            raise RuntimeError("PRIVATE provider https://private.invalid/?token=PRIVATE")

        with patch.object(self.host.video_client, "begin_battle", interrupted):
            code, results = await self.begin()
        self.assertEqual(observed, [code])
        self.assertIn(code, text(results))
        self.assertIn("只读取原会话", text(results))
        self.assertNotIn("PRIVATE", text(results))
        self.assertNotIn(
            "PRIVATE", self.controller._battle_sessions_file.read_text(encoding="utf-8")
        )
        self.assertIs(self.host.video_client._video_context_callback, previous)
        restarted = VideoBattleController(self.host)
        self.assertEqual(restarted._battle_sessions[code]["context"], self.disk()[code]["context"])
        self.assertIsNone(restarted._battle_sessions[code]["vote"])
        self.assertFalse(restarted._battle_sessions[code]["delivery_attempted"])
        self.assertEqual(self.host.video_client.calls, [("create",)])
        self.assertFalse(self.host.native_results)
        self.assert_released()

    async def test_callback_validation_or_save_failure_prevents_create(self):
        async def create(**_kwargs):
            await self.host.video_client._video_context_callback(
                copy.deepcopy(self.host.video_client.response["context"])
            )
            self.host.video_client.calls.append(("create",))

        with (
            patch.object(self.host.video_client, "begin_battle", create),
            patch.object(self.controller, "_battle_save", side_effect=OSError("PRIVATE disk")),
        ):
            _, results = await self.begin()
        self.assertIn("失败", text(results))
        self.assertFalse(self.host.video_client.calls)
        self.assertFalse(self.controller._battle_sessions_file.exists())
        self.assert_released()

        async def bad_context(**_kwargs):
            await self.host.video_client._video_context_callback({"prompt": "PRIVATE"})
            self.host.video_client.calls.append(("create",))

        with patch.object(self.host.video_client, "begin_battle", bad_context):
            await self.begin()
        self.assertFalse(self.host.video_client.calls)
        self.assert_released()

    async def test_callback_pending_retained_on_timeout_and_cancellation(self):
        for cancel in (False, True):
            host = Host(self.temp.name + ("/cancel" if cancel else "/timeout"))
            controller = VideoBattleController(host)
            started = asyncio.Event()

            async def blocked(host=host, started=started, **_kwargs):
                await host.video_client._video_context_callback(
                    host.video_client.response["context"]
                )
                host.video_client.calls.append(("create",))
                started.set()
                await asyncio.Event().wait()

            host.video_client.begin_battle = blocked
            real_timeout = asyncio.timeout

            def fast_timeout(_seconds, real_timeout=real_timeout):
                return real_timeout(0.02)

            with patch.object(video_module.asyncio, "timeout", fast_timeout):
                task = asyncio.create_task(collect(controller.video_battle(self.event, "offline")))
                await asyncio.wait_for(started.wait(), 1)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    results = await task
                    self.assertIn("超时", text(results))
            restarted = VideoBattleController(host)
            code, entry = next(iter(restarted._battle_sessions.items()))
            self.assertIsNone(entry["vote"])
            self.assertFalse(entry["delivery_attempted"])
            if not cancel:
                self.assertIn(code, text(results))
            self.assertEqual(host.video_client.calls, [("create",)])
            self.assertFalse(controller._battle_busy)
            self.assertEqual(controller._active_generations, 0)
            self.assertIsNone(host.video_client._video_context_callback)

    async def test_resume_reads_saved_exact_context_once_after_restart_not_latest(self):
        old, _ = await self.pending()
        latest, _ = await self.pending()
        self.assertNotEqual(old, latest)
        restarted = VideoBattleController(self.host)
        received = []

        async def read(context):
            received.append(copy.deepcopy(context))
            context["mutated"] = True
            return copy.deepcopy(self.host.video_client.response)

        self.host.video_client.read_video_battle = read
        before = list(self.host.video_client.calls)
        results = await collect(restarted.video_resume(self.event, old.lower()))
        self.assertEqual(received, [self.disk()[old]["context"]])
        self.assertEqual(self.host.video_client.calls, before)
        self.assertEqual(restarted._battle_sessions[old]["vote"], "")
        self.assertIsNone(restarted._battle_sessions[latest]["vote"])
        self.assertEqual(len(self.host.native_results), 1)
        self.assertFalse(videos(results))
        self.assertEqual(restarted._active_generations, 0)
        self.assertFalse(restarted._battle_busy)
        self.assertFalse(self.votes())
        await collect(restarted.video_resume(self.event, old))
        self.assertEqual(len(received), 1)
        self.assertEqual(len(self.host.native_results), 1)

    async def test_resume_explicit_identity_ttl_transport_and_ack_close_no_business_call(self):
        code, _ = await self.pending()
        self.host.video_client.read_video_battle = unittest.mock.AsyncMock()
        for event, requested in (
            (self.event, ""),
            (self.event, "unknown"),
            (Event(owner="bob"), code),
            (Event(chat="other"), code),
            (QuoteEvent(native_id="999"), ""),
        ):
            result = await collect(self.controller.video_resume(event, requested))
            self.assertIn("失败", text(result))
        self.host.config["transport_mode"] = "direct"
        self.assertIn("通道", text(await collect(self.controller.video_resume(self.event, code))))
        self.host.config["transport_mode"] = "bridge"
        generator = self.controller.video_resume(self.event, code)
        self.assertIn(code, (await anext(generator)).text)
        await generator.aclose()
        self.assert_released()
        self.controller._battle_sessions[code]["timestamp"] -= 90000
        self.assertIn("失败", text(await collect(self.controller.video_resume(self.event, code))))
        self.host.video_client.read_video_battle.assert_not_awaited()

    async def test_resume_uncertain_read_or_wrong_session_retains_pending_without_send(self):
        code, _ = await self.pending()
        saved = self.disk()[code]["context"]

        async def failed(context):
            self.assertEqual(context, saved)
            raise RuntimeError("PRIVATE https://private.invalid/?token=PRIVATE")

        self.host.video_client.read_video_battle = failed
        results = await collect(self.controller.video_resume(self.event, code))
        self.assertIn(code, text(results))
        self.assertNotIn("PRIVATE", text(results))

        async def wrong(_context):
            response = copy.deepcopy(self.host.video_client.response)
            response["context"]["messageAId"] = "44444444-4444-4444-8444-444444444444"
            return response

        self.host.video_client.read_video_battle = wrong
        await collect(self.controller.video_resume(self.event, code))
        self.assertEqual(self.disk()[code]["context"], saved)
        self.assertFalse(self.disk()[code]["delivery_attempted"])
        self.assertFalse(self.host.native_results)
        self.assert_released()

    async def test_resume_partial_download_fail_then_unknown_receipt_never_resends_after_restart(
        self,
    ):
        code, _ = await self.pending()

        async def read(_context):
            return copy.deepcopy(self.host.video_client.response)

        self.host.video_client.read_video_battle = read
        self.host.video_client.response["sides"]["b"]["finished"] = False
        await collect(self.controller.video_resume(self.event, code))
        self.assertFalse(self.host.native_results)
        self.assertFalse(self.disk()[code]["delivery_attempted"])
        self.host.video_client.response["sides"]["b"]["finished"] = True
        self.host.bad_videos.add(self.host.video_client.response["sides"]["b"]["videos"][0]["url"])
        await collect(self.controller.video_resume(self.event, code))
        self.assertFalse(self.host.native_results)
        self.host.bad_videos.clear()
        self.host.receipt = None
        await collect(self.controller.video_resume(self.event, code))
        self.assertEqual(len(self.host.native_results), 1)
        self.assertIsNone(self.disk()[code]["vote"])
        self.assertTrue(self.disk()[code]["delivery_attempted"])
        restarted = VideoBattleController(self.host)
        self.host.video_client.read_video_battle = unittest.mock.AsyncMock()
        result = await collect(restarted.video_resume(self.event, code))
        self.assertIn("不能恢复", text(result))
        self.host.video_client.read_video_battle.assert_not_awaited()
        self.assertEqual(len(self.host.native_results), 1)
        self.assertFalse(self.votes())

    async def test_legacy_battle_entry_cannot_run_image_generation(self):
        results = await collect(self.controller.battle(self.event, "offline"))
        self.assertEqual([r.text for r in results], [ACK])
        self.assertEqual(len(self.host.native_results), 1)
        self.assertEqual(len(videos(self.host.native_results)), 2)
        self.assertFalse(self.host.client.calls)

    async def test_video_recovery_commands_never_target_image_latest(self):
        code, _ = await self.begin()
        await collect(self.controller.battle_vote(self.event, f"A {code}"))
        # Exercise confirmed-vote/name-readback-pending, not the already-revealed
        # rejection branch which correctly has no recovery instruction.
        self.controller._battle_sessions[code]["names"] = None
        self.controller._battle_save()
        again = await collect(self.controller.battle_vote(self.event, f"A {code}"))
        self.assertIn("mvbattle揭晓", text(again))
        self.assertNotIn("/battle揭晓 查看", text(again))
        help_result = await collect(self.controller.battle_reveal(self.event, "too many args"))
        self.assertIn("mvbattle揭晓", text(help_result))
        self.assertNotIn("/battle不投票", text(help_result))
