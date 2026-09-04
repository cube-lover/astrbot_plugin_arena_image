"""Regression tests for the bridge-free direct-CDP transport.

The invariants locked here are the ones whose failure would either lose a
feature that the bridge already had, or resurrect the "Cookie 验证失败" report:
Chrome stores the ~4.6 KB ``arena-auth-prod-v1`` cookie in numbered chunks, and
an exact-name lookup silently downgrades a signed-in browser to anonymous.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from astrbot_plugin_arena_image import arena_direct, bridge_client

from novnc_gateway import _verify_token
from tests.test_arena_image_hardening import _make_plugin

PLUGIN_ROOT = Path(arena_direct.__file__).resolve().parent


def _segment(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwt(payload: dict) -> str:
    return f"{_segment({'alg': 'HS256', 'typ': 'JWT'})}.{_segment(payload)}.{'s' * 43}"


def _session_token(*, expires_in: int = 3600, anonymous: bool = False) -> str:
    """A realistic ``base64-<json>`` Supabase envelope, signed-in or anonymous."""
    expiry = int(time.time()) + int(expires_in)
    claims: dict = {"exp": expiry, "is_anonymous": anonymous}
    user: dict = {"is_anonymous": anonymous}
    if not anonymous:
        claims["email"] = "operator@example.com"
        claims["app_metadata"] = {"provider": "google", "providers": ["google"]}
        user["email"] = "operator@example.com"
    envelope = {
        "access_token": _jwt(claims),
        "refresh_token": "r" * 32,
        "expires_at": expiry,
        "user": user,
    }
    body = base64.b64encode(
        json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return f"base64-{body}"


def _chunked_cookies(token: str, *, chunks: int = 2) -> list[dict]:
    """Split like Chrome does: numbered chunks only, bare name absent."""
    body = token[len("base64-") :]
    size = max(1, (len(body) + chunks - 1) // chunks)
    parts = [body[index : index + size] for index in range(0, len(body), size)]
    cookies = [
        {"name": f"arena-auth-prod-v1.{index}", "value": value, "domain": ".arena.ai"}
        for index, value in enumerate(parts)
    ]
    cookies.append({"name": "cf_clearance", "value": "cf" * 20, "domain": ".arena.ai"})
    return cookies


def _uuid7(created_at: float) -> str:
    millis = int(created_at * 1000)
    raw = bytearray(millis.to_bytes(6, "big") + bytes(range(10)))
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _image_model(public_name: str, model_id: str, **extra) -> dict:
    model = {
        "id": model_id,
        "publicName": public_name,
        "organization": "openai",
        "capabilities": {"outputCapabilities": {"image": True}},
    }
    model.update(extra)
    return model


def _filler_models(count: int = 12) -> list[dict]:
    return [
        _image_model(f"filler-{index}", _uuid7(time.time() - 86400 * (index + 1)))
        for index in range(count)
    ]


class FakePage:
    """Stands in for ``ArenaPage``: same seven methods, no browser."""

    def __init__(
        self,
        *,
        cookies: list[dict] | None = None,
        models: list[dict] | None = None,
        response: dict | None = None,
        url: str = "https://arena.ai/text/direct",
    ) -> None:
        self.url = url
        self._cookies = list(cookies or [])
        self._models = list(models or [])
        self._response = response or {"status": 200, "headers": {}, "text": ""}
        self.actions: list[str] = []
        self.requests: list[dict] = []
        self.cookie_reads: list[bool] = []
        self.table_reads = 0

    async def cookies(self, *, refresh: bool = False) -> list[dict]:
        self.cookie_reads.append(bool(refresh))
        return list(self._cookies)

    async def auth_token(self, *, refresh: bool = False) -> str:
        self.cookie_reads.append(bool(refresh))
        return arena_direct.combine_auth_cookie(self._cookies)

    async def mint_recaptcha(self, action: str) -> str:
        self.actions.append(action)
        return f"token-for-{action}"

    async def fetch(self, path, *, method="POST", headers=None, body=None, timeout=None):
        self.requests.append(
            {
                "path": path,
                "method": method,
                "body": body,
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        return dict(self._response)

    async def model_table(self) -> list[dict]:
        self.table_reads += 1
        return list(self._models)

    async def next_actions(self) -> dict[str, str]:
        return {"generateUploadUrl": "UPLOAD_ID", "getSignedUrl": "SIGNED_ID"}


class DirectTransportCase(unittest.TestCase):
    """Isolates the module-level caches so tests cannot leak into each other."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_dir = Path(temp.name)
        # A fresh lock per test: every test drives its own `asyncio.run` loop and
        # an `asyncio.Lock` refuses to be shared across event loops.
        arena_direct._LOCK = asyncio.Lock()
        arena_direct._MODELS = []
        arena_direct._MODELS_AT = 0.0
        arena_direct._ACTIONS = {}
        arena_direct._ACTIONS_AT = 0.0
        arena_direct._UPLOADS.clear()
        arena_direct._SESSIONS.clear()
        arena_direct._HEALTH = {"models": {}, "variants": {}}
        arena_direct._HEALTH_PATH = None

    def run_async(self, coro):
        return asyncio.run(coro)

    def client(self, page: FakePage | None = None, **kwargs):
        """A client whose `open_page` yields `page` instead of a real browser."""
        options = {
            "gateway_url": "http://HOST:6081",
            "link_secret": "link-secret",
            "vnc_url": "http://HOST:6082/vnc.html",
            "data_dir": self.data_dir,
        }
        options.update(kwargs)
        client = arena_direct.ArenaDirectClient("http://arena-browser:9223", **options)
        if page is not None:
            @contextlib.asynccontextmanager
            async def _fake_open_page(*_args, **_kwargs):
                yield page

            self._patch(arena_direct, "open_page", _fake_open_page)
        return client

    def _patch(self, target, name: str, value) -> None:
        original = getattr(target, name)
        setattr(target, name, value)
        self.addCleanup(setattr, target, name, original)


