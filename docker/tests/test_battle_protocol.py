"""Offline Battle protocol contracts; no Arena, CDP or listening relay server.

Reuse the direct transport's cookie/session fakes, but make response queues strict:
any unexpected request (especially an extra preference POST) fails immediately.
The relay cases exercise the real browser selector and RelayPage with an in-memory
submit stub, so even those cases cannot send a real vote or open a browser.
"""

from __future__ import annotations

import json
import time
import unittest
import uuid
from unittest.mock import AsyncMock, Mock

from astrbot_plugin_arena_image import arena_battle, arena_direct, arena_relay, bridge_client

from .test_arena_direct_transport import (
    DirectTransportCase,
    FakeHTTPX,
    FakePage,
    _chunked_cookies,
    _flight,
    _png_data_url,
    _session_token,
)

EVALUATION_ID = "11111111-1111-7111-8111-111111111111"
MESSAGE_A_ID = "22222222-2222-7222-8222-222222222222"
MESSAGE_B_ID = "33333333-3333-7333-8333-333333333333"
BROWSER_SCOPE = "cdp:offline-battle-browser"
IMAGE_A = "https://images.example.invalid/battle-a.png"
IMAGE_B = "https://images.example.invalid/battle-b.png"
MODEL_A_ID = "model-alpha"
MODEL_B_ID = "model-beta"
NAMES = {"a": "Display Alpha", "b": "Display Beta"}

STREAM = "\n".join(
    [
        'data: a0:"answer A"',
        'data: b0:"answer B"',
        "a2:" + json.dumps([{"type": "image", "image": IMAGE_A}]),
        "b2:" + json.dumps([{"type": "image", "image": IMAGE_B}]),
        'ad:{"finishReason":"stop"}',
        'bd:{"finishReason":"length"}',
    ]
)


def _context(scope: str = BROWSER_SCOPE) -> dict:
    return {
        "evaluationSessionId": EVALUATION_ID,
        "messageAId": MESSAGE_A_ID,
        "messageBId": MESSAGE_B_ID,
        "browser_scope": scope,
    }


def _response(body: str = "", status: int = 200, headers: dict | None = None) -> dict:
    return {"status": status, "text": body, "headers": dict(headers or {})}


def _html_flight(text: str, *, split_at: int | None = None) -> str:
    chunks = [text] if split_at is None else [text[:split_at], text[split_at:]]
    return (
        "<html><script>self.__next_f.push([0])</script>"
        + "".join(
            "<script>self.__next_f.push([1," + json.dumps(chunk) + "])</script>" for chunk in chunks
        )
        + "</html>"
    )


def _models() -> list[dict]:
    # Catalog order and publicName intentionally disagree with the A/B mapping.
    return [
        {"id": MODEL_B_ID, "publicName": "Public Beta", "displayName": " Display Beta "},
        {"id": "decoy", "displayName": "Must not be selected"},
        {"id": MODEL_A_ID, "publicName": "Public Alpha", "displayName": " Display Alpha "},
    ]


def _revealed_data() -> dict:
    return {"modelAId": MODEL_A_ID, "modelBId": MODEL_B_ID, "revealedModels": _models()}


def _feedback(context: dict, value: str = "model_a", **extra) -> dict:
    return {
        **{key: context[key] for key in ("evaluationSessionId", "messageAId", "messageBId")},
        "modelAId": MODEL_A_ID,
        "modelBId": MODEL_B_ID,
        "value": value,
        **extra,
    }


def _session_data(
    context: dict, feedbacks: list[dict] | None = None, *, revealed_models: list[dict] | None = None
) -> dict:
    # Real SSR initialState binds both assistant messages to this Battle session.
    return {
        "id": context["evaluationSessionId"],
        "mode": "battle",
        "messages": [
            {
                "id": context["message" + side.upper() + "Id"],
                "evaluationSessionId": context["evaluationSessionId"],
                "role": "assistant",
                "participantPosition": side,
            }
            for side in ("a", "b")
        ],
        "pairwiseFeedbacks": list(feedbacks or []),
        "revealedModels": _models() if revealed_models is None else revealed_models,
    }


def _session_body(
    context: dict, feedbacks: list[dict] | None = None, *, revealed_models: list[dict] | None = None
) -> str:
    return _html_flight(
        _flight({"initialState": _session_data(context, feedbacks, revealed_models=revealed_models)})
    )


def _success() -> dict:
    return _response(_flight({"success": True, "data": _revealed_data()}))


def _action_id() -> str:
    # A different syntactically valid action on every call; never a production ID.
    return uuid.uuid4().hex + "abcdef01"


def _bundle(action: str, name: str = "createPairwiseFeedback") -> str:
    return (
        f'(0,r.createServerReference)("{action}",r.callServer,void 0,r.findSourceMapURL,"{name}")'
    )


class BattlePage(FakePage):
    """FakePage plus a browser identity, strict replies and a forbidden catalog."""

    def __init__(self, *, scope=BROWSER_SCOPE, responses=(), action_sets=None, **kwargs):
        kwargs.setdefault("cookies", _chunked_cookies(_session_token()))
        if action_sets is None:
            action_sets = [{"createPairwiseFeedback": _action_id()}]
        super().__init__(responses=list(responses), action_sets=action_sets, **kwargs)
        self.cache_key = scope

    async def fetch(self, path, *, method="POST", headers=None, body=None, timeout=None):
        if not self._responses:
            raise AssertionError(f"unexpected offline request: {method} {path}")
        return await super().fetch(path, method=method, headers=headers, body=body, timeout=timeout)

    async def model_table(self):
        self.table_reads += 1
        raise AssertionError("anonymous Battle must not query/select a model catalog")


class BattleParsingTests(unittest.TestCase):
    def test_interleaved_lanes_keep_text_images_and_finish_reasons_separate(self):
        shared = "data:image/png;base64,offline-fixture"
        stream = "\n".join(
            [
                "event: message",
                ' data: a0:" A first " ',
                'data: b0:" B first "',
                'b0:"second B "',
                'a0:"second A "',
                "a2:" + json.dumps([{"type": "image", "image": IMAGE_A}, shared]),
                "data: b2:"
                + json.dumps([{"type": "image_url", "image": {"url": IMAGE_B}}, shared]),
                "a2:" + json.dumps([{"type": "image", "image": IMAGE_A}]),
                'data: bd:{"finishReason":"length"}',
                'data: ad:{"finishReason":"stop"}',
                'c0:"not lane A or B"',
                'ag:"not answer text"',
                'bc:[{"type":"image","image":"not an output"}]',
                "data: [DONE]",
            ]
        )
        result = arena_battle.parse_battle_stream(stream)
        self.assertEqual(
            result["a"],
            {
                "text": "A first second A",
                "images": [IMAGE_A, shared],
                "error": "",
                "finish_reason": "stop",
            },
        )
        self.assertEqual(
            result["b"],
            {
                "text": "B first second B",
                "images": [IMAGE_B, shared],
                "error": "",
                "finish_reason": "length",
            },
        )

    def test_a3_and_b3_errors_do_not_overwrite_the_other_lane(self):
        b_error = {"message": "B 拒绝", "code": "moderation"}
        result = arena_battle.parse_battle_stream(
            STREAM + '\ndata: a3:"A refused"\ndata: b3:' + json.dumps(b_error)
        )
        self.assertEqual(result["a"]["error"], "A refused")
        self.assertEqual(result["b"]["error"], json.dumps(b_error, ensure_ascii=False))
        self.assertEqual(result["a"]["images"], [IMAGE_A])
        self.assertEqual(result["b"]["images"], [IMAGE_B])
        self.assertEqual(result["a"]["finish_reason"], "stop")
        self.assertEqual(result["b"]["finish_reason"], "length")

    def test_one_lane_error_does_not_erase_the_successful_lane(self):
        result = arena_battle.parse_battle_stream(
            'a3:"unavailable"\n' + "b2:" + json.dumps([{"type": "image", "image": IMAGE_B}])
        )
        self.assertEqual(result["a"]["images"], [])
        self.assertEqual(result["a"]["error"], "unavailable")
        self.assertEqual(result["b"]["images"], [IMAGE_B])
        self.assertEqual(result["b"]["error"], "")

    def test_empty_or_unrelated_stream_has_two_empty_lanes(self):
        empty = {"text": "", "images": [], "error": "", "finish_reason": ""}
        for body in ("", None, 'data: c0:"ignore"\nevent: done\n: keepalive'):
            with self.subTest(body=body):
                self.assertEqual(arena_battle.parse_battle_stream(body), {"a": empty, "b": empty})


