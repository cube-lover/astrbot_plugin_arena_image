"""Regression tests for provider errors hidden inside Arena HTTP-200 streams."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

from astrbot_plugin_arena_image import arena_direct, bridge_client
from tests.test_arena_direct_transport import (
    DirectTransportCase,
    FakePage,
    _chunked_cookies,
    _filler_models,
    _image_model,
    _session_token,
    _uuid7,
)
from tests.test_arena_image_hardening import _make_plugin


MODEL = "gpt-image-2 (medium)"
ENDPOINT = "luna-lisa-alpha"
PROVIDER_ERROR = (
    "Error during image generation with customOpenai for model endpoint "
    f"{ENDPOINT}: Failed to fetch image: 400 Bad Request - "
    f'{{"error":{{"message":"The model \'{ENDPOINT}\' does not exist."}}}}'
)
SUCCESS = (
    'a2:[{"type":"image","image":"https://fixture.invalid/image.png"}]\nad:{"finishReason":"stop"}'
)


def reply(error="", status=200, headers=None):
    return {
        "status": status,
        "headers": headers or {},
        "text": "a3:" + json.dumps(error) if error else SUCCESS,
    }


class StreamHealthTests(DirectTransportCase):
    def setUp(self):
        super().setUp()
        self.sleep = AsyncMock()
        self._patch(arena_direct.asyncio, "sleep", self.sleep)

    def page(self, *, variants=2, responses=None):
        now = time.time()
        self.rows = [
            _image_model(MODEL, _uuid7(now - 60 * (index + 1))) for index in range(variants)
        ]
        # Both a different public model and a newer nonselectable GPT row must
        # remain excluded even when every selectable GPT variant is failing.
        return FakePage(
            cookies=_chunked_cookies(_session_token()),
            models=_filler_models()
            + self.rows
            + [
                _image_model(ENDPOINT, _uuid7(now + 10)),
                _image_model(MODEL, _uuid7(now + 20), userSelectable=False),
            ],
            responses=responses,
            response=reply(),
        )

    def test_missing_endpoint_records_real_failure_and_explains_both_names(self):
        page = self.page(variants=1, responses=[reply(PROVIDER_ERROR)])
        client = self.client(page)
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(client.complete(model=MODEL, prompt="a circle"))
        exc = caught.exception
        self.assertEqual(exc.status_code, 400)
        self.assertEqual(exc.code, "provider_model_unavailable")
        self.assertFalse(exc.requires_interactive_auth)
        self.assertEqual(exc.payload["error"]["requested_model"], MODEL)
        self.assertEqual(exc.payload["error"]["provider_endpoint"], ENDPOINT)
        self.assertEqual(exc.payload["error"]["arena_model_id"], self.rows[0]["id"])
        self.assertIn("所选模型：" + MODEL, str(exc))
        self.assertIn("上游端点", str(exc))
        self.assertEqual(len(page.requests), 1)
        health = self.run_async(client.model_health())
        self.assertEqual(health["models"][0]["status_code"], 400)
        self.assertEqual(health["models"][0]["transport_status_code"], 200)
        self.assertEqual(health["models"][0]["provider_endpoint"], ENDPOINT)
        self.assertTrue(arena_direct._variant_failed_recently(self.rows[0]["id"]))

    def test_retry_uses_only_a_different_selectable_id_of_the_same_public_name(self):
        page = self.page(responses=[reply(PROVIDER_ERROR), reply()])
        result = self.run_async(
            self.client(page).complete(model=MODEL, prompt="a circle", images=[])
        )
        submitted = [json.loads(request["body"]) for request in page.requests]
        self.assertEqual([item["modelAId"] for item in submitted], [row["id"] for row in self.rows])
        self.assertEqual(result["model"], MODEL)
        self.assertEqual(result["arena_model_id"], self.rows[1]["id"])
        self.assertEqual([item["userMessage"]["content"] for item in submitted], ["a circle"] * 2)
        self.assertNotEqual(submitted[0]["id"], submitted[1]["id"])
        self.assertEqual(page.actions, ["chat_submit", "chat_submit"])
        self.assertTrue(arena_direct._variant_failed_recently(self.rows[0]["id"]))
        self.assertFalse(arena_direct._variant_failed_recently(self.rows[1]["id"]))

    def test_at_most_one_missing_endpoint_retry_per_request(self):
        page = self.page(variants=4, responses=[reply(PROVIDER_ERROR)] * 4)
        with self.assertRaises(bridge_client.BridgeError):
            self.run_async(self.client(page).complete(model=MODEL, prompt="a circle"))
        self.assertEqual(len(page.requests), 2)
        self.assertEqual(
            [json.loads(request["body"])["modelAId"] for request in page.requests],
            [row["id"] for row in self.rows[:2]],
        )

    def test_the_next_request_skips_both_failed_variants(self):
        page = self.page(
            variants=3, responses=[reply(PROVIDER_ERROR), reply(PROVIDER_ERROR), reply()]
        )
        client = self.client(page)
        with self.assertRaises(bridge_client.BridgeError):
            self.run_async(client.complete(model=MODEL, prompt="a circle"))
        result = self.run_async(client.complete(model=MODEL, prompt="a circle"))
        self.assertEqual(result["arena_model_id"], self.rows[2]["id"])
        self.assertEqual(result["model"], MODEL)

    def test_other_stream_errors_are_502_and_do_not_retry_a_different_variant(self):
        page = self.page(responses=[reply("Response contains no images.")])
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(self.client(page).complete(model=MODEL, prompt="a circle"))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(len(page.requests), 1)
        self.assertTrue(arena_direct._variant_failed_recently(self.rows[0]["id"]))
        self.assertEqual(arena_direct._HEALTH["models"][MODEL]["status_code"], 502)

    def test_generic_generation_failure_keeps_actual_http200_and_no_retry(self):
        page = self.page(responses=[reply("The generation failed"), reply()])
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(self.client(page).complete(model=MODEL, prompt="a circle"))
        exc = caught.exception
        self.assertEqual(exc.status_code, 502)  # Compatible proxy failure classification.
        self.assertEqual(exc.code, "stream_generation_failed")
        self.assertEqual(exc.payload["error"]["transport_status_code"], 200)
        self.assertIsNone(exc.payload["error"]["reported_upstream_status_code"])
        self.assertEqual(exc.payload["error"]["requested_model"], MODEL)
        self.assertEqual(exc.payload["error"]["arena_model_id"], self.rows[0]["id"])
        self.assertFalse(exc.is_rate_limited)
        self.assertFalse(exc.requires_interactive_auth)
        self.assertIn("HTTP 200", str(exc))
        self.assertIn("The generation failed", str(exc))
        self.assertNotIn("上游错误（HTTP 502）", str(exc))
        self.assertEqual(len(page.requests), 1)
        self.sleep.assert_not_awaited()

    def test_real_http502_is_not_described_as_an_http200_stream_failure(self):
        page = self.page(responses=[reply("Bad Gateway", status=502)])
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(self.client(page).complete(model=MODEL, prompt="a circle"))
        self.assertEqual(caught.exception.code, "http_502")
        self.assertIn("HTTP 502", str(caught.exception))
        self.assertNotIn("HTTP 200", str(caught.exception))
        self.assertEqual(len(page.requests), 1)

    def test_explicit_provider_http502_keeps_reported_upstream_status(self):
        page = self.page(responses=[reply("Provider returned 502 Bad Gateway")])
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(self.client(page).complete(model=MODEL, prompt="a circle"))
        self.assertEqual(caught.exception.code, "http_502")
        self.assertIn("HTTP 502", str(caught.exception))
        self.assertNotIn("插件的失败分类", str(caught.exception))

    def test_unknown_failure_is_not_invented_as_moderation_or_auth(self):
        page = self.page(responses=[reply("The generation failed")])
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(self.client(page).complete(model=MODEL, prompt="a circle"))
        self.assertEqual(caught.exception.code, "stream_generation_failed")
        self.assertFalse(caught.exception.requires_interactive_auth)
        page = self.page(responses=[reply("recaptcha validation failed", status=403)])
        with self.assertRaises(bridge_client.BridgeError) as challenged:
            self.run_async(self.client(page).complete(model=MODEL, prompt="a circle"))
        self.assertEqual(challenged.exception.code, "arena_session_rejected")
        self.assertTrue(challenged.exception.requires_interactive_auth)

    def test_model_list_retains_http200_metadata_from_snapshot(self):
        main, plugin = _make_plugin(self, {"model_health_cache_seconds": 0})
        plugin._client = lambda: type(
            "Client", (), {"model_health": AsyncMock(return_value={
                "models": [{
                    "id": MODEL, "status_code": 502, "checked_at": time.time(),
                    "transport_status_code": "200",
                    "error_code": "stream_generation_failed",
                }],
            })},
        )()
        health = self.run_async(plugin._fetch_model_health(force=True))
        self.assertEqual(health[MODEL]["transport_status_code"], 200)
        text = main.ArenaImagePlugin._model_health_text(health[MODEL])
        self.assertIn("⚠502", text)
        self.assertIn("生成失败（请求HTTP200）", text)
        self.assertNotIn("✅", text)

    def test_empty_image_stream_is_not_healthy(self):
        page = self.page(responses=[{"status": 200, "text": "", "headers": {}}])
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(self.client(page).complete(model=MODEL, prompt="a circle"))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(arena_direct._HEALTH["models"][MODEL]["status_code"], 502)

    def test_returned_image_wins_over_a_stream_warning(self):
        response = reply()
        response["text"] += "\na3:" + json.dumps(PROVIDER_ERROR)
        page = self.page(responses=[response])
        result = self.run_async(self.client(page).complete(model=MODEL, prompt="a circle"))
        self.assertTrue(bridge_client.image_urls(result))
        self.assertEqual(arena_direct._HEALTH["models"][MODEL]["status_code"], 200)
        self.assertFalse(arena_direct._variant_failed_recently(self.rows[0]["id"]))
        self.assertEqual(len(page.requests), 1)

    def test_moderation_answer_does_not_mark_the_variant_broken(self):
        page = self.page(responses=[reply("Request blocked by content moderation.")])
        result = self.run_async(self.client(page).complete(model=MODEL, prompt="a circle"))
        self.assertEqual(bridge_client.image_urls(result), [])
        self.assertEqual(arena_direct._HEALTH["models"][MODEL]["status_code"], 200)
        self.assertFalse(arena_direct._variant_failed_recently(self.rows[0]["id"]))
        self.assertEqual(len(page.requests), 1)

    def test_rate_limit_after_fallback_stops_and_does_not_blacklist_the_second_id(self):
        page = self.page(
            responses=[
                reply(PROVIDER_ERROR),
                reply("Too many requests", 429, {"Retry-After": "300"}),
            ]
        )
        client = self.client(page, rate_limit_retries=2, rate_limit_max_wait=30)
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(client.complete(model=MODEL, prompt="a circle"))
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after, 300)
        self.assertEqual(len(page.requests), 2)
        self.assertTrue(arena_direct._variant_failed_recently(self.rows[0]["id"]))
        self.assertFalse(arena_direct._variant_failed_recently(self.rows[1]["id"]))
        self.sleep.assert_awaited_once_with(2.0)

    def test_a_long_retry_after_is_not_shortened(self):
        page = self.page(responses=[reply("Too many requests", 429, {"Retry-After": "120"})])
        with self.assertRaises(bridge_client.BridgeError):
            self.run_async(
                self.client(page, rate_limit_retries=2, rate_limit_max_wait=30).complete(
                    model=MODEL, prompt="a circle"
                )
            )
        self.assertEqual(len(page.requests), 1)
        self.sleep.assert_not_awaited()

    def test_streamed_rate_limit_keeps_the_same_model_variant_on_retry(self):
        page = self.page(responses=[reply("Too many requests"), reply()])
        result = self.run_async(
            self.client(page, rate_limit_retries=1).complete(model=MODEL, prompt="a circle")
        )
        self.assertEqual(
            [json.loads(request["body"])["modelAId"] for request in page.requests],
            [self.rows[0]["id"]] * 2,
        )
        self.assertEqual(result["arena_model_id"], self.rows[0]["id"])

    def test_session_failure_does_not_mark_the_variant_broken(self):
        page = self.page(responses=[reply("Arena auth token has expired", 401)])
        with self.assertRaises(bridge_client.BridgeError):
            self.run_async(self.client(page).complete(model=MODEL, prompt="a circle"))
        self.assertFalse(arena_direct._variant_failed_recently(self.rows[0]["id"]))
        self.assertEqual(len(page.requests), 1)

    def test_persisted_fake_success_is_repaired_without_inventing_a_variant_id(self):
        path = self.data_dir / "arena_model_health.json"
        now = time.time()
        path.write_text(
            json.dumps(
                {
                    "models": {
                        MODEL: {
                            "id": MODEL,
                            "status_code": 200,
                            "message": PROVIDER_ERROR[:200],
                            "checked_at": now,
                        },
                        "healthy": {
                            "id": "healthy",
                            "status_code": 200,
                            "message": "",
                            "checked_at": now,
                        },
                        "refusal": {
                            "id": "refusal",
                            "status_code": 200,
                            "message": "content moderation",
                            "checked_at": now,
                        },
                    },
                    "variants": {
                        "limited": {"id": "limited", "status_code": 429, "checked_at": now},
                    },
                }
            ),
            encoding="utf-8",
        )
        snapshot = self.run_async(self.client().model_health())
        by_id = {row["id"]: row for row in snapshot["models"]}
        self.assertEqual(by_id[MODEL]["status_code"], 400)
        self.assertEqual(by_id[MODEL]["error_code"], "provider_model_unavailable")
        self.assertEqual(by_id["healthy"]["status_code"], 200)
        self.assertEqual(by_id["refusal"]["status_code"], 200)
        self.assertEqual(snapshot["variants"], [])
        # The old file lacked the selected UUID: do not guess which row failed.
        self.assertNotIn("arena_model_id", by_id[MODEL])

    def test_health_survives_reload_and_preserves_same_name_variant_avoidance(self):
        page = self.page(variants=1, responses=[reply(PROVIDER_ERROR)])
        with self.assertRaises(bridge_client.BridgeError):
            self.run_async(self.client(page).complete(model=MODEL, prompt="a circle"))
        arena_direct._HEALTH = {"models": {}, "variants": {}}
        arena_direct._HEALTH_PATH = None
        arena_direct._load_health(self.data_dir / "arena_model_health.json")
        self.assertTrue(arena_direct._variant_failed_recently(self.rows[0]["id"]))
        self.assertEqual(arena_direct._HEALTH["models"][MODEL]["status_code"], 400)

    def test_error_status_parsing_does_not_treat_model_numbers_as_http_codes(self):
        cases = [
            ("HTTP 503", 503),
            ("Failed to fetch image: 400 Bad Request", 400),
            ("model checkpoint-400 produced no images", 502),
            ("Content moderation blocked request", 200),
            ("", 200),
            ("Rate limit exceeded", 429),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(arena_direct._stream_error_status(200, error), expected)

    def test_model_list_explains_provider_failure_without_renaming_public_model(self):
        main, plugin = _make_plugin(self, {"model_health_cache_seconds": 0})
        plugin._client = lambda: type(
            "Client",
            (),
            {
                "model_health": AsyncMock(
                    return_value={
                        "models": [
                            {
                                "id": MODEL,
                                "status_code": 400,
                                "checked_at": time.time(),
                                "error_code": "provider_model_unavailable",
                                "provider_endpoint": ENDPOINT,
                            }
                        ]
                    }
                ),
            },
        )()
        health = self.run_async(plugin._fetch_model_health(force=True))
        self.assertEqual(health[MODEL]["provider_endpoint"], ENDPOINT)
        text = main.ArenaImagePlugin._model_health_text(health[MODEL])
        self.assertIn("⚠400 上游端点失效", text)
        self.assertNotIn("✅", text)
        self.assertNotIn(ENDPOINT, health)