class ChunkedAuthCookieTests(DirectTransportCase):
    """The exact failure that used to surface as "Cookie 验证失败"."""

    def test_chunked_cookie_still_reads_as_logged_in(self) -> None:
        token = _session_token()
        cookies = _chunked_cookies(token)
        names = {cookie["name"] for cookie in cookies}
        self.assertNotIn(arena_direct.AUTH_COOKIE_NAME, names)
        self.assertIn("arena-auth-prod-v1.0", names)
        self.assertIn("arena-auth-prod-v1.1", names)

        rebuilt = arena_direct.combine_auth_cookie(cookies)
        self.assertEqual(rebuilt, token)
        self.assertTrue(arena_direct.session_is_plausible(rebuilt))
        self.assertTrue(arena_direct.session_is_logged_in(rebuilt))
        self.assertFalse(arena_direct.session_is_expired(rebuilt))

        # An exact-name lookup is what produced the false "not logged in".
        self.assertEqual(
            arena_direct.cookie_value(cookies, arena_direct.AUTH_COOKIE_NAME), ""
        )

        state = self.run_async(arena_direct._live_state(FakePage(cookies=cookies)))
        self.assertTrue(state["has_arena_auth"])
        self.assertTrue(state["has_logged_in"])
        self.assertTrue(state["has_cf_clearance"])
        self.assertTrue(state["verified"])
        self.assertFalse(state["session_expired"])
        self.assertEqual(state["session_source"], "live-browser")
        self.assertEqual(state["recaptcha_action"], "chat_submit")

    def test_chunks_join_in_numeric_not_string_order(self) -> None:
        token = _session_token()
        cookies = _chunked_cookies(token, chunks=12)
        chunk_cookies = [
            cookie
            for cookie in cookies
            if cookie["name"].startswith("arena-auth-prod-v1.")
        ]
        indexes = sorted(int(c["name"].rsplit(".", 1)[-1]) for c in chunk_cookies)
        self.assertGreaterEqual(len(indexes), 11)
        self.assertEqual(indexes[-1], len(indexes) - 1)
        self.assertEqual(arena_direct.combine_auth_cookie(cookies), token)
        self.assertEqual(arena_direct.combine_auth_cookie(list(reversed(cookies))), token)

        # Once a tenth chunk exists, ".10" sorts before ".2" as a string, which
        # would corrupt the value -- prove that ordering is not what is used.
        string_order = "base64-" + "".join(
            cookie["value"] for cookie in sorted(chunk_cookies, key=lambda c: c["name"])
        )
        self.assertNotEqual(string_order, token)

    def test_prefix_is_restored_when_only_chunks_exist(self) -> None:
        token = _session_token()
        cookies = _chunked_cookies(token)
        self.assertFalse(cookies[0]["value"].startswith("base64-"))
        self.assertTrue(
            arena_direct.combine_auth_cookie(cookies).startswith("base64-")
        )

    def test_anonymous_session_downgrades_recaptcha_action(self) -> None:
        cookies = _chunked_cookies(_session_token(anonymous=True))
        state = self.run_async(arena_direct._live_state(FakePage(cookies=cookies)))
        self.assertTrue(state["has_arena_auth"])
        self.assertFalse(state["has_logged_in"])
        self.assertFalse(state["verified"])
        self.assertEqual(state["recaptcha_action"], "sign_up")