class FlightAndNamesTests(unittest.TestCase):
    def test_plain_action_flight_decodes_nested_values_and_ignores_non_json_rows(self):
        values = [{"meta": "header"}, ["$", {"success": False}], None, "$undefined"]
        text = "\n".join(f"{index:x}:" + json.dumps(value) for index, value in enumerate(values))
        text += "\nmissing colon\n9:I[not-json]\na:{broken\nb:T5,hello\n"
        self.assertEqual(arena_battle.flight_values(text), values)

    def test_html_flight_concatenates_escaped_chunks_before_parsing(self):
        payload = {"nested": [{"success": True, "data": {"label": '模型 "A"\nB\\C'}}]}
        text = _flight(payload)
        split = text.index("label") + 2  # Split inside a JSON key, not at a row boundary.
        html = _html_flight(text, split_at=split)
        html += '<script>self.__next_f.push([1,"\\q"])</script>\n9:{"outside":"ignore"}'
        self.assertEqual(arena_battle.flight_values(html), [{}, payload])
        self.assertEqual(arena_battle.action_result(html), payload["nested"][0])

    def test_action_result_preserves_success_true_and_false_in_plain_and_html(self):
        for success in (True, False):
            payload = {"success": success, "data": _revealed_data(), "message": "fixture"}
            for body in (
                _flight({"nested": [payload]}),
                _html_flight(_flight({"nested": [payload]})),
            ):
                with self.subTest(success=success, html=body.startswith("<")):
                    result = arena_battle.action_result(body)
                    self.assertEqual(result, payload)
                    self.assertIs(result["success"], success)

    def test_action_result_requires_explicit_boolean_confirmation(self):
        for success in (0, 1, "true", "false", None):
            with self.subTest(success=success), self.assertRaises(bridge_client.BridgeError):
                arena_battle.action_result(_flight({"success": success, "data": _revealed_data()}))
        for body in ("", "<html>OK</html>", _flight({"data": _revealed_data()})):
            with self.subTest(body=body), self.assertRaises(bridge_client.BridgeError):
                arena_battle.action_result(body)

    def test_revealed_names_match_ids_not_catalog_order_and_prefer_display_name(self):
        self.assertEqual(arena_battle.revealed_names(_revealed_data()), NAMES)
        data = _revealed_data()
        data["modelAId"], data["modelBId"] = data["modelBId"], data["modelAId"]
        self.assertEqual(arena_battle.revealed_names(data), {"a": NAMES["b"], "b": NAMES["a"]})

    def test_revealed_names_load_nested_html_models_and_fall_back_to_public_name(self):
        data = {"modelAId": MODEL_A_ID, "modelBId": MODEL_B_ID}
        rows = _models()
        rows[0]["displayName"] = ""
        body = _html_flight(_flight({"nested": [{"revealedModels": rows}]}))
        self.assertEqual(
            arena_battle.revealed_names(data, body), {"a": NAMES["a"], "b": "Public Beta"}
        )

    def test_revealed_names_never_guess_from_default_selection_or_unmatched_ids(self):
        body = _html_flight(
            _flight({"models": _models(), "modelAId": MODEL_A_ID, "modelBId": MODEL_B_ID})
        )
        self.assertEqual(arena_battle.revealed_names({}, body), {})
        data = {"modelAId": "unknown", "modelBId": MODEL_B_ID, "revealedModels": _models()}
        self.assertEqual(arena_battle.revealed_names(data), {"b": NAMES["b"]})
        self.assertEqual(arena_battle.revealed_names({"revealedModels": _models()}), {})

    def test_revealed_names_with_blank_names_return_partial_not_fabricated_names(self):
        data = _revealed_data()
        data["revealedModels"][-1].update(displayName="", publicName="")
        self.assertEqual(arena_battle.revealed_names(data), {"b": NAMES["b"]})

    def test_session_fixture_matches_real_ssr_initial_state_with_bound_assistant_messages(self):
        context = _context()
        payload = arena_battle.flight_values(_session_body(context))[1]
        session = payload["initialState"]
        self.assertEqual(session["id"], EVALUATION_ID)
        self.assertEqual(session["mode"], "battle")
        self.assertEqual(session["pairwiseFeedbacks"], [])
        self.assertEqual(session["revealedModels"], _models())
        self.assertEqual(
            session["messages"],
            [
                {
                    "id": message_id,
                    "evaluationSessionId": EVALUATION_ID,
                    "role": "assistant",
                    "participantPosition": side,
                }
                for message_id, side in ((MESSAGE_A_ID, "a"), (MESSAGE_B_ID, "b"))
            ],
        )
        self.assertEqual(arena_battle.verified_session(_session_body(context), context), session)

    def test_context_normalizes_uuids_drops_extra_fields_and_does_not_mutate_input(self):
        context = _context()
        context["messageAId"] = "ABCDEFAB-CDEF-7ABC-8ABC-ABCDEFABCDEF"
        context["do_not_send"] = "private-context"
        original = dict(context)
        normalized = arena_battle.battle_context(context)
        self.assertEqual(normalized["messageAId"], context["messageAId"].lower())
        self.assertEqual(set(normalized), set(_context()))
        self.assertEqual(context, original)