class StreamParsingTests(DirectTransportCase):
    IMAGE = "https://cdn.arena.ai/generated/apple.png"

    def test_sse_prefixes_and_error_line(self) -> None:
        body = "\n".join(
            [
                'ag:"thinking"',
                'data: a0:"here you go"',
                f'data: a2:[{{"type":"image","image":"{self.IMAGE}"}}]',
                f'a2:[{{"type":"image","image":"{self.IMAGE}"}}]',
                'ac:[{"title":"cite"}]',
                'a3:"content moderation blocked"',
                'ad:{"finishReason":"stop"}',
                "",
            ]
        )
        stream = arena_direct.parse_stream(body)
        self.assertEqual(stream["images"], [self.IMAGE])
        self.assertEqual(stream["text"], "here you go")
        self.assertEqual(stream["error"], "content moderation blocked")
        self.assertEqual(stream["finish_reason"], "stop")

    def test_payload_survives_bridge_image_extraction(self) -> None:
        stream = {"images": [self.IMAGE], "text": "", "finish_reason": "stop"}
        payload = arena_direct.ArenaDirectClient._openai_payload(
            "gpt-image-2 (medium)", "model-id", stream
        )
        self.assertEqual(bridge_client.image_urls(payload), [self.IMAGE])
        message = payload["choices"][0]["message"]
        self.assertIn(f"![Generated Image]({self.IMAGE})", message["content"])
        self.assertEqual(message["images"], [self.IMAGE])
        self.assertEqual(payload["model"], "gpt-image-2 (medium)")
        self.assertEqual(payload["arena_model_id"], "model-id")
        self.assertEqual(payload["object"], "chat.completion")

    def test_data_uri_image_is_kept(self) -> None:
        data_uri = "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode()
        stream = arena_direct.parse_stream(f'a2:[{{"type":"image","image":"{data_uri}"}}]')
        self.assertEqual(stream["images"], [data_uri])
        payload = arena_direct.ArenaDirectClient._openai_payload("m", "id", stream)
        self.assertEqual(bridge_client.image_urls(payload), [data_uri])


class ModelSelectionTests(DirectTransportCase):
    def test_keep_model_matches_bridge_filter(self) -> None:
        stealthy = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223", allow_stealth_models=True
        )
        strict = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223", allow_stealth_models=False
        )
        cases = [
            ("plain image row", _image_model("gpt-image-2 (medium)", "a"), True, True),
            (
                "explicitly unselectable",
                _image_model("x", "b", userSelectable=False),
                False,
                False,
            ),
            (
                # Text rows stay in, exactly like the bridge's /api/v1/models:
                # `main.py` is what narrows the list down to image models.
                "text-only output row",
                {"id": "c", "publicName": "y", "organization": "openai",
                 "capabilities": {"outputCapabilities": {"text": True}}},
                True,
                True,
            ),
            (
                "no supported output at all",
                {"id": "c2", "publicName": "y2", "organization": "openai",
                 "capabilities": {"outputCapabilities": {"audio": True}}},
                False,
                False,
            ),
            (
                "stealth row without organization",
                _image_model("mystery-alpha", "d", organization=""),
                True,
                False,
            ),
            (
                "allowlisted stealth row",
                _image_model("luna-lisa-alpha", "e", organization=""),
                True,
                True,
            ),
            ("not a dict", "nope", False, False),
        ]
        for label, model, when_open, when_strict in cases:
            with self.subTest(label):
                self.assertIs(stealthy._keep_model(model), when_open)
                self.assertIs(strict._keep_model(model), when_strict)

    def test_pick_variant_prefers_newest_then_avoids_recent_failure(self) -> None:
        now = time.time()
        newest = _image_model("m", _uuid7(now - 3600))
        older = _image_model("m", _uuid7(now - 30 * 86400))
        rows = [older, newest]
        self.assertEqual(
            arena_direct.ArenaDirectClient._pick_variant(rows)["id"], newest["id"]
        )

        arena_direct._record_health("m", newest["id"], 500, "internal error")
        self.assertTrue(arena_direct._variant_failed_recently(newest["id"]))
        self.assertEqual(
            arena_direct.ArenaDirectClient._pick_variant(rows)["id"], older["id"]
        )

        # A stale failure record stops counting once the window passes.
        arena_direct._HEALTH["variants"][newest["id"]]["checked_at"] = (
            now - arena_direct.MODEL_VARIANT_FAILURE_TTL_SECONDS - 60
        )
        self.assertFalse(arena_direct._variant_failed_recently(newest["id"]))
        self.assertEqual(
            arena_direct.ArenaDirectClient._pick_variant(rows)["id"], newest["id"]
        )

    def test_uuid7_created_at_ignores_uuid4_rows(self) -> None:
        self.assertIsNone(arena_direct.uuid7_created_at(str(uuid.uuid4())))
        self.assertIsNone(arena_direct.uuid7_created_at("not-a-uuid"))
        self.assertIsNone(arena_direct.uuid7_created_at(None))
        stamp = arena_direct.uuid7_created_at(_uuid7(1_700_000_000.0))
        self.assertEqual(stamp, 1_700_000_000)

    def test_list_models_returns_row_level_entries(self) -> None:
        models = _filler_models() + [
            _image_model("gpt-image-2 (medium)", _uuid7(time.time() - 60)),
            _image_model("gpt-image-2 (medium)", _uuid7(time.time() - 8 * 86400)),
            _image_model("hidden", _uuid7(time.time()), userSelectable=False),
        ]
        page = FakePage(models=models)
        rows = self.run_async(self.client(page).list_models())
        names = [row.get("id") for row in rows]
        # Row level, exactly like the bridge: `main.py` keeps the newest row per
        # public name itself, so both checkpoints must survive this call.
        self.assertEqual(names.count("gpt-image-2 (medium)"), 2)
        self.assertNotIn("hidden", names)
        picked = [row for row in rows if row["id"] == "gpt-image-2 (medium)"]
        self.assertTrue(all(row["output_image"] for row in picked))
        self.assertTrue(all(isinstance(row["created_at"], int) for row in picked))
        self.assertEqual(
            bridge_client.model_created_at(max(picked, key=lambda r: r["created_at"])),
            max(row["created_at"] for row in picked),
        )

    def test_truncated_table_never_replaces_a_good_one(self) -> None:
        good = FakePage(models=_filler_models())
        client = self.client(good)
        self.assertEqual(len(self.run_async(client.list_models())), 12)

        arena_direct._MODELS_AT = 0.0  # force a refetch, keep the cached rows
        truncated = FakePage(models=_filler_models(3))
        client = self.client(truncated)
        self.assertEqual(len(self.run_async(client.list_models())), 12)

    def test_empty_table_without_cache_is_reported(self) -> None:
        client = self.client(FakePage(models=_filler_models(2)))
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(client.list_models())
        self.assertEqual(caught.exception.code, "model_table_empty")