class BattleBeginTests(DirectTransportCase):
    def test_begin_is_anonymous_no_model_ids_no_model_table_and_returns_bound_context(self):
        page = BattlePage(responses=[_response(STREAM)])
        # A populated cache must not silently turn this into direct-model selection.
        arena_direct.cache_scope(page)["models"] = [{"id": "catalog-id-must-not-leak"}]
        result = self.run_async(self.client(page).begin_battle(prompt="  画一只猫  "))
        self.assertEqual(result["sides"], arena_battle.parse_battle_stream(STREAM))
        self.assertEqual(result["evaluation_id"], result["context"]["evaluationSessionId"])
        self.assertEqual(result["context"]["browser_scope"], BROWSER_SCOPE)
        self.assertEqual(set(result), {"evaluation_id", "sides", "context"})
        self.assertEqual(page.table_reads, 0)
        self.assertEqual(page.action_reads, 0)
        self.assertEqual(page.actions, ["chat_submit"])
        self.assertIn(True, page.cookie_reads)
        self.assertEqual(len(page.requests), 1)
        request = page.requests[0]
        self.assertEqual(request["path"], arena_direct.STREAM_CREATE_EVALUATION_PATH)
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["timeout"], 300.0)
        payload = json.loads(request["body"])
        self.assertEqual(
            set(payload),
            {
                "id",
                "mode",
                "userMessageId",
                "modelAMessageId",
                "modelBMessageId",
                "userMessage",
                "modality",
                "recaptchaV3Token",
            },
        )
        self.assertEqual(payload["mode"], "battle")
        self.assertEqual(payload["modality"], "image")
        self.assertEqual(payload["recaptchaV3Token"], "token-for-chat_submit")
        self.assertEqual(
            payload["userMessage"],
            {"content": "画一只猫", "experimental_attachments": [], "metadata": {}},
        )
        for forbidden in ("modelAId", "modelBId", "model", "catalog-id-must-not-leak"):
            self.assertNotIn(forbidden, payload)
        for forbidden in ("modelAId", "modelBId", "catalog-id-must-not-leak"):
            self.assertNotIn(forbidden, request["body"])
        ids = [
            payload[key] for key in ("id", "userMessageId", "modelAMessageId", "modelBMessageId")
        ]
        self.assertEqual(len(set(ids)), 4)
        self.assertTrue(all(uuid.UUID(value).version == 7 for value in ids))
        self.assertEqual(payload["modelAMessageId"], result["context"]["messageAId"])
        self.assertEqual(payload["modelBMessageId"], result["context"]["messageBId"])
        self.assertEqual(arena_battle.battle_context(result["context"]), result["context"])
        self.assertEqual(arena_direct._HEALTH, {"models": {}, "variants": {}})

    def test_image_only_begin_uploads_on_the_same_page_without_selecting_models(self):
        httpx = FakeHTTPX()
        self._patch(arena_direct, "httpx", httpx)
        page = BattlePage(
            responses=[
                _response(json.dumps({
                    "uploadUrl": "https://upload.example.invalid/put",
                    "key": "uploads/fixture",
                })),
                _response(json.dumps({"url": "https://images.example.invalid/reference.png"})),
                _response(STREAM),
            ],
            action_sets=[{"generateUploadUrl": "OFFLINE_UPLOAD", "getSignedUrl": "OFFLINE_SIGNED"}],
        )
        result = self.run_async(
            self.client(page).begin_battle(prompt=" ", images=[" ", " " + _png_data_url() + " "])
        )
        self.assertEqual(len(httpx.puts), 1)  # FakeHTTPX: no actual signed PUT.
        self.assertEqual(page.table_reads, 0)
        self.assertEqual(page.action_reads, 0)
        self.assertEqual(page.reload_calls, 0)
        self.assertEqual(page.actions, ["chat_submit"])
        self.assertEqual(len(page.requests), 3)
        for request, path in zip(page.requests[:2], (
            "/api/storage/generate-upload-url", "/api/storage/get-signed-url",
        )):
            self.assertEqual(request["path"], path)
            self.assertEqual(request["method"], "POST")
            self.assertEqual(request["headers"], {
                "Content-Type": "application/json", "Accept": "application/json",
            })
        self.assertEqual(json.loads(page.requests[1]["body"]), {"key": "uploads/fixture"})
        payload = json.loads(page.requests[-1]["body"])
        self.assertEqual(payload["mode"], "battle")
        self.assertNotIn("modelAId", payload)
        self.assertNotIn("modelBId", payload)
        self.assertEqual(payload["userMessage"]["content"], "请根据参考图生成图片")
        attachments = payload["userMessage"]["experimental_attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["name"], "uploads/fixture")
        self.assertEqual(attachments[0]["contentType"], "image/png")
        self.assertEqual(attachments[0]["url"], "https://images.example.invalid/reference.png")
        self.assertEqual(result["context"]["browser_scope"], page.cache_key)

    def test_blank_input_is_rejected_before_opening_a_page(self):
        page = BattlePage()
        client = self.client(page)
        opened = Mock(side_effect=AssertionError("must validate input before opening browser"))
        self._patch(client, "_page", opened)
        with self.assertRaises(bridge_client.BridgeError):
            self.run_async(client.begin_battle(prompt=" \n ", images=["", " "]))
        opened.assert_not_called()
        self.assertEqual(page.requests, [])

    def test_http_200_empty_metadata_or_finish_only_stream_is_failure(self):
        for body in (
            "",
            " \n",
            "<html>HTTP 200 is not a generation</html>",
            'ag:"thinking"\nbg:"thinking"',
            'ad:{"finishReason":"stop"}\nbd:{"finishReason":"stop"}',
        ):
            with self.subTest(body=body):
                page = BattlePage(responses=[_response(body)])
                with self.assertRaisesRegex(bridge_client.BridgeError, "空流"):
                    self.run_async(self.client(page).begin_battle(prompt="fixture"))
                self.assertEqual(len(page.requests), 1)
                self.assertEqual(page.table_reads, 0)

    def test_partial_lane_error_is_returned_as_an_error_not_hidden_by_http_200(self):
        body = 'a3:"A failed"\n' + "b2:" + json.dumps([{"type": "image", "image": IMAGE_B}])
        page = BattlePage(responses=[_response(body)])
        result = self.run_async(self.client(page).begin_battle(prompt="fixture"))
        self.assertEqual(result["sides"]["a"]["error"], "A failed")
        self.assertEqual(result["sides"]["a"]["images"], [])
        self.assertEqual(result["sides"]["b"]["images"], [IMAGE_B])

    def test_upstream_failure_is_generic_not_retried_or_recorded_as_a_model_failure(self):
        hidden_error = f'{NAMES["a"]} / {MODEL_A_ID}: provider unavailable'
        for status in (500, 429, 0):
            with self.subTest(status=status):
                page = BattlePage(
                    responses=[
                        _response('a3:' + json.dumps(hidden_error), status, {"Retry-After": "7"})
                    ]
                )
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    self.run_async(self.client(page).begin_battle(prompt="fixture"))
                self.assertEqual(
                    str(caught.exception),
                    f"匿名 Battle 上游请求失败（HTTP {status}），未揭晓模型；请稍后重试",
                )
                self.assertEqual(caught.exception.status_code, status or None)
                self.assertIsNone(caught.exception.payload)
                self.assertIsNone(caught.exception.retry_after)
                self.assertEqual(caught.exception.code, "")
                for hidden in (hidden_error, NAMES["a"], MODEL_A_ID):
                    self.assertNotIn(hidden, repr(caught.exception))
                self.assertEqual(len(page.requests), 1)
                self.assertEqual(page.actions, ["chat_submit"])
                self.assertEqual(page.table_reads, 0)
                self.assertEqual(arena_direct._HEALTH, {"models": {}, "variants": {}})

    def test_empty_recaptcha_token_is_sent_as_empty_without_a_generation_retry(self):
        page = BattlePage(responses=[_response(STREAM)])
        minted = AsyncMock(return_value="")
        self._patch(page, "mint_recaptcha", minted)
        self.run_async(self.client(page).begin_battle(prompt="fixture"))
        minted.assert_awaited_once_with("chat_submit")
        self.assertEqual(json.loads(page.requests[0]["body"])["recaptchaV3Token"], "")
        self.assertEqual(len(page.requests), 1)
        self.assertEqual(page.table_reads, 0)

    def test_ambiguous_generation_timeout_is_not_retried(self):
        page = BattlePage()
        fetched = AsyncMock(side_effect=TimeoutError("offline ambiguous response"))
        self._patch(page, "fetch", fetched)
        with self.assertRaises(TimeoutError):
            self.run_async(self.client(page).begin_battle(prompt="fixture"))
        fetched.assert_awaited_once()
        self.assertEqual(fetched.await_args.args, (arena_direct.STREAM_CREATE_EVALUATION_PATH,))
        self.assertEqual(page.actions, ["chat_submit"])
        self.assertEqual(page.table_reads, 0)

    def test_logged_out_or_anonymous_account_cannot_start_battle(self):
        for cookies in ([], _chunked_cookies(_session_token(anonymous=True))):
            with self.subTest(anonymous=bool(cookies)):
                page = BattlePage(cookies=cookies)
                with self.assertRaises(bridge_client.BridgeError):
                    self.run_async(self.client(page).begin_battle(prompt="fixture"))
                self.assertEqual(page.requests, [])
                self.assertEqual(page.actions, [])
                self.assertEqual(page.table_reads, 0)


class BattleFeedbackTests(DirectTransportCase):
    def assert_session_read_only(self, page, context):
        self.assertEqual(
            [(request["method"], request["path"]) for request in page.requests],
            [("GET", "/c/" + context["evaluationSessionId"])],
        )
        self.assertIsNone(page.requests[0]["body"])
        self.assertEqual(page.requests[0]["headers"], {})
        self.assertEqual(page.action_reads, 0)
        self.assertEqual(page.actions, [])
        self.assertEqual(page.table_reads, 0)
        self.assertEqual(page.reload_calls, 0)

    def assert_feedback_post(self, page, context, value, action):
        self.assertIn(value, ("model_a", "model_b"))
        posts = [request for request in page.requests if request["method"] == "POST"]
        self.assertEqual(len(posts), 1)
        request = posts[0]
        self.assertEqual(request["path"], "/c/" + context["evaluationSessionId"])
        self.assertEqual(
            request["headers"],
            {
                "Accept": "text/x-component",
                "Content-Type": "text/plain;charset=UTF-8",
                "Next-Action": action,
            },
        )
        self.assertEqual(
            json.loads(request["body"]),
            [
                {
                    **{
                        key: context[key]
                        for key in ("evaluationSessionId", "messageAId", "messageBId")
                    },
                    "value": value,
                    "recaptchaV3Token": "token-for-pairwise_feedback",
                }
            ],
        )
        self.assertEqual(request["timeout"], 300.0)
        self.assertNotIn("browser_scope", request["body"])
        self.assertNotIn("modelAId", request["body"])
        self.assertNotIn("modelBId", request["body"])
        self.assertEqual(page.actions, ["pairwise_feedback"])
        self.assertEqual(page.table_reads, 0)

    def test_vote_a_and_b_use_pairwise_feedback_with_exact_session_body(self):
        for winner in ("a", "b"):
            with self.subTest(winner=winner):
                context = _context()
                context["private_extra"] = "must-not-cross-wire"
                original = dict(context)
                action = _action_id()
                page = BattlePage(
                    responses=[_response(_session_body(context)), _success()],
                    action_sets=[{"createPairwiseFeedback": action}],
                )
                names = self.run_async(self.client(page).vote_battle(context, winner=winner))
                self.assertEqual(names, NAMES)
                self.assertEqual(context, original)
                self.assertEqual([request["method"] for request in page.requests], ["GET", "POST"])
                self.assertEqual(page.requests[0]["path"], "/c/" + EVALUATION_ID)
                self.assert_feedback_post(page, context, "model_" + winner, action)
                self.assertNotIn("must-not-cross-wire", page.requests[-1]["body"])
                self.assertEqual(page.action_reads, 1)
                self.assertEqual(page.reload_calls, 0)

    def test_empty_recaptcha_token_uses_null_without_faking_a_preference(self):
        context = _context()
        page = BattlePage(responses=[_response(_session_body(context)), _success()])
        minted = AsyncMock(return_value="")
        self._patch(page, "mint_recaptcha", minted)
        self.assertEqual(self.run_async(self.client(page).vote_battle(context, winner="b")), NAMES)
        minted.assert_awaited_once_with("pairwise_feedback")
        payload = json.loads(page.requests[-1]["body"])
        self.assertIsNone(payload[0]["recaptchaV3Token"])
        self.assertEqual(payload[0]["value"], "model_b")
        self.assertEqual([request["method"] for request in page.requests], ["GET", "POST"])

    def test_ambiguous_vote_timeout_retries_by_reading_the_recorded_feedback(self):
        context = _context()
        page = BattlePage()
        fetched = AsyncMock(
            side_effect=[
                _response(_session_body(context)),
                TimeoutError("offline ambiguous response"),
                _response(_session_body(context, [_feedback(context)])),
            ]
        )
        self._patch(page, "fetch", fetched)
        client = self.client(page)
        with self.assertRaises(TimeoutError):
            self.run_async(client.vote_battle(context, winner="a"))
        self.assertEqual(self.run_async(client.vote_battle(context, winner="a")), NAMES)
        calls = fetched.await_args_list
        self.assertEqual([call.kwargs["method"] for call in calls], ["GET", "POST", "GET"])
        self.assertTrue(all(call.args == ("/c/" + EVALUATION_ID,) for call in calls))
        self.assertEqual(page.actions, ["pairwise_feedback"])
        self.assertEqual(page.action_reads, 1)
        self.assertEqual(page.reload_calls, 0)

    def test_explicit_409_refreshes_origin_once_without_repost_then_retry_reads_feedback(self):
        context = _context()
        original_context = dict(context)
        action = _action_id()
        page = BattlePage(action_sets=[{"createPairwiseFeedback": action}])
        scope = arena_direct.cache_scope(page)
        scope.update(
            actions={"createPairwiseFeedback": action},
            actions_at=time.time(),
            models_at=time.time(),
        )
        fetched = AsyncMock(
            side_effect=[
                _response(_session_body(context)),
                arena_direct._StaleActionError("offline explicit HTTP 409 refresh_required"),
                _response(_session_body(context, [_feedback(context, "model_b")])),
            ]
        )
        reloaded = AsyncMock(wraps=page.reload)
        invalidated = AsyncMock(wraps=arena_direct._invalidate_deployment_caches)
        self._patch(page, "fetch", fetched)
        self._patch(page, "reload", reloaded)
        self._patch(arena_direct, "_invalidate_deployment_caches", invalidated)
        client = self.client(page)
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(client.vote_battle(context, winner="b"))
        self.assertEqual(
            str(caught.exception),
            "官网要求更新页面，已刷新原浏览器；投票未确认，请重试原操作",
        )
        self.assertIsInstance(caught.exception.__cause__, arena_direct._StaleActionError)
        self.assertEqual(
            [call.kwargs["method"] for call in fetched.await_args_list], ["GET", "POST"]
        )
        reloaded.assert_awaited_once_with()
        invalidated.assert_awaited_once_with(page)
        self.assertEqual(page.reload_calls, 1)
        self.assertIs(arena_direct.cache_scope(page), scope)
        self.assertEqual(scope["actions"], {})
        self.assertEqual(scope["actions_at"], 0.0)
        self.assertEqual(scope["models_at"], 0.0)
        self.assertEqual(page.cache_key, original_context["browser_scope"])
        self.assertEqual(context, original_context)

        # A caller retry must re-read the actual feedback on this same browser.
        self.assertEqual(self.run_async(client.vote_battle(context, winner="b")), NAMES)
        calls = fetched.await_args_list
        self.assertEqual([call.kwargs["method"] for call in calls], ["GET", "POST", "GET"])
        self.assertTrue(all(call.args == ("/c/" + EVALUATION_ID,) for call in calls))
        self.assertEqual(calls[1].kwargs["headers"]["Next-Action"], action)
        self.assertEqual(
            json.loads(calls[1].kwargs["body"]),
            [
                {
                    **{key: context[key] for key in ("evaluationSessionId", "messageAId", "messageBId")},
                    "value": "model_b",
                    "recaptchaV3Token": "token-for-pairwise_feedback",
                }
            ],
        )
        reloaded.assert_awaited_once_with()
        invalidated.assert_awaited_once_with(page)
        self.assertEqual(page.actions, ["pairwise_feedback"])
        self.assertEqual(page.action_reads, 1)
        self.assertEqual(page.table_reads, 0)
        self.assertEqual(page.cache_key, original_context["browser_scope"])
        self.assertEqual(context, original_context)

    def test_http_200_success_false_is_not_a_success_even_if_names_are_present(self):
        context = _context()
        action = _action_id()
        failure = _response(_html_flight(_flight({"success": False, "data": _revealed_data()})))
        page = BattlePage(
            responses=[_response(_session_body(context)), failure],
            action_sets=[{"createPairwiseFeedback": action}],
        )
        with self.assertRaisesRegex(bridge_client.BridgeError, "拒绝了投票"):
            self.run_async(self.client(page).vote_battle(context, winner="a"))
        self.assert_feedback_post(page, context, "model_a", action)
        self.assertEqual(len(page.requests), 2)

    def test_http_200_without_boolean_confirmation_cannot_report_vote_success(self):
        context = _context()
        for body in (
            _flight({"data": _revealed_data()}),
            _flight({"success": "true", "data": _revealed_data()}),
            "<html>OK</html>",
            "",
        ):
            with self.subTest(body=body):
                page = BattlePage(responses=[_response(_session_body(context)), _response(body)])
                with self.assertRaises(bridge_client.BridgeError):
                    self.run_async(self.client(page).vote_battle(context, winner="b"))
                self.assertEqual([request["method"] for request in page.requests], ["GET", "POST"])
                self.assertEqual(page.actions, ["pairwise_feedback"])

    def test_unvoted_reveal_is_read_only_even_with_names_and_available_actions(self):
        context = _context()
        for has_scripts in (False, True):
            with self.subTest(has_scripts=has_scripts):
                session = _session_data(context)
                session.update(modelAId=MODEL_A_ID, modelBId=MODEL_B_ID)
                initial = _html_flight(_flight({"initialState": session}))
                if has_scripts:
                    initial += '<script src="/_next/static/chunks/current-session.js"></script>'
                action = _action_id()
                page = BattlePage(
                    responses=[_response(initial)],
                    action_sets=[{"createPairwiseFeedback": action}],
                )
                arena_direct.cache_scope(page).update(
                    actions={"createPairwiseFeedback": action}, actions_at=time.time()
                )
                with self.assertRaisesRegex(bridge_client.BridgeError, "官网尚未记录评价"):
                    self.run_async(self.client(page).reveal_battle(context))
                self.assert_session_read_only(page, context)

    def test_reveal_ignores_unrelated_feedback_without_submitting_any_preference(self):
        context = _context()
        for value in ("model_a", "model_b", "skip"):
            for extra in (
                {"messageAId": str(uuid.uuid4())},
                {"messageBId": str(uuid.uuid4())},
                {"messageAId": MESSAGE_B_ID, "messageBId": MESSAGE_A_ID},
            ):
                with self.subTest(value=value, extra=extra):
                    page = BattlePage(
                        responses=[
                            _response(_session_body(context, [_feedback(context, value, **extra)]))
                        ]
                    )
                    with self.assertRaisesRegex(bridge_client.BridgeError, "官网尚未记录评价"):
                        self.run_async(self.client(page).reveal_battle(context))
                    self.assert_session_read_only(page, context)

    def test_failed_session_read_aborts_before_discovering_actions_or_minting_token(self):
        context = _context()
        for status in (0, 401, 403, 404, 429, 500):
            for reveal in (False, True):
                with self.subTest(status=status, reveal=reveal):
                    page = BattlePage(responses=[_response("unreadable " + NAMES["a"], status)])
                    client = self.client(page)
                    operation = (
                        client.reveal_battle(context)
                        if reveal
                        else client.vote_battle(context, winner="a")
                    )
                    with self.assertRaisesRegex(
                        bridge_client.BridgeError, "无法读取原始 Battle 会话"
                    ) as caught:
                        self.run_async(operation)
                    self.assertNotIn(NAMES["a"], repr(caught.exception))
                    self.assertIsNone(caught.exception.payload)
                    self.assert_session_read_only(page, context)

    def test_http_200_login_empty_flight_or_wrong_session_never_submits_feedback(self):
        context = _context()
        other = {**context, "evaluationSessionId": str(uuid.uuid4())}
        outside_messages = _session_data(context)["messages"]
        bodies = {
            "login": '<html><form action="/login">Sign in</form></html>',
            "empty": "",
            "empty_flight": _html_flight(_flight({})),
            "bootstrap_only": "<html><script>self.__next_f.push([0])</script></html>",
            "wrong_session": _session_body(other),
            # Exact context elsewhere in the tree must not validate another session.
            "unrelated_context": _html_flight(
                _flight({"context": context, "initialState": _session_data(other)})
            ),
            "messages_outside_session": _html_flight(
                _flight(
                    {
                        "messages": outside_messages,
                        "initialState": {**_session_data(context), "messages": []},
                    }
                )
            ),
            "loose_feedbacks": _html_flight(_flight({"pairwiseFeedbacks": []})),
        }
        for label, body in bodies.items():
            for reveal in (False, True):
                with self.subTest(body=label, reveal=reveal):
                    page = BattlePage(responses=[_response(body)])
                    client = self.client(page)
                    operation = (
                        client.reveal_battle(context)
                        if reveal
                        else client.vote_battle(context, winner="a")
                    )
                    with self.assertRaises(bridge_client.BridgeError):
                        self.run_async(operation)
                    self.assert_session_read_only(page, context)

    def test_missing_or_malformed_session_fields_fail_closed_before_any_post(self):
        context = _context()
        mutations = [(field, "missing", None) for field in ("pairwiseFeedbacks", "messages")]
        for field in ("pairwiseFeedbacks", "messages"):
            mutations.extend((field, "invalid", value) for value in (None, {}, "not a list"))
        mutations.extend(
            ("pairwiseFeedbacks", "invalid", value)
            for value in ([None], ["invalid"], [{}], [_feedback(context, value="")])
        )
        mutations.extend(
            ("pairwiseFeedbacks", "invalid", [_feedback(context, **{field: None})])
            for field in ("messageAId", "messageBId")
        )
        mutations.append(("messages", "invalid", []))
        for field, kind, value in mutations:
            for reveal in (False, True):
                with self.subTest(field=field, kind=kind, value=value, reveal=reveal):
                    session = _session_data(context)
                    if kind == "missing":
                        del session[field]
                    else:
                        session[field] = value
                    page = BattlePage(
                        responses=[_response(_html_flight(_flight({"initialState": session})))]
                    )
                    client = self.client(page)
                    operation = (
                        client.reveal_battle(context)
                        if reveal
                        else client.vote_battle(context, winner="b")
                    )
                    with self.assertRaises(bridge_client.BridgeError):
                        self.run_async(operation)
                    self.assert_session_read_only(page, context)

    def test_wrong_or_ambiguous_a_b_messages_never_submit_feedback(self):
        context = _context()
        for index, side in enumerate(("a", "b")):
            mutations = [
                ("missing_message", None, None),
                ("duplicate_message", None, None),
                ("wrong_id", "id", str(uuid.uuid4())),
                ("wrong_session", "evaluationSessionId", str(uuid.uuid4())),
                ("wrong_role", "role", "user"),
                ("wrong_position", "participantPosition", "b" if side == "a" else "a"),
                ("uppercase_position", "participantPosition", side.upper()),
            ] + [
                ("missing_field", field, None)
                for field in ("id", "evaluationSessionId", "role", "participantPosition")
            ]
            for label, field, value in mutations:
                for reveal in (False, True):
                    with self.subTest(side=side, mutation=label, field=field, reveal=reveal):
                        session = _session_data(context)
                        messages = session["messages"]
                        if label == "missing_message":
                            messages.pop(index)
                        elif label == "duplicate_message":
                            messages.append(dict(messages[index]))
                        elif label == "missing_field":
                            del messages[index][field]
                        else:
                            messages[index][field] = value
                        page = BattlePage(
                            responses=[
                                _response(_html_flight(_flight({"initialState": session})))
                            ]
                        )
                        client = self.client(page)
                        operation = (
                            client.reveal_battle(context)
                            if reveal
                            else client.vote_battle(context, winner=side)
                        )
                        with self.assertRaisesRegex(bridge_client.BridgeError, "原始 A/B 消息"):
                            self.run_async(operation)
                        self.assert_session_read_only(page, context)

    def test_conflicting_exact_session_snapshots_never_submit_feedback(self):
        context = _context()
        initial = _session_data(context)
        recorded = _session_data(context, [_feedback(context)])
        body = _html_flight(_flight({"snapshots": [initial, recorded]}))
        page = BattlePage(responses=[_response(body)])
        with self.assertRaisesRegex(bridge_client.BridgeError, "状态不一致"):
            self.run_async(self.client(page).vote_battle(context, winner="a"))
        self.assert_session_read_only(page, context)

    def test_other_session_and_unrelated_messages_do_not_shadow_exact_initial_state(self):
        context = _context()
        other = {**context, "evaluationSessionId": str(uuid.uuid4())}
        session = _session_data(context, [_feedback(context)])
        session["messages"].insert(
            0,
            {"id": str(uuid.uuid4()), "role": "user", "evaluationSessionId": EVALUATION_ID},
        )
        session["messages"].reverse()  # A/B positions, not list order, bind the lanes.
        body = _html_flight(
            _flight({"other": _session_data(other), "initialState": session})
        )
        page = BattlePage(responses=[_response(body)])
        self.assertEqual(self.run_async(self.client(page).vote_battle(context, winner="a")), NAMES)
        self.assert_session_read_only(page, context)

    def test_invalid_winner_or_context_fails_before_opening_any_browser(self):
        client = self.client()
        opened = Mock(side_effect=AssertionError("invalid context must not open a browser"))
        self._patch(client, "_battle_page", opened)
        for winner in ("", "A", "c", "skip", None):
            with self.subTest(winner=winner), self.assertRaises(bridge_client.BridgeError):
                self.run_async(client.vote_battle(_context(), winner=winner))
        invalid = [None, [], {}, {**_context(), "browser_scope": ""}]
        for field in ("evaluationSessionId", "messageAId", "messageBId"):
            invalid.extend(({**_context(), field: "invalid"}, {**_context(), field: None}))
            incomplete = _context()
            del incomplete[field]
            invalid.append(incomplete)
        for context in invalid:
            for operation in (
                client.reveal_battle,
                lambda ctx: client.vote_battle(ctx, winner="a"),
            ):
                with self.subTest(context=context), self.assertRaises(bridge_client.BridgeError):
                    self.run_async(operation(context))
        opened.assert_not_called()

    def test_browser_scope_mismatch_stops_before_live_state_and_all_requests(self):
        for reveal in (False, True):
            with self.subTest(reveal=reveal):
                page = BattlePage(scope="cdp:other-offline-browser")
                client = self.client(page)
                operation = (
                    client.reveal_battle(_context())
                    if reveal
                    else client.vote_battle(_context(), winner="b")
                )
                with self.assertRaisesRegex(bridge_client.BridgeError, "不能换账号"):
                    self.run_async(operation)
                self.assertEqual(page.cookie_reads, [])
                self.assertEqual(page.requests, [])
                self.assertEqual(page.actions, [])
                self.assertEqual(page.action_reads, 0)

    def test_logged_out_account_cannot_vote_even_on_the_original_browser(self):
        page = BattlePage(cookies=[])
        with self.assertRaises(bridge_client.BridgeError):
            self.run_async(self.client(page).vote_battle(_context(), winner="a"))
        self.assertEqual(page.requests, [])
        self.assertEqual(page.actions, [])

    def test_existing_matching_feedback_is_read_only_for_vote_retries_and_reveal(self):
        # Reveal accepts any recorded value, including historical SKIP; it never submits one.
        for value in ("model_a", "model_b", "skip", "legacy_value"):
            operations = ["reveal"]
            if value in ("model_a", "model_b"):
                operations.append("vote_retry")
            for operation in operations:
                with self.subTest(value=value, operation=operation):
                    context = _context()
                    unrelated = _feedback(context, "model_b", messageAId=str(uuid.uuid4()))
                    feedbacks = [unrelated, _feedback(context, value)]
                    page = BattlePage(responses=[_response(_session_body(context, feedbacks))])
                    client = self.client(page)
                    result = (
                        client.reveal_battle(context)
                        if operation == "reveal"
                        else client.vote_battle(context, winner=value[-1])
                    )
                    self.assertEqual(self.run_async(result), NAMES)
                    self.assert_session_read_only(page, context)

    def test_successful_vote_then_same_session_retry_does_not_resubmit(self):
        context = _context()
        page = BattlePage(
            responses=[
                _response(_session_body(context)),
                _success(),
                _response(_session_body(context, [_feedback(context)])),
            ]
        )
        client = self.client(page)

        async def scenario():
            first = await client.vote_battle(context, winner="a")
            again = await client.vote_battle(context, winner="a")
            return first, again

        self.assertEqual(self.run_async(scenario()), (NAMES, NAMES))
        self.assertEqual([request["method"] for request in page.requests], ["GET", "POST", "GET"])
        self.assertEqual(page.actions, ["pairwise_feedback"])
        self.assertEqual(page.action_reads, 1)

    def test_recorded_feedback_without_names_confirms_only_vote_not_read_only_reveal(self):
        context = _context()
        for value in ("model_a", "model_b", "skip", "legacy_value"):
            with self.subTest(value=value):
                body = _session_body(context, [_feedback(context, value)], revealed_models=[])
                can_retry_vote = value in ("model_a", "model_b")
                page = BattlePage(responses=[_response(body)] * (4 if can_retry_vote else 2))
                client = self.client(page)
                if can_retry_vote:
                    for _attempt in range(2):
                        self.assertEqual(
                            self.run_async(client.vote_battle(context, winner=value[-1])),
                            {"success": True, "reveal_pending": True},
                        )
                # Reveal never returns pending or submits a new vote, including historical SKIP.
                for _attempt in range(2):
                    with self.assertRaisesRegex(bridge_client.BridgeError, "不会重复提交"):
                        self.run_async(client.reveal_battle(context))
                self.assertEqual(
                    [request["method"] for request in page.requests],
                    ["GET"] * (4 if can_retry_vote else 2),
                )
                self.assertEqual(page.actions, [])
                self.assertEqual(page.action_reads, 0)
                self.assertEqual(page.table_reads, 0)
                self.assertEqual(page.reload_calls, 0)

    def test_recorded_different_preference_or_skip_cannot_be_changed(self):
        context = _context()
        for recorded, requested in (("model_a", "b"), ("model_b", "a"), ("skip", "a")):
            with self.subTest(recorded=recorded, requested=requested):
                page = BattlePage(
                    responses=[_response(_session_body(context, [_feedback(context, recorded)]))]
                )
                with self.assertRaisesRegex(bridge_client.BridgeError, "不能更改盲测结果"):
                    self.run_async(self.client(page).vote_battle(context, winner=requested))
                self.assertEqual([request["method"] for request in page.requests], ["GET"])
                self.assertEqual(page.actions, [])
                self.assertEqual(page.action_reads, 0)

    def test_reveal_after_successful_vote_is_read_only_without_resubmitting(self):
        # This is also the real command flow: battle_vote calls reveal_battle
        # immediately after vote_battle. Reading names must not change the vote.
        context = _context()
        page = BattlePage(
            responses=[
                _response(_session_body(context)),
                _success(),
                _response(_session_body(context, [_feedback(context)])),
            ]
        )
        client = self.client(page)

        async def scenario():
            await client.vote_battle(context, winner="a")
            return await client.reveal_battle(context)

        self.assertEqual(self.run_async(scenario()), NAMES)
        self.assertEqual([request["method"] for request in page.requests], ["GET", "POST", "GET"])
        self.assertEqual(page.actions, ["pairwise_feedback"])
        self.assertEqual(page.action_reads, 1)

    def test_confirmation_without_names_can_recover_by_reading_recorded_feedback(self):
        context = _context()
        incomplete = {"success": True, "data": {"modelAId": MODEL_A_ID, "modelBId": MODEL_B_ID}}
        page = BattlePage(
            responses=[
                _response(_session_body(context)),
                _response(_flight(incomplete)),
                _response(_flight({})),
                _response(_session_body(context, [_feedback(context)])),
            ]
        )
        client = self.client(page)
        self.assertEqual(
            self.run_async(client.vote_battle(context, winner="a")),
            {"success": True, "reveal_pending": True},
        )
        self.assertEqual(self.run_async(client.vote_battle(context, winner="a")), NAMES)
        self.assertEqual(
            [request["method"] for request in page.requests], ["GET", "POST", "GET", "GET"]
        )
        self.assertEqual(page.actions, ["pairwise_feedback"])
        self.assertEqual(page.action_reads, 1)

    def test_success_missing_revealed_models_fetches_only_that_session_for_names(self):
        context = _context()
        page = BattlePage(
            responses=[
                _response(_session_body(context)),
                _response(
                    _flight(
                        {"success": True, "data": {"modelAId": MODEL_A_ID, "modelBId": MODEL_B_ID}}
                    )
                ),
                _response(_session_body(context)),
            ]
        )
        self.assertEqual(self.run_async(self.client(page).vote_battle(context, winner="b")), NAMES)
        self.assertEqual([request["method"] for request in page.requests], ["GET", "POST", "GET"])
        self.assertTrue(all(request["path"] == "/c/" + EVALUATION_ID for request in page.requests))
        self.assertEqual(page.table_reads, 0)

    def test_success_missing_data_confirms_vote_pending_then_reveals_without_resubmission(self):
        context = _context()
        payloads = [{"success": True}] + [
            {"success": True, "data": data} for data in (None, [], "not data", {})
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                action = _action_id()
                page = BattlePage(
                    responses=[
                        _response(_session_body(context)),
                        _response(_flight(payload)),
                        _response(_session_body(context, [_feedback(context)], revealed_models=[])),
                        _response(_session_body(context, [_feedback(context)])),
                    ],
                    action_sets=[{"createPairwiseFeedback": action}],
                )
                client = self.client(page)
                self.assertEqual(
                    self.run_async(client.vote_battle(context, winner="a")),
                    {"success": True, "reveal_pending": True},
                )
                self.assertEqual(self.run_async(client.reveal_battle(context)), NAMES)
                self.assertEqual(
                    [request["method"] for request in page.requests],
                    ["GET", "POST", "GET", "GET"],
                )
                self.assert_feedback_post(page, context, "model_a", action)
                self.assertEqual(page.action_reads, 1)
                self.assertEqual(page.reload_calls, 0)

    def test_confirmation_with_missing_or_partial_names_returns_pending_not_fabricated_names(self):
        context = _context()
        partial = _revealed_data()
        partial["revealedModels"] = [row for row in _models() if row["id"] == MODEL_B_ID]
        blank = _revealed_data()
        for row in blank["revealedModels"]:
            row.update(displayName="", publicName="")
        payloads = [
            {"revealedModels": _models()},  # No A/B IDs: never guess catalog order.
            {"modelAId": MODEL_A_ID, "modelBId": MODEL_B_ID},
            partial,
            blank,
        ]
        for data in payloads:
            with self.subTest(data=data):
                action = _action_id()
                feedback = _feedback(context, "model_b")
                read_feedback = _feedback(
                    context,
                    "model_b",
                    modelAId=data.get("modelAId"),
                    modelBId=data.get("modelBId"),
                )
                page = BattlePage(
                    responses=[
                        _response(_session_body(context, revealed_models=[])),
                        _response(_flight({"success": True, "data": data})),
                        _response(_session_body(context, [feedback], revealed_models=[])),
                        _response(
                            _session_body(
                                context,
                                [read_feedback],
                                revealed_models=data.get("revealedModels", []),
                            )
                        ),
                    ],
                    action_sets=[{"createPairwiseFeedback": action}],
                )
                client = self.client(page)
                self.assertEqual(
                    self.run_async(client.vote_battle(context, winner="b")),
                    {"success": True, "reveal_pending": True},
                )
                # Pending confirms the vote, not names. Reveal only reads the recorded result.
                with self.assertRaisesRegex(bridge_client.BridgeError, "模型名未返回"):
                    self.run_async(client.reveal_battle(context))
                self.assertEqual(
                    [request["method"] for request in page.requests], ["GET", "POST", "GET", "GET"]
                )
                self.assert_feedback_post(page, context, "model_b", action)
                self.assertEqual(page.action_reads, 1)
                self.assertEqual(page.reload_calls, 0)

    def test_confirmed_vote_survives_failed_or_unverified_name_readback(self):
        context = _context()
        other = {**context, "evaluationSessionId": str(uuid.uuid4())}
        readbacks = [
            _response("failed " + NAMES["a"], 500),
            _response('<html><form action="/login">Sign in</form></html>'),
            _response(_html_flight(_flight({}))),
            _response(_session_body(other, [_feedback(context)])),
        ]
        for readback in readbacks:
            with self.subTest(readback=readback):
                page = BattlePage(
                    responses=[
                        _response(_session_body(context)),
                        _response(_flight({"success": True})),
                        readback,
                        _response(_session_body(context, [_feedback(context)])),
                    ]
                )
                client = self.client(page)
                self.assertEqual(
                    self.run_async(client.vote_battle(context, winner="a")),
                    {"success": True, "reveal_pending": True},
                )
                self.assertEqual(self.run_async(client.reveal_battle(context)), NAMES)
                self.assertEqual(
                    [request["method"] for request in page.requests],
                    ["GET", "POST", "GET", "GET"],
                )
                self.assertEqual(page.actions, ["pairwise_feedback"])
                self.assertEqual(page.action_reads, 1)
                self.assertEqual(page.table_reads, 0)

    def test_confirmed_vote_survives_name_readback_timeout_and_cannot_change_preference(self):
        context = _context()
        page = BattlePage()
        fetched = AsyncMock(
            side_effect=[
                _response(_session_body(context)),
                _response(_flight({"success": True})),
                TimeoutError("offline name readback timeout"),
                _response(_session_body(context, [_feedback(context)])),
                _response(_session_body(context, [_feedback(context)])),
            ]
        )
        self._patch(page, "fetch", fetched)
        client = self.client(page)
        self.assertEqual(
            self.run_async(client.vote_battle(context, winner="a")),
            {"success": True, "reveal_pending": True},
        )
        with self.assertRaisesRegex(bridge_client.BridgeError, "不能更改盲测结果"):
            self.run_async(client.vote_battle(context, winner="b"))
        self.assertEqual(self.run_async(client.reveal_battle(context)), NAMES)
        calls = fetched.await_args_list
        self.assertEqual(
            [call.kwargs["method"] for call in calls], ["GET", "POST", "GET", "GET", "GET"]
        )
        self.assertTrue(all(call.args == ("/c/" + EVALUATION_ID,) for call in calls))
        self.assertEqual(json.loads(calls[1].kwargs["body"])[0]["value"], "model_a")
        self.assertEqual(page.actions, ["pairwise_feedback"])
        self.assertEqual(page.action_reads, 1)
        self.assertEqual(page.table_reads, 0)

    def test_dynamic_next_action_is_rediscovered_not_hardcoded_or_taken_from_cache(self):
        context = _context()
        other = {**context, "evaluationSessionId": str(uuid.uuid4())}
        actions = [_action_id(), _action_id()]
        self.assertNotEqual(*actions)
        page = BattlePage(
            responses=[
                _response(_session_body(context)),
                _success(),
                _response(_session_body(other)),
                _success(),
            ],
            action_sets=[{"createPairwiseFeedback": action} for action in actions],
        )
        arena_direct.cache_scope(page).update(
            actions={"createPairwiseFeedback": "STALE_CACHE_ID"}, actions_at=time.time()
        )
        client = self.client(page)
        self.assertEqual(self.run_async(client.vote_battle(context, winner="a")), NAMES)
        self.assertEqual(self.run_async(client.vote_battle(other, winner="b")), NAMES)
        posts = [request for request in page.requests if request["method"] == "POST"]
        self.assertEqual([request["headers"]["Next-Action"] for request in posts], actions)
        self.assertEqual(page.action_reads, 2)
        self.assertEqual(page.reload_calls, 0)

    def test_missing_tab_action_is_discovered_from_exact_session_scripts(self):
        context = _context()
        action = _action_id()
        initial = _session_body(context) + "".join(
            [
                '<script src="https://third-party.example.invalid/never-fetch.js"></script>',
                '<script src="/assets/not-next.js"></script>',
                '<script src="/_next/static/chunks/boot.js"></script>',
                "<script src='/_next/static/chunks/session-only.js'></script>",
                '<script src="/_next/static/chunks/session-only.js"></script>',
            ]
        )
        page = BattlePage(
            responses=[
                _response(initial),
                _response(_bundle(_action_id(), "generateUploadUrl")),
                _response(_bundle(action)),
                _success(),
            ],
            action_sets=[{}],
            url="https://arena.ai/text/direct",  # An old non-Battle tab is enough.
        )
        self.assertEqual(self.run_async(self.client(page).vote_battle(context, winner="a")), NAMES)
        self.assertEqual(
            [(request["method"], request["path"]) for request in page.requests],
            [
                ("GET", "/c/" + EVALUATION_ID),
                ("GET", "/_next/static/chunks/boot.js"),
                ("GET", "/_next/static/chunks/session-only.js"),
                ("POST", "/c/" + EVALUATION_ID),
            ],
        )
        self.assertTrue(all(request["timeout"] <= 10.0 for request in page.requests[1:3]))
        self.assert_feedback_post(page, context, "model_a", action)
        self.assertEqual(page.action_reads, 0)

    def test_current_session_script_action_wins_over_nonempty_old_tab_and_cache(self):
        context = _context()
        stale, current = _action_id(), _action_id()
        page = BattlePage(
            responses=[
                _response(
                    _session_body(context)
                    + '<script src="/_next/static/chunks/current-session.js"></script>'
                ),
                _response(_bundle(current)),
                _success(),
            ],
            action_sets=[{"createPairwiseFeedback": stale}],
            url="https://arena.ai/text/direct",
        )
        arena_direct.cache_scope(page).update(
            actions={"createPairwiseFeedback": stale}, actions_at=time.time()
        )
        self.assertEqual(self.run_async(self.client(page).vote_battle(context, winner="a")), NAMES)
        self.assertEqual(
            [(request["method"], request["path"]) for request in page.requests],
            [
                ("GET", "/c/" + EVALUATION_ID),
                ("GET", "/_next/static/chunks/current-session.js"),
                ("POST", "/c/" + EVALUATION_ID),
            ],
        )
        self.assert_feedback_post(page, context, "model_a", current)
        self.assertNotEqual(page.requests[-1]["headers"]["Next-Action"], stale)
        self.assertEqual(page.action_reads, 0)
        self.assertEqual(page.reload_calls, 0)
        self.assertLessEqual(page.requests[1]["timeout"], 10.0)

    def test_action_after_sixtieth_script_uses_current_session_not_old_tab(self):
        context = _context()
        original_wait_for = arena_battle.asyncio.wait_for
        for action_position in (80, 160):
            with self.subTest(action_position=action_position):
                stale, current = _action_id(), _action_id()
                scripts = [
                    f"/_next/static/chunks/session-{index:03d}.js"
                    for index in range(1, max(103, action_position + 1) + 1)
                ]
                initial = _session_body(context) + "".join(
                    f'<script src="{src}"></script>' for src in scripts
                )
                page = BattlePage(
                    responses=[
                        _response(initial),
                        *[_response("/* unrelated chunk */") for _ in range(action_position - 1)],
                        _response(_bundle(current)),
                        _success(),
                    ],
                    action_sets=[{"createPairwiseFeedback": stale}],
                    url="https://arena.ai/text/direct",
                )
                arena_direct.cache_scope(page).update(
                    actions={"createPairwiseFeedback": stale}, actions_at=time.time()
                )
                waited = AsyncMock(wraps=original_wait_for)
                self._patch(arena_battle.asyncio, "wait_for", waited)
                self.assertEqual(
                    self.run_async(self.client(page).vote_battle(context, winner="a")), NAMES
                )
                self.assertEqual(
                    [(request["method"], request["path"]) for request in page.requests],
                    [("GET", "/c/" + EVALUATION_ID)]
                    + [("GET", src) for src in scripts[:action_position]]
                    + [("POST", "/c/" + EVALUATION_ID)],
                )
                self.assert_feedback_post(page, context, "model_a", current)
                self.assertNotEqual(page.requests[-1]["headers"]["Next-Action"], stale)
                self.assertTrue(
                    all(request["timeout"] <= 10.0 for request in page.requests[1:-1])
                )
                waited.assert_awaited_once()
                self.assertEqual(waited.await_args.kwargs["timeout"], 60.0)
                self.assertEqual(page.action_reads, 0)
                self.assertEqual(page.reload_calls, 0)
                self.assertEqual(page._responses, [])

    def test_action_beyond_160_scripts_fails_closed_without_old_tab_fallback(self):
        context = _context()
        stale, current = _action_id(), _action_id()
        scripts = [f"/_next/static/chunks/session-{index:03d}.js" for index in range(1, 162)]
        initial = _session_body(context) + "".join(
            f'<script src="{src}"></script>' for src in scripts
        )
        beyond_limit = _response(_bundle(current))
        page = BattlePage(
            responses=[
                _response(initial),
                *[_response("/* unrelated chunk */") for _ in range(160)],
                beyond_limit,
            ],
            action_sets=[{"createPairwiseFeedback": stale}],
        )
        arena_direct.cache_scope(page).update(
            actions={"createPairwiseFeedback": stale}, actions_at=time.time()
        )
        with self.assertRaisesRegex(bridge_client.BridgeError, "投票 Action 未找到"):
            self.run_async(self.client(page).vote_battle(context, winner="b"))
        self.assertEqual(
            [(request["method"], request["path"]) for request in page.requests],
            [("GET", "/c/" + EVALUATION_ID)] + [("GET", src) for src in scripts[:160]],
        )
        self.assertEqual(page._responses, [beyond_limit])  # Script 161 must remain unread.
        self.assertEqual(page.actions, [])
        self.assertEqual(page.action_reads, 0)
        self.assertEqual(page.table_reads, 0)
        self.assertEqual(page.reload_calls, 0)

    def test_total_action_scan_deadline_aborts_before_token_or_post(self):
        context = _context()

        async def expired_scan(coro, *, timeout):
            coro.close()  # Simulate wait_for expiry without leaving an unawaited coroutine.
            raise TimeoutError("offline total action scan deadline")

        for client_timeout in (300.0, 15.0):
            with self.subTest(client_timeout=client_timeout):
                page = BattlePage(
                    responses=[
                        _response(
                            _session_body(context)
                            + '<script src="/_next/static/chunks/current-session.js"></script>'
                        )
                    ],
                    action_sets=[{"createPairwiseFeedback": _action_id()}],
                )
                waited = AsyncMock(side_effect=expired_scan)
                self._patch(arena_battle.asyncio, "wait_for", waited)
                with self.assertRaisesRegex(bridge_client.BridgeError, "投票接口超时，未提交评价"):
                    self.run_async(
                        self.client(page, timeout=client_timeout).vote_battle(context, winner="a")
                    )
                waited.assert_awaited_once()
                self.assertEqual(waited.await_args.kwargs["timeout"], min(client_timeout, 60.0))
                self.assert_session_read_only(page, context)

    def test_script_fetch_timeout_aborts_scan_without_token_post_or_old_tab_action(self):
        context = _context()
        script = "/_next/static/chunks/current-session.js"
        page = BattlePage(action_sets=[{"createPairwiseFeedback": _action_id()}])
        fetched = AsyncMock(
            side_effect=[
                _response(_session_body(context) + f'<script src="{script}"></script>'),
                TimeoutError("offline script fetch timeout"),
            ]
        )
        self._patch(page, "fetch", fetched)
        with self.assertRaisesRegex(bridge_client.BridgeError, "投票接口超时，未提交评价"):
            self.run_async(self.client(page).vote_battle(context, winner="b"))
        self.assertEqual(
            [(call.kwargs["method"], call.args[0]) for call in fetched.await_args_list],
            [("GET", "/c/" + EVALUATION_ID), ("GET", script)],
        )
        self.assertLessEqual(fetched.await_args_list[1].kwargs["timeout"], 10.0)
        self.assertEqual(page.actions, [])
        self.assertEqual(page.action_reads, 0)
        self.assertEqual(page.table_reads, 0)
        self.assertEqual(page.reload_calls, 0)

    def test_flight_import_or_preload_only_action_uses_current_build_without_stale_fallback(self):
        context = _context()
        for source in ("flight_import", "preload"):
            for script_count in (0, 56):
                with self.subTest(source=source, script_count=script_count):
                    stale, current = _action_id(), _action_id()
                    boot_scripts = [
                        f"/_next/static/chunks/boot-{index:03d}.js"
                        for index in range(1, script_count + 1)
                    ]
                    modules = [
                        f"/_next/static/chunks/import-{index:03d}.js" for index in range(1, 8)
                    ]
                    state = _flight({"initialState": _session_data(context)})
                    if source == "flight_import":
                        # Next Flight I rows reference route modules without script src tags.
                        state += "2:I" + json.dumps([1234, modules, "default"]) + "\n"
                    initial = _html_flight(state) + "".join(
                        f'<script src="{src}"></script>' for src in boot_scripts
                    )
                    if source == "preload":
                        initial += "".join(
                            f'<link rel="preload" as="script" href="{src}">' for src in modules
                        )
                    self.assertNotIn(f'src="{modules[-1]}"', initial)
                    paths = boot_scripts + modules
                    page = BattlePage(
                        responses=[
                            _response(initial),
                            *[_response("/* unrelated chunk */") for _ in range(len(paths) - 1)],
                            _response(_bundle(current)),
                            _success(),
                        ],
                        action_sets=[{"createPairwiseFeedback": stale}],
                        url="https://arena.ai/text/direct",
                    )
                    arena_direct.cache_scope(page).update(
                        actions={"createPairwiseFeedback": stale}, actions_at=time.time()
                    )
                    self.assertEqual(
                        self.run_async(self.client(page).vote_battle(context, winner="a")), NAMES
                    )
                    self.assertEqual(
                        [(request["method"], request["path"]) for request in page.requests],
                        [("GET", "/c/" + EVALUATION_ID)]
                        + [("GET", src) for src in paths]
                        + [("POST", "/c/" + EVALUATION_ID)],
                    )
                    self.assert_feedback_post(page, context, "model_a", current)
                    self.assertNotEqual(page.requests[-1]["headers"]["Next-Action"], stale)
                    self.assertEqual(page.action_reads, 0)
                    self.assertEqual(page.reload_calls, 0)
                    self.assertEqual(page._responses, [])

    def test_foreign_asset_urls_are_never_fetched_only_same_origin_next_paths(self):
        context = _context()
        foreign_script = "https://assets.example.invalid/_next/static/chunks/foreign-script.js"
        foreign_preload = "//assets.example.invalid/_next/static/chunks/foreign-preload.js"
        unrelated_foreign = "https://third-party.example.invalid/outside.js"
        foreign_paths = [
            "/_next/static/chunks/foreign-script.js",
            "/_next/static/chunks/foreign-preload.js",
        ]
        current_path = "/_next/static/chunks/current-preload.js"
        for has_current in (False, True):
            with self.subTest(has_current=has_current):
                stale, current = _action_id(), _action_id()
                initial = (
                    _session_body(context)
                    + f'<script src="{foreign_script}"></script>'
                    + f'<link rel="preload" as="script" href="{foreign_preload}">'
                    + f'<script src="{unrelated_foreign}"></script>'
                )
                if has_current:
                    initial += f'<link rel="preload" as="script" href="{current_path}">'
                paths = foreign_paths + ([current_path] if has_current else [])
                replies = [_response(initial)] + [_response("/* no action */") for _ in foreign_paths]
                if has_current:
                    replies += [_response(_bundle(current)), _success()]
                page = BattlePage(
                    responses=replies, action_sets=[{"createPairwiseFeedback": stale}]
                )
                arena_direct.cache_scope(page).update(
                    actions={"createPairwiseFeedback": stale}, actions_at=time.time()
                )
                operation = self.client(page).vote_battle(context, winner="b")
                if has_current:
                    self.assertEqual(self.run_async(operation), NAMES)
                    self.assert_feedback_post(page, context, "model_b", current)
                else:
                    with self.assertRaisesRegex(bridge_client.BridgeError, "投票 Action 未找到"):
                        self.run_async(operation)
                    self.assertEqual(page.actions, [])
                # Regex may extract /_next paths from URLs; fetch must remain same-origin.
                self.assertEqual(
                    [(request["method"], request["path"]) for request in page.requests],
                    [("GET", "/c/" + EVALUATION_ID)]
                    + [("GET", src) for src in paths]
                    + ([("POST", "/c/" + EVALUATION_ID)] if has_current else []),
                )
                fetched_paths = [request["path"] for request in page.requests]
                for foreign in (foreign_script, foreign_preload, unrelated_foreign):
                    self.assertNotIn(foreign, fetched_paths)
                self.assertTrue(
                    all(path.startswith("/") and not path.startswith("//") for path in fetched_paths)
                )
                self.assertEqual(page.action_reads, 0)
                self.assertEqual(page.table_reads, 0)
                self.assertEqual(page.reload_calls, 0)
                self.assertEqual(page._responses, [])

    def test_present_session_scripts_without_feedback_action_never_fall_back_to_stale_tab(self):
        context = _context()
        for response in (
            _response("unavailable", 404),
            _response(_bundle(_action_id(), "generateUploadUrl")),
        ):
            with self.subTest(response=response):
                page = BattlePage(
                    responses=[
                        _response(
                            _session_body(context)
                            + '<script src="/_next/static/chunks/current-session.js"></script>'
                        ),
                        response,
                    ],
                    action_sets=[{"createPairwiseFeedback": _action_id()}],
                )
                with self.assertRaisesRegex(bridge_client.BridgeError, "投票 Action 未找到"):
                    self.run_async(self.client(page).vote_battle(context, winner="b"))
                self.assertEqual([request["method"] for request in page.requests], ["GET", "GET"])
                self.assertEqual(page.action_reads, 0)
                self.assertEqual(page.actions, [])
                self.assertEqual(page.table_reads, 0)
                self.assertEqual(page.reload_calls, 0)

    def test_no_dynamic_feedback_action_means_no_token_and_no_post(self):
        page = BattlePage(responses=[_response(_session_body(_context()))], action_sets=[{}])
        with self.assertRaisesRegex(bridge_client.BridgeError, "投票 Action 未找到"):
            self.run_async(self.client(page).vote_battle(_context(), winner="a"))
        self.assertEqual([request["method"] for request in page.requests], ["GET"])
        self.assertEqual(page.actions, [])

    def test_non200_feedback_errors_do_not_leak_hidden_models_or_retry(self):
        context = _context()
        hidden_body = _flight(
            {"success": True, "data": _revealed_data(), "error": {"message": NAMES["a"]}}
        )
        for status in (0, 401, 403, 404, 429, 500):
            for winner in ("a", "b"):
                with self.subTest(status=status, winner=winner):
                    page = BattlePage(
                        responses=[_response(_session_body(context)), _response(hidden_body, status)]
                    )
                    with self.assertRaises(bridge_client.BridgeError) as caught:
                        self.run_async(self.client(page).vote_battle(context, winner=winner))
                    self.assertEqual(
                        str(caught.exception),
                        f"匿名 Battle 上游请求失败（HTTP {status}），未揭晓模型；请稍后重试",
                    )
                    self.assertEqual(caught.exception.status_code, status or None)
                    self.assertIsNone(caught.exception.payload)
                    for hidden in (*NAMES.values(), MODEL_A_ID, MODEL_B_ID, "Public Alpha"):
                        self.assertNotIn(hidden, repr(caught.exception))
                    self.assertEqual([request["method"] for request in page.requests], ["GET", "POST"])
                    self.assertEqual(page.actions, ["pairwise_feedback"])
                    self.assertEqual(page.action_reads, 1)
                    self.assertEqual(page.table_reads, 0)
                    self.assertEqual(page.reload_calls, 0)
                    self.assertEqual(arena_direct._HEALTH, {"models": {}, "variants": {}})

    def test_stale_or_failed_action_does_not_retry_a_potentially_recorded_vote(self):
        for status, body, message in (
            (404, "Server action not found.", "更新了投票接口"),
            (500, "internal failure", "HTTP 500"),
        ):
            with self.subTest(status=status):
                page = BattlePage(
                    responses=[_response(_session_body(_context())), _response(body, status)]
                )
                with self.assertRaisesRegex(bridge_client.BridgeError, message):
                    self.run_async(self.client(page).vote_battle(_context(), winner="a"))
                self.assertEqual([request["method"] for request in page.requests], ["GET", "POST"])
                self.assertEqual(page.actions, ["pairwise_feedback"])
                self.assertEqual(page.action_reads, 1)
                self.assertEqual(page.reload_calls, 0)


class BattleRelayBindingTests(DirectTransportCase):
    """Real RelayServer selection/RelayPage dispatch; no socket server is started."""

    def relay(self, replies=()):
        server = arena_relay.RelayServer(
            host="127.0.0.1", secret="offline-battle-test-secret", data_dir=self.data_dir
        )
        original = arena_relay.RelayBrowser("origin-browser")
        other = arena_relay.RelayBrowser("other-browser")
        server._browsers = {original.id: original, other.id: other}
        # The generic selector would choose the other browser after this one.
        original.last_job_at = time.time()
        queue = list(replies)
        seen = []
        action = _action_id()
        logged_in = {
            "has_arena_auth": True,
            "has_logged_in": True,
            "session_logged_in": True,
            "verified": True,
            "recaptcha_action": "chat_submit",
            "session_source": "userscript",
        }

        async def submit(browser, verb, payload=None, *, timeout=None):
            seen.append((browser.id, verb, dict(payload or {}), timeout))
            if verb == "state":
                return {"state": dict(logged_in)}
            if verb == "actions":
                return {"actions": {"createPairwiseFeedback": action}}
            if verb == "recaptcha":
                return {"token": "offline-relay-token"}
            if verb == "fetch":
                if not queue:
                    raise AssertionError("unexpected relay fetch: " + str(payload))
                return queue.pop(0)
            raise AssertionError("unexpected relay verb: " + verb)

        self._patch(server, "submit", submit)
        server.acquire = AsyncMock(wraps=server.acquire)
        return server, original, other, seen, action

    def user_client(self, server):
        return arena_relay.ArenaUserscriptClient(server, data_dir=self.data_dir, acquire_wait=0)

    def test_vote_is_pinned_to_origin_not_the_next_available_browser(self):
        context = _context("relay:origin-browser")
        server, original, other, seen, action = self.relay(
            [_response(_session_body(context)), _success()]
        )
        self.assertIs(server.pick(), other)
        self.assertEqual(
            self.run_async(self.user_client(server).vote_battle(context, winner="b")), NAMES
        )
        self.assertFalse(server.running)
        server.acquire.assert_not_awaited()
        self.assertTrue(all(browser_id == original.id for browser_id, *_rest in seen))
        self.assertEqual(
            [verb for _browser_id, verb, _payload, _timeout in seen],
            ["state", "fetch", "actions", "recaptcha", "fetch"],
        )
        recaptcha = next(
            payload for _browser_id, verb, payload, _timeout in seen if verb == "recaptcha"
        )
        self.assertEqual(recaptcha, {"action": "pairwise_feedback"})
        fetches = [payload for _browser_id, verb, payload, _timeout in seen if verb == "fetch"]
        self.assertEqual(fetches[0]["method"], "GET")
        self.assertEqual(fetches[0]["path"], "/c/" + EVALUATION_ID)
        self.assertEqual(fetches[1]["headers"]["Next-Action"], action)
        sent = json.loads(fetches[1]["body"])
        self.assertEqual(
            sent,
            [
                {
                    "evaluationSessionId": EVALUATION_ID,
                    "messageAId": MESSAGE_A_ID,
                    "messageBId": MESSAGE_B_ID,
                    "value": "model_b",
                    "recaptchaV3Token": "offline-relay-token",
                }
            ],
        )

    def test_reveal_is_read_only_and_pinned_to_origin_for_unvoted_and_recorded_sessions(self):
        context = _context("relay:origin-browser")
        for value in (None, "model_a", "model_b", "skip", "legacy_value"):
            with self.subTest(value=value):
                feedbacks = [] if value is None else [_feedback(context, value)]
                server, original, other, seen, _action = self.relay(
                    [_response(_session_body(context, feedbacks))]
                )
                self.assertIs(server.pick(), other)
                operation = self.user_client(server).reveal_battle(context)
                if value is None:
                    with self.assertRaisesRegex(bridge_client.BridgeError, "官网尚未记录评价"):
                        self.run_async(operation)
                else:
                    self.assertEqual(self.run_async(operation), NAMES)
                server.acquire.assert_not_awaited()
                self.assertFalse(server.running)
                self.assertTrue(all(browser_id == original.id for browser_id, *_rest in seen))
                self.assertEqual(
                    [verb for _browser_id, verb, _payload, _timeout in seen], ["state", "fetch"]
                )
                request = seen[1][2]
                self.assertEqual(request["method"], "GET")
                self.assertEqual(request["path"], "/c/" + EVALUATION_ID)
                self.assertFalse(request.get("body"))
                self.assertNotIn("Next-Action", request.get("headers") or {})

    def test_begin_then_read_only_retry_keeps_the_original_relay_browser(self):
        server, original, other, _seen, _action = self.relay([_response(STREAM)])
        original.last_job_at = 0
        other.last_job_at = time.time() + 60
        client = self.user_client(server)
        result = self.run_async(client.begin_battle(prompt="fixture"))
        context = result["context"]
        self.assertEqual(context["browser_scope"], "relay:" + original.id)
        server.acquire.assert_awaited_once()
        server.acquire.reset_mock()
        original.last_job_at = time.time() + 60
        other.last_job_at = 0
        self.assertIs(server.pick(), other)
        session = _response(_session_body(context, [_feedback(context)]))
        self._patch(
            server,
            "submit",
            AsyncMock(
                side_effect=[
                    {"state": {"has_arena_auth": True, "has_logged_in": True}},
                    session,
                ]
            ),
        )
        self.assertEqual(self.run_async(client.vote_battle(context, winner="a")), NAMES)
        server.acquire.assert_not_awaited()
        calls = server.submit.await_args_list
        self.assertEqual([call.args[1] for call in calls], ["state", "fetch"])
        self.assertTrue(all(call.args[0] is original for call in calls))
        self.assertEqual(calls[1].args[2]["method"], "GET")

    def test_offline_or_missing_origin_does_not_fall_back_to_an_online_browser(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                server, original, other, seen, _action = self.relay()
                if missing:
                    del server._browsers[original.id]
                else:
                    original.last_seen = time.time() - arena_relay.BROWSER_STALE_SECONDS - 1
                self.assertIs(server.pick(), other)
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    self.run_async(
                        self.user_client(server).vote_battle(
                            _context("relay:" + original.id), winner="a"
                        )
                    )
                self.assertEqual(caught.exception.code, "relay_browser_lost")
                server.acquire.assert_not_awaited()
                self.assertEqual(seen, [])
                self.assertFalse(server.running)

    def test_direct_scope_is_rejected_by_userscript_transport_before_dispatch(self):
        server, _original, _other, seen, _action = self.relay()
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(self.user_client(server).reveal_battle(_context()))
        self.assertEqual(caught.exception.code, "relay_browser_lost")
        self.assertEqual(seen, [])
        server.acquire.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