class SessionGateTests(DirectTransportCase):
    def _code_for(self, cookies: list[dict]) -> str:
        client = self.client(FakePage(cookies=cookies))
        state = self.run_async(arena_direct._live_state(FakePage(cookies=cookies)))
        with self.assertRaises(bridge_client.BridgeError) as caught:
            client._require_session(state, "http://HOST:6081/v/token")
        self.assertTrue(caught.exception.requires_interactive_auth)
        return caught.exception.code

    def test_missing_cookie_says_missing(self) -> None:
        code = self._code_for([{"name": "cf_clearance", "value": "cf"}])
        self.assertEqual(code, "arena_auth_required")

    def test_expired_cookie_says_expired_not_missing(self) -> None:
        # An expired token is not "plausible", so the missing-cookie branch would
        # blame a cookie that is present -- the wording operators complained about.
        cookies = _chunked_cookies(_session_token(expires_in=-600))
        self.assertEqual(self._code_for(cookies), "arena_auth_expired")

    def test_anonymous_session_says_login_required(self) -> None:
        cookies = _chunked_cookies(_session_token(anonymous=True))
        self.assertEqual(self._code_for(cookies), "arena_login_required")

    def test_logged_in_session_passes_the_gate(self) -> None:
        cookies = _chunked_cookies(_session_token())
        client = self.client(FakePage(cookies=cookies))
        state = self.run_async(arena_direct._live_state(FakePage(cookies=cookies)))
        self.assertIsNone(client._require_session(state, ""))

    def test_health_reports_the_live_verdict(self) -> None:
        cookies = _chunked_cookies(_session_token())
        payload = self.run_async(self.client(FakePage(cookies=cookies)).health())
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["transport"], "direct-cdp")
        self.assertTrue(payload["has_logged_in"])
        self.assertEqual(payload["recaptcha_action"], "chat_submit")

        anonymous = _chunked_cookies(_session_token(anonymous=True))
        degraded = self.run_async(self.client(FakePage(cookies=anonymous)).health())
        self.assertEqual(degraded["status"], "degraded")


class GenerationTests(DirectTransportCase):
    MODEL = "gpt-image-2 (medium)"
    IMAGE = "https://cdn.arena.ai/generated/apple.png"
    SUCCESS_BODY = (
        'a2:[{"type":"image","image":"https://cdn.arena.ai/generated/apple.png"}]\n'
        'ad:{"finishReason":"stop"}'
    )

    def _page(self, *, status: int = 200, body: str | None = None, headers=None) -> FakePage:
        now = time.time()
        self.newest = _image_model(self.MODEL, _uuid7(now - 120))
        self.older = _image_model(self.MODEL, _uuid7(now - 20 * 86400))
        return FakePage(
            cookies=_chunked_cookies(_session_token()),
            models=_filler_models() + [self.older, self.newest],
            response={
                "status": status,
                "headers": headers or {},
                "text": self.SUCCESS_BODY if body is None else body,
            },
        )

    def test_end_to_end_generation_without_a_bridge(self) -> None:
        page = self._page()
        result = self.run_async(
            self.client(page).complete(model=self.MODEL, prompt="a red apple")
        )
        self.assertEqual(bridge_client.image_urls(result), [self.IMAGE])
        self.assertEqual(result["model"], self.MODEL)
        self.assertEqual(result["arena_model_id"], self.newest["id"])
        self.assertEqual(page.actions, ["chat_submit"])

        self.assertEqual(len(page.requests), 1)
        request = page.requests[0]
        self.assertEqual(request["path"], arena_direct.STREAM_CREATE_EVALUATION_PATH)
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["timeout"], 300.0)
        # The session verdict is always re-read from the live browser, never
        # served from a cached copy that Chrome has already rotated.
        self.assertIn(True, page.cookie_reads)
        sent = json.loads(request["body"])
        self.assertEqual(sent["mode"], "direct-battle")
        self.assertEqual(sent["modality"], "image")
        self.assertEqual(sent["modelAId"], self.newest["id"])
        self.assertEqual(sent["recaptchaV3Token"], "token-for-chat_submit")
        self.assertEqual(sent["userMessage"]["content"], "a red apple")
        self.assertEqual(sent["userMessage"]["experimental_attachments"], [])
        ids = [
            sent["id"],
            sent["userMessageId"],
            sent["modelAMessageId"],
            sent["modelBMessageId"],
        ]
        self.assertEqual(len(set(ids)), 4)
        for value in ids:
            self.assertEqual(uuid.UUID(value).version, 7)

        health = self.run_async(self.client(page).model_health())
        rows = {row["id"]: row for row in health["models"]}
        self.assertEqual(rows[self.MODEL]["status_code"], 200)
        self.assertEqual(health["variants"], [])

    def test_moderation_refusal_is_reported_as_text_not_as_auth_failure(self) -> None:
        body = 'a3:"Your request was blocked by our content moderation system."'
        page = self._page(body=body)
        result = self.run_async(
            self.client(page).complete(model=self.MODEL, prompt="something refused")
        )
        self.assertEqual(bridge_client.image_urls(result), [])
        self.assertIn("moderation", result["choices"][0]["message"]["content"])
        # A refusal is the model answering, so the checkpoint must not be blamed.
        self.assertFalse(arena_direct._variant_failed_recently(self.newest["id"]))

    def test_server_error_marks_the_variant_and_falls_back_next_time(self) -> None:
        page = self._page(status=500, body='a3:"internal server error"')
        newest_id, older_id = self.newest["id"], self.older["id"]
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(self.client(page).complete(model=self.MODEL, prompt="a red apple"))
        self.assertEqual(caught.exception.code, "http_500")
        self.assertTrue(arena_direct._variant_failed_recently(newest_id))

        healthy = FakePage(
            cookies=_chunked_cookies(_session_token()),
            models=list(page._models),
            response={"status": 200, "headers": {}, "text": self.SUCCESS_BODY},
        )
        result = self.run_async(
            self.client(healthy).complete(model=self.MODEL, prompt="a red apple")
        )
        self.assertEqual(json.loads(healthy.requests[0]["body"])["modelAId"], older_id)
        self.assertEqual(bridge_client.image_urls(result), [self.IMAGE])
        self.assertEqual(result["arena_model_id"], older_id)

    def test_rate_limit_surfaces_retry_after(self) -> None:
        page = self._page(
            status=429, body='a3:"too many requests"', headers={"Retry-After": "12"}
        )
        client = self.client(page, rate_limit_retries=0)
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(client.complete(model=self.MODEL, prompt="a red apple"))
        self.assertTrue(caught.exception.is_rate_limited)
        self.assertEqual(caught.exception.retry_after, 12.0)

    def test_unknown_model_never_reaches_the_network(self) -> None:
        page = self._page()
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.run_async(self.client(page).complete(model="no-such-model", prompt="x"))
        self.assertEqual(caught.exception.code, "model_not_found")
        self.assertEqual(page.requests, [])


class InteractiveAuthTests(DirectTransportCase):
    def test_signed_link_validates_in_the_gateway_and_hides_the_secret(self) -> None:
        url = arena_direct.build_interactive_link(
            "http://HOST:6081/", expires_at=time.time() + 900, secret="link-secret"
        )
        self.assertTrue(url.startswith("http://HOST:6081/v/"))
        self.assertNotIn("link-secret", url)
        self.assertNotIn("password", url)
        token = url.rsplit("/", 1)[-1]
        self.assertTrue(_verify_token(token, "link-secret"))
        self.assertFalse(_verify_token(token, "other-secret"))

    def test_expired_link_is_rejected_by_the_gateway(self) -> None:
        url = arena_direct.build_interactive_link(
            "http://HOST:6081", expires_at=time.time() - 5, secret="link-secret"
        )
        self.assertFalse(_verify_token(url.rsplit("/", 1)[-1], "link-secret"))

    def test_link_falls_back_to_vnc_url_without_a_secret(self) -> None:
        self.assertEqual(arena_direct.build_interactive_link("http://HOST:6081", expires_at=1, secret=""), "")
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223",
            gateway_url="http://HOST:6081",
            vnc_url="http://HOST:6082/vnc.html",
            link_secret="",
        )
        self.assertEqual(client._link(), "http://HOST:6082/vnc.html")

    def test_unknown_session_id_reports_the_live_verdict(self) -> None:
        page = FakePage(cookies=_chunked_cookies(_session_token()))
        payload = self.run_async(
            self.client(page).interactive_auth_status("session-that-was-never-stored")
        )
        # A plugin reload used to strand the operator on "unknown session".
        self.assertEqual(payload["session_id"], "session-that-was-never-stored")
        self.assertEqual(payload["status"], "verified")
        self.assertTrue(payload["verified"])
        self.assertTrue(payload["has_logged_in"])
        self.assertTrue(payload["browser_url"].startswith("http://HOST:6081/v/"))
        self.assertIn("已登录", payload["message"])

    def test_status_without_any_session_still_answers(self) -> None:
        page = FakePage(cookies=_chunked_cookies(_session_token(anonymous=True)))
        payload = self.run_async(self.client(page).latest_interactive_auth_status())
        self.assertEqual(payload["status"], "waiting")
        self.assertFalse(payload["verified"])
        self.assertTrue(payload["has_arena_auth"])
        self.assertIn("匿名", payload["message"])

    def test_empty_session_id_is_still_a_hard_error(self) -> None:
        client = self.client(FakePage(cookies=[]))
        with self.assertRaises(bridge_client.BridgeError):
            self.run_async(client.interactive_auth_status(""))


class TransportDispatchTests(DirectTransportCase):
    """Rollback layer 1: the operator flips one config key, no Docker change."""

    def test_default_configuration_still_uses_the_bridge(self) -> None:
        main, plugin = _make_plugin(self, {})
        self.assertEqual(plugin._transport_mode(), "bridge")
        client = plugin._client()
        self.assertIsInstance(client, main.ArenaBridgeClient)
        self.assertNotIsInstance(client, arena_direct.ArenaDirectClient)

    def test_direct_mode_builds_the_direct_client(self) -> None:
        main, plugin = _make_plugin(
            self,
            {
                "transport_mode": "direct",
                "browser_cdp_url": "http://arena-browser:9223",
                "browser_gateway_url": "http://HOST:6081",
                "browser_vnc_url": "http://HOST:6082/vnc.html",
                "interactive_link_secret": "link-secret",
                "interactive_link_ttl": 900,
                "request_timeout": 240,
                "rate_limit_retries": 3,
            },
        )
        self.assertIs(main.ArenaDirectClient, arena_direct.ArenaDirectClient)
        client = plugin._client()
        self.assertIsInstance(client, arena_direct.ArenaDirectClient)
        self.assertEqual(client.cdp_url, "http://arena-browser:9223")
        self.assertEqual(client.gateway_url, "http://HOST:6081")
        self.assertEqual(client.timeout, 240.0)
        self.assertEqual(client.rate_limit_retries, 3)
        self.assertTrue(client.allow_stealth_models)
        link = client._link()
        self.assertTrue(link.startswith("http://HOST:6081/v/"))
        self.assertTrue(_verify_token(link.rsplit("/", 1)[-1], "link-secret"))

    def test_unknown_transport_value_falls_back_to_the_bridge(self) -> None:
        for value in ("", "Bridge", "nonsense", None, "DIRECT "):
            with self.subTest(value=value):
                main, plugin = _make_plugin(self, {"transport_mode": value})
                expected = "direct" if str(value or "").strip().casefold() == "direct" else "bridge"
                self.assertEqual(plugin._transport_mode(), expected)

    def test_schema_documents_every_direct_key(self) -> None:
        schema = json.loads(
            (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig")
        )
        self.assertEqual(schema["transport_mode"]["default"], "bridge")
        self.assertEqual(schema["transport_mode"]["options"], ["bridge", "direct"])
        for key in (
            "browser_cdp_url",
            "browser_gateway_url",
            "browser_vnc_url",
            "interactive_link_secret",
            "interactive_link_ttl",
            "allow_stealth_models",
        ):
            with self.subTest(key=key):
                self.assertIn(key, schema)
        self.assertTrue(schema["interactive_link_secret"].get("secret"))
        self.assertEqual(schema["browser_cdp_url"]["default"], "http://arena-browser:9223")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()











