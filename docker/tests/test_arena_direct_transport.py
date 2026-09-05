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
import zlib
from pathlib import Path
from types import SimpleNamespace

import httpx

from astrbot_plugin_arena_image import arena_direct, bridge_client

from novnc_gateway import _verify_token
from tests.test_arena_image_hardening import _make_plugin

PLUGIN_ROOT = Path(arena_direct.__file__).resolve().parent


def _segment(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwt(payload: dict) -> str:
    return f"{_segment({'alg': 'HS256', 'typ': 'JWT'})}.{_segment(payload)}.{'s' * 43}"


def _session_token(
    *, expires_in: int = 3600, anonymous: bool = False, refresh: bool = True
) -> str:
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
        "expires_at": expiry,
        "user": user,
    }
    if refresh:
        envelope["refresh_token"] = "r" * 32
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
        refreshed_cookies: list[dict] | None = None,
        responses: list[dict] | None = None,
        action_sets: list[dict] | None = None,
    ) -> None:
        self.url = url
        self._cookies = list(cookies or [])
        self._refreshed_cookies = (
            None if refreshed_cookies is None else list(refreshed_cookies)
        )
        self._models = list(models or [])
        self._response = response or {"status": 200, "headers": {}, "text": ""}
        # A queue of scripted replies, for flows that make several requests.
        self._responses = None if responses is None else list(responses)
        self._action_sets = None if action_sets is None else list(action_sets)
        self.actions: list[str] = []
        self.requests: list[dict] = []
        self.cookie_reads: list[bool] = []
        self.table_reads = 0
        self.refresh_calls = 0
        self.reload_calls = 0
        self.action_reads = 0

    async def cookies(self, *, refresh: bool = False) -> list[dict]:
        self.cookie_reads.append(bool(refresh))
        return list(self._cookies)

    async def refresh_session(self, *, budget: float | None = None) -> str:
        """Rotate to ``refreshed_cookies`` if the test supplied any."""
        self.refresh_calls += 1
        if self._refreshed_cookies is not None:
            self._cookies = list(self._refreshed_cookies)
        return arena_direct.combine_auth_cookie(self._cookies)

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
        return dict(self._responses.pop(0)) if self._responses else dict(self._response)

    async def reload(self, *, budget: float | None = None) -> bool:
        self.reload_calls += 1
        return True

    async def model_table(self) -> list[dict]:
        self.table_reads += 1
        return list(self._models)

    async def next_actions(self) -> dict[str, str]:
        self.action_reads += 1
        if self._action_sets:
            return dict(self._action_sets.pop(0))
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
        arena_direct._LINK_SECRETS.clear()
        arena_direct._GATEWAY_URLS.clear()
        arena_direct._CDP_ENDPOINTS.clear()

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
        # No refresh token means the login really is gone, not merely stale.
        cookies = _chunked_cookies(_session_token(expires_in=-600, refresh=False))
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


class StaleSessionTests(DirectTransportCase):
    """An hour-old access token is not a logout: the page rotates it itself.

    This is the shape of the complaint that keeps coming back -- the browser is
    still signed in, only the short-lived access token lapsed while the tab sat
    idle, and reporting "Cookie 失效，请重新绑定" sends the operator to redo a
    login that was never lost.
    """

    def test_a_stale_token_is_recognised_as_recoverable(self) -> None:
        stale = _session_token(expires_in=-600)
        self.assertTrue(arena_direct.session_is_expired(stale))
        self.assertTrue(arena_direct.session_can_refresh(stale))

    def test_a_logged_out_session_is_not_recoverable(self) -> None:
        # No refresh token beside the expired one: nothing to rotate with.
        self.assertFalse(
            arena_direct.session_can_refresh(
                _session_token(expires_in=-600, refresh=False)
            )
        )

    def test_an_anonymous_session_is_not_recoverable(self) -> None:
        # Anonymous Supabase sessions carry a refresh token too, so the shape
        # alone must not be mistaken for a login.
        self.assertFalse(
            arena_direct.session_can_refresh(
                _session_token(expires_in=-600, anonymous=True)
            )
        )

    def test_a_missing_or_opaque_token_is_not_recoverable(self) -> None:
        for value in ("", "not-a-token", "base64-###"):
            self.assertFalse(arena_direct.session_can_refresh(value))

    def test_live_state_rotates_a_stale_token_before_judging(self) -> None:
        page = FakePage(
            cookies=_chunked_cookies(_session_token(expires_in=-600)),
            refreshed_cookies=_chunked_cookies(_session_token(expires_in=3600)),
        )
        state = self.run_async(arena_direct._live_state(page))
        self.assertEqual(page.refresh_calls, 1)
        self.assertTrue(state["has_logged_in"])
        self.assertTrue(state["has_arena_auth"])
        self.assertTrue(state["session_refreshed"])
        self.assertFalse(state["session_expired"])
        self.assertFalse(state["session_refreshable"])
        self.assertEqual(state["recaptcha_action"], "chat_submit")

    def test_a_fresh_token_is_never_rotated(self) -> None:
        page = FakePage(cookies=_chunked_cookies(_session_token()))
        state = self.run_async(arena_direct._live_state(page))
        self.assertEqual(page.refresh_calls, 0)
        self.assertFalse(state["session_refreshed"])

    def test_a_logged_out_session_is_never_rotated(self) -> None:
        page = FakePage(
            cookies=_chunked_cookies(_session_token(expires_in=-600, refresh=False))
        )
        state = self.run_async(arena_direct._live_state(page))
        self.assertEqual(page.refresh_calls, 0)
        self.assertTrue(state["session_expired"])
        self.assertFalse(state["session_refreshable"])

    def test_a_failed_rotation_says_stale_not_logged_out(self) -> None:
        cookies = _chunked_cookies(_session_token(expires_in=-600))
        page = FakePage(cookies=cookies)  # refresh keeps returning the stale token
        state = self.run_async(arena_direct._live_state(page))
        self.assertEqual(page.refresh_calls, 1)
        self.assertTrue(state["session_refreshable"])
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.client(page)._require_session(state, "http://HOST:6081/v/token")
        self.assertEqual(caught.exception.code, "arena_auth_stale")
        self.assertIn("登录还在", str(caught.exception))
        self.assertNotIn("重新绑定 后重新登录", str(caught.exception))

    def test_health_explains_a_stale_token_without_crying_logout(self) -> None:
        cookies = _chunked_cookies(_session_token(expires_in=-600))
        payload = self.run_async(self.client(FakePage(cookies=cookies)).health())
        self.assertEqual(payload["status"], "degraded")
        self.assertTrue(payload["session_refreshable"])
        self.assertIn("登录还在", payload["message"])

    def test_a_rotated_session_generates_without_a_detour(self) -> None:
        page = FakePage(
            cookies=_chunked_cookies(_session_token(expires_in=-600)),
            refreshed_cookies=_chunked_cookies(_session_token(expires_in=3600)),
            models=_filler_models()
            + [_image_model("gpt-image-2 (medium)", _uuid7(time.time() - 60))],
            response={
                "status": 200,
                "headers": {},
                "text": (
                    'a2:[{"type":"image","image":"https://cdn.arena.ai/x.png"}]\n'
                    'ad:{"finishReason":"stop"}'
                ),
            },
        )
        payload = self.run_async(
            self.client(page).complete(model="gpt-image-2 (medium)", prompt="cat")
        )
        self.assertEqual(bridge_client.image_urls(payload), ["https://cdn.arena.ai/x.png"])
        # The rotated session must also fix the reCAPTCHA action: minting
        # `sign_up` on a signed-in account is what Arena answers 403 to.
        self.assertEqual(page.actions, ["chat_submit"])


class StatusWordingTests(unittest.TestCase):
    """What the operator actually reads in `/竞技场验证状态`."""

    def test_a_stale_token_reads_as_still_logged_in(self) -> None:
        _main, plugin = _make_plugin(self)
        text = plugin._format_verification_status(
            {
                "status": "waiting",
                "has_cf_clearance": True,
                "has_arena_auth": False,
                "has_logged_in": False,
                "session_refreshable": True,
                "session_source": "live-browser",
            }
        )
        self.assertIn("登录还在", text)
        self.assertIn("令牌待续期", text)
        self.assertNotIn("匿名无法出图", text)

    def test_a_real_logout_still_says_anonymous(self) -> None:
        _main, plugin = _make_plugin(self)
        text = plugin._format_verification_status(
            {
                "status": "waiting",
                "has_cf_clearance": True,
                "has_arena_auth": True,
                "has_logged_in": False,
            }
        )
        self.assertIn("匿名", text)
        self.assertNotIn("登录还在", text)


class RotatingCDP:
    """Just enough CDP for `ArenaPage.refresh_session` to be observable.

    ``reads_until_fresh`` models the real timing: the cookie is rewritten by
    Chrome a moment *after* the request that triggered the rotation returns, so
    the first read can still show the stale value.
    """

    def __init__(self, *, stale: list[dict], fresh: list[dict], reads_until_fresh: int = 1) -> None:
        self.stale = list(stale)
        self.fresh = list(fresh)
        self.reads_until_fresh = int(reads_until_fresh)
        self.activated: list[str] = []
        self.expressions: list[str] = []
        self.cookie_reads = 0
        self.woken = False

    async def command(self, method, params=None, *, session_id=None, timeout=None):
        params = dict(params or {})
        if method == "Target.activateTarget":
            self.activated.append(str(params.get("targetId") or ""))
            return {}
        if method in {"Network.getAllCookies", "Storage.getCookies"}:
            self.cookie_reads += 1
            if self.woken and self.cookie_reads > self.reads_until_fresh:
                return {"cookies": list(self.fresh)}
            return {"cookies": list(self.stale)}
        raise arena_direct.CDPError(f"unsupported in the fake: {method}")

    async def evaluate(self, expression, *, session_id: str, timeout=None):
        self.expressions.append(str(expression))
        self.woken = True
        return {"visible": "hidden", "status": 200}


class SessionRotationTests(DirectTransportCase):
    """How `ArenaPage.refresh_session` nudges the page, at the CDP level."""

    def _page(self, cdp: RotatingCDP, *, target_id: str = "arena-1"):
        return arena_direct.ArenaPage(
            cdp, "session-1", "https://arena.ai/text/direct", target_id=target_id
        )

    def _cdp(self, **kwargs) -> RotatingCDP:
        return RotatingCDP(
            stale=_chunked_cookies(_session_token(expires_in=-600)),
            fresh=_chunked_cookies(_session_token(expires_in=3600)),
            **kwargs,
        )

    def test_it_activates_the_tab_and_asks_the_page_for_a_rotation(self) -> None:
        self._patch(arena_direct, "SESSION_REFRESH_POLL_SECONDS", 0.01)
        cdp = self._cdp()
        token = self.run_async(self._page(cdp).refresh_session())
        self.assertEqual(cdp.activated, ["arena-1"])
        self.assertEqual(len(cdp.expressions), 1)
        # Activating restarts Supabase's timer; the GET goes through Arena's SSR
        # middleware.  Both matter, so both are locked.
        self.assertIn(arena_direct.ARENA_PAGE_PATH, cdp.expressions[0])
        self.assertIn("credentials", cdp.expressions[0])
        self.assertIn("visibilitychange", cdp.expressions[0])
        self.assertFalse(arena_direct.session_is_expired(token))

    def test_it_keeps_polling_until_chrome_has_rewritten_the_cookie(self) -> None:
        self._patch(arena_direct, "SESSION_REFRESH_POLL_SECONDS", 0.01)
        cdp = self._cdp(reads_until_fresh=3)
        token = self.run_async(self._page(cdp).refresh_session())
        self.assertGreater(cdp.cookie_reads, 3)
        self.assertFalse(arena_direct.session_is_expired(token))

    def test_it_gives_up_inside_the_budget_instead_of_hanging(self) -> None:
        self._patch(arena_direct, "SESSION_REFRESH_POLL_SECONDS", 0.01)
        cdp = self._cdp(reads_until_fresh=10**6)  # never rotates
        started = time.monotonic()
        token = self.run_async(self._page(cdp).refresh_session(budget=0.1))
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertTrue(arena_direct.session_is_expired(token))

    def test_a_tab_without_a_known_id_is_still_woken(self) -> None:
        self._patch(arena_direct, "SESSION_REFRESH_POLL_SECONDS", 0.01)
        cdp = self._cdp()
        token = self.run_async(self._page(cdp, target_id="").refresh_session())
        self.assertEqual(cdp.activated, [])
        self.assertEqual(len(cdp.expressions), 1)
        self.assertFalse(arena_direct.session_is_expired(token))

    def test_open_page_hands_over_the_target_id(self) -> None:
        # Without the id there is nothing to activate, and a hidden tab is
        # exactly the case that needs activating.
        cdp = FakeCDP()
        self._patch(arena_direct, "CDPWebSocket", lambda *_a, **_k: cdp)

        async def _run():
            async with arena_direct.open_page("http://arena-browser:9223") as page:
                return page.target_id

        self.assertEqual(self.run_async(_run()), "arena-1")


class ReloadingCDP:
    """Just enough CDP for `ArenaPage.reload` to be observable."""

    def __init__(self, *, refuse: str = "", ready_after: int = 1, failed_polls: int = 0) -> None:
        self.refuse = str(refuse)  # a method that this Chrome will not run
        self.ready_after = int(ready_after)  # polls before the document is done
        self.failed_polls = int(failed_polls)  # polls that die mid-navigation
        self.calls: list[dict] = []
        self.polls = 0

    async def command(self, method, params=None, *, session_id=None, timeout=None):
        self.calls.append(
            {"method": str(method), "params": dict(params or {}), "session": session_id}
        )
        if method == self.refuse:
            raise arena_direct.CDPError(f"refused: {method}")
        return {}

    async def evaluate(self, expression, *, session_id: str, timeout=None):
        self.polls += 1
        if self.polls <= self.failed_polls:
            # Evaluating against a document that is being swapped out is normal.
            raise arena_direct.CDPError("execution context destroyed")
        return "complete" if self.polls >= self.ready_after else "loading"


class PageReloadTests(DirectTransportCase):
    """How `ArenaPage.reload` re-fetches the bundle, at the CDP level."""

    def setUp(self) -> None:
        super().setUp()
        self._patch(arena_direct, "PAGE_RELOAD_POLL_SECONDS", 0.01)
        self._patch(arena_direct, "PAGE_RELOAD_SETTLE_SECONDS", 0.01)

    def _page(self, cdp: ReloadingCDP):
        return arena_direct.ArenaPage(
            cdp, "session-1", "https://arena.ai/text/direct", target_id="arena-1"
        )

    def test_it_reloads_the_tab_and_bypasses_the_cache(self) -> None:
        cdp = ReloadingCDP()
        self.assertTrue(self.run_async(self._page(cdp).reload()))
        methods = [call["method"] for call in cdp.calls]
        self.assertEqual(methods, ["Page.enable", "Page.reload"])
        reload_call = cdp.calls[-1]
        # A cached bundle would hand back the same dead action ids.
        self.assertIs(reload_call["params"].get("ignoreCache"), True)
        self.assertEqual(reload_call["session"], "session-1")

    def test_it_waits_for_the_document_before_claiming_success(self) -> None:
        cdp = ReloadingCDP(ready_after=3)
        self.assertTrue(self.run_async(self._page(cdp).reload()))
        self.assertGreaterEqual(cdp.polls, 3)

    def test_a_poll_that_dies_mid_navigation_is_not_a_failure(self) -> None:
        cdp = ReloadingCDP(failed_polls=2, ready_after=3)
        self.assertTrue(self.run_async(self._page(cdp).reload()))
        self.assertGreaterEqual(cdp.polls, 3)

    def test_a_refused_reload_is_reported_rather_than_raised(self) -> None:
        cdp = ReloadingCDP(refuse="Page.reload")
        self.assertFalse(self.run_async(self._page(cdp).reload()))
        self.assertEqual(cdp.polls, 0)

    def test_page_enable_being_unavailable_does_not_stop_the_reload(self) -> None:
        cdp = ReloadingCDP(refuse="Page.enable")
        self.assertTrue(self.run_async(self._page(cdp).reload()))
        self.assertEqual([call["method"] for call in cdp.calls], ["Page.enable", "Page.reload"])

    def test_a_document_that_never_completes_gives_up_inside_the_budget(self) -> None:
        cdp = ReloadingCDP(ready_after=10**6)
        started = time.monotonic()
        self.assertFalse(self.run_async(self._page(cdp).reload(budget=0.1)))
        self.assertLess(time.monotonic() - started, 5.0)


def _png_bytes() -> bytes:
    """A real 1x1 PNG, so the MIME sniffer agrees it is one."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return len(data).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")

    header = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def _png_data_url() -> str:
    return "data:image/png;base64," + base64.b64encode(_png_bytes()).decode()


def _flight(payload: dict) -> str:
    """A Next.js flight response body, whose `1:` line carries the payload."""
    return "0:{}\n1:" + json.dumps(payload) + "\n"


class FakeHTTPX:
    """Only the two names the signed-R2 PUT touches on ``httpx``."""

    HTTPError = httpx.HTTPError

    def __init__(self, status: int = 200) -> None:
        self.status = int(status)
        self.puts: list[dict] = []

    def AsyncClient(self, **_kwargs):  # noqa: N802 - mirrors httpx's own name
        outer = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            async def put(self, url, *, content=None, headers=None):
                outer.puts.append(
                    {"url": url, "size": len(content or b""), "headers": dict(headers or {})}
                )
                return SimpleNamespace(status_code=outer.status)

        return _Client()


class StaleServerActionTests(DirectTransportCase):
    """Arena redeploys a few times a day; every deploy renames the actions.

    A tab left open across one keeps handing out the previous build's ids, and
    Arena answers those with a bare 404.  Before the retry below that read as
    "img2img is broken" and no amount of re-binding the session helped.
    """

    STALE = {"status": 404, "headers": {}, "text": "Server action not found."}
    UPLOAD_OK = {
        "status": 200,
        "headers": {},
        "text": _flight(
            {
                "success": True,
                "data": {"uploadUrl": "https://r2.example/put?sig=1", "key": "uploads/a.png"},
            }
        ),
    }
    SIGNED_OK = {
        "status": 200,
        "headers": {},
        "text": _flight({"success": True, "data": {"url": "https://r2.example/get?sig=2"}}),
    }
    OLD_IDS = {"generateUploadUrl": "OLD_UP", "getSignedUrl": "OLD_SIGN"}
    NEW_IDS = {"generateUploadUrl": "NEW_UP", "getSignedUrl": "NEW_SIGN"}

    def setUp(self) -> None:
        super().setUp()
        self.httpx = FakeHTTPX()
        self._patch(arena_direct, "httpx", self.httpx)

    def _upload(self, page: FakePage):
        client = self.client(page)
        return self.run_async(client._upload_reference(page, _png_data_url()))

    def test_marker_is_what_separates_a_redeploy_from_a_real_404(self) -> None:
        self.assertTrue(arena_direct.action_is_stale(404, "Server action not found."))
        self.assertTrue(arena_direct.action_is_stale(404, "SERVER ACTION NOT FOUND."))
        self.assertFalse(arena_direct.action_is_stale(404, "Not Found"))
        self.assertFalse(arena_direct.action_is_stale(200, "Server action not found."))
        self.assertFalse(arena_direct.action_is_stale(0, ""))

    def test_a_redeploy_is_retried_through_a_tab_reload(self) -> None:
        page = FakePage(
            responses=[self.STALE, self.UPLOAD_OK, self.SIGNED_OK],
            action_sets=[self.OLD_IDS, self.NEW_IDS],
        )
        entry = self._upload(page)
        self.assertEqual(entry["contentType"], "image/png")
        self.assertEqual(entry["url"], "https://r2.example/get?sig=2")
        self.assertEqual(page.reload_calls, 1)
        # Rescanning without the reload would have returned the same dead ids.
        self.assertEqual(page.action_reads, 2)
        sent = [request["headers"].get("Next-Action") for request in page.requests]
        self.assertEqual(sent, ["OLD_UP", "NEW_UP", "NEW_SIGN"])
        self.assertEqual(len(self.httpx.puts), 1)
        self.assertEqual(self.httpx.puts[0]["size"], len(_png_bytes()))

    def test_a_redeploy_between_step_one_and_step_three_is_also_retried(self) -> None:
        page = FakePage(
            responses=[self.UPLOAD_OK, self.STALE, self.UPLOAD_OK, self.SIGNED_OK],
            action_sets=[self.OLD_IDS, self.NEW_IDS],
        )
        entry = self._upload(page)
        self.assertEqual(entry["url"], "https://r2.example/get?sig=2")
        self.assertEqual(page.reload_calls, 1)
        # The image is PUT again on the retry: the first key belonged to the
        # build that has already gone away.
        self.assertEqual(len(self.httpx.puts), 2)

    def test_two_failures_in_a_row_stop_and_explain_themselves(self) -> None:
        page = FakePage(
            responses=[self.STALE, self.STALE],
            action_sets=[self.OLD_IDS, self.NEW_IDS],
        )
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self._upload(page)
        self.assertEqual(caught.exception.code, "upload_action_stale")
        message = str(caught.exception)
        self.assertIn("更新了页面", message)
        self.assertNotIn("重新绑定", message)  # the session was never the problem
        self.assertEqual(page.reload_calls, 1)  # exactly one retry, not a loop
        self.assertEqual(self.httpx.puts, [])

    def test_the_happy_path_never_reloads_the_tab(self) -> None:
        page = FakePage(responses=[self.UPLOAD_OK, self.SIGNED_OK])
        self._upload(page)
        self.assertEqual(page.reload_calls, 0)
        self.assertEqual(page.action_reads, 1)

    def test_a_plain_404_is_still_reported_as_a_plain_failure(self) -> None:
        page = FakePage(responses=[{"status": 404, "headers": {}, "text": "Not Found"}])
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self._upload(page)
        self.assertEqual(caught.exception.code, "upload_url_failed")
        self.assertEqual(page.reload_calls, 0)

    def test_refreshing_the_actions_ignores_a_warm_cache(self) -> None:
        arena_direct._ACTIONS = dict(self.OLD_IDS)
        arena_direct._ACTIONS_AT = time.time()
        page = FakePage(action_sets=[self.NEW_IDS])
        client = self.client(page)
        cached = self.run_async(client._next_actions(page))
        self.assertEqual(cached, self.OLD_IDS)
        self.assertEqual(page.reload_calls, 0)
        fresh = self.run_async(client._next_actions(page, refresh=True))
        self.assertEqual(fresh, self.NEW_IDS)
        self.assertEqual(page.reload_calls, 1)
        self.assertEqual(arena_direct._ACTIONS, self.NEW_IDS)


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


class FakeCDP:
    """A CDP socket that only knows targets and one file:// read.

    ``start_interactive_auth`` talks to the socket directly rather than through
    ``open_page``, so the auto-secret path needs its own double.  Anything this
    fake does not implement raises ``CDPError``, which is exactly what the real
    socket does for an unsupported method - the client is expected to survive it.
    """

    def __init__(self, *, file_text: str = "", files: dict | None = None, arena_tab: bool = True) -> None:
        self.file_text = file_text
        self.files = dict(files or {})
        self.arena_tab = arena_tab
        self.calls: list[tuple[str, dict]] = []
        self.created: list[str] = []
        self.closed: list[str] = []
        self._sessions: dict[str, str] = {}
        self._targets: dict[str, str] = {}
        self._n = 0

    async def __aenter__(self) -> FakeCDP:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def _content(self, url: str) -> str:
        if url in self.files:
            return self.files[url]
        if "secrets" in url:
            return self.file_text
        return ""

    async def command(self, method, params=None, *, session_id=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, {**params, "session": session_id, "timeout": timeout}))
        if method == "Target.getTargets":
            if not self.arena_tab:
                return {"targetInfos": []}
            return {
                "targetInfos": [
                    {"type": "page", "targetId": "arena-1", "url": "https://arena.ai/text/direct"}
                ]
            }
        if method == "Target.createTarget":
            self._n += 1
            target_id = f"target-{self._n}"
            url = str(params.get("url") or "")
            self.created.append(url)
            self._targets[target_id] = url
            return {"targetId": target_id}
        if method == "Target.attachToTarget":
            target_id = str(params.get("targetId") or "")
            session = f"session-for-{target_id}"
            if target_id in self._targets:
                self._sessions[session] = self._targets[target_id]
            return {"sessionId": session}
        if method == "Target.closeTarget":
            self.closed.append(str(params.get("targetId") or ""))
            return {}
        if method == "Target.activateTarget":
            return {}
        raise arena_direct.CDPError(f"unsupported in the fake: {method}")

    async def evaluate(self, expression, *, session_id: str, timeout=None):
        self.calls.append(
            (
                "Runtime.evaluate",
                {"session": session_id, "timeout": timeout, "expression": str(expression)[:40]},
            )
        )
        if session_id in self._sessions:
            return self._content(self._sessions[session_id])
        raise arena_direct.CDPError("no page here")


class AutoLinkSecretTests(DirectTransportCase):
    """The operator fills in one address; the signing key is discovered.

    A hand-copied key was the only way to produce an unopenable link: it drifts
    the moment the secret is regenerated.  Reading it out of the same file the
    gateway validates against removes that failure mode entirely.
    """

    SECRET = "a1b2c3d4" * 8  # 64 hex chars, like `setup.sh` writes

    def setUp(self) -> None:
        super().setUp()
        arena_direct._LINK_SECRETS.clear()
        arena_direct._GATEWAY_URLS.clear()

    def _cdp(self, **kwargs) -> FakeCDP:
        fake = FakeCDP(file_text=kwargs.pop("file_text", self.SECRET), **kwargs)
        self._patch(arena_direct, "CDPWebSocket", lambda *_a, **_k: fake)
        return fake

    def test_gateway_url_is_derived_from_the_novnc_address(self) -> None:
        self.assertEqual(
            arena_direct.gateway_base_from("http://HOST:6082/vnc.html?autoconnect=true"),
            "http://HOST:6081",
        )
        # An address that already points at the gateway keeps its prefix.
        self.assertEqual(
            arena_direct.gateway_base_from("https://HOST:6081/arena"),
            "https://HOST:6081/arena",
        )
        self.assertEqual(arena_direct.gateway_base_from("", ""), "")

    def test_secret_shaped_values_are_accepted_and_html_is_not(self) -> None:
        self.assertTrue(arena_direct.looks_like_link_secret(self.SECRET))
        self.assertFalse(arena_direct.looks_like_link_secret(""))
        self.assertFalse(arena_direct.looks_like_link_secret("short"))
        self.assertFalse(
            arena_direct.looks_like_link_secret("<html><body>404 not found</body></html>")
        )

    def test_scratch_tab_is_always_closed(self) -> None:
        fake = FakeCDP(file_text=self.SECRET)
        text = self.run_async(
            arena_direct.read_browser_file(fake, "file:///run/secrets/interactive_link_secret")
        )
        self.assertEqual(text, self.SECRET)
        self.assertEqual(fake.created, ["file:///run/secrets/interactive_link_secret"])
        self.assertEqual(len(fake.closed), 1, "a leaked tab would pile up in the operator's Chrome")
        self.assertTrue(all(params.get("background") for m, params in fake.calls if m == "Target.createTarget"))
        reads = [params for method, params in fake.calls if method == "Runtime.evaluate"]
        self.assertTrue(reads[0]["expression"].startswith("document.body"))
        self.assertTrue(all(params["timeout"] for params in reads), "reads must be bounded")

    def test_unreadable_file_returns_empty_and_still_closes(self) -> None:
        fake = FakeCDP(file_text="")
        text = self.run_async(
            arena_direct.read_browser_file(
                fake, "file:///run/secrets/interactive_link_secret", attempts=1
            )
        )
        self.assertEqual(text, "")
        self.assertEqual(len(fake.closed), 1)

    def test_link_is_signed_with_the_key_read_from_the_browser(self) -> None:
        fake = self._cdp()
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223",
            gateway_url="http://HOST:6081",
            link_secret="",            # nothing configured: this is the point
            data_dir=self.data_dir,
        )
        payload = self.run_async(client.start_interactive_auth())
        link = payload["browser_url"]
        self.assertTrue(link.startswith("http://HOST:6081/v/"))
        token = link.rsplit("/", 1)[-1]
        self.assertTrue(_verify_token(token, self.SECRET), "the gateway must accept the link")
        self.assertNotIn(self.SECRET, link)
        self.assertIn("file:///run/secrets/interactive_link_secret", fake.created)

    def test_the_key_is_read_once_and_then_cached(self) -> None:
        fake = self._cdp()
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223", gateway_url="http://HOST:6081", data_dir=self.data_dir
        )
        first = self.run_async(client.start_interactive_auth())
        second = self.run_async(client.start_interactive_auth())
        reads = [url for url in fake.created if url.startswith("file://")]
        self.assertEqual(len(reads), 1, "one file read per hour, not one per verification")
        for payload in (first, second):
            self.assertTrue(
                _verify_token(payload["browser_url"].rsplit("/", 1)[-1], self.SECRET)
            )

    def test_a_configured_key_wins_and_skips_the_browser_read(self) -> None:
        fake = self._cdp(file_text="ffffffff" * 8)
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223",
            gateway_url="http://HOST:6081",
            link_secret="explicit-secret-value",
            data_dir=self.data_dir,
        )
        payload = self.run_async(client.start_interactive_auth())
        token = payload["browser_url"].rsplit("/", 1)[-1]
        self.assertTrue(_verify_token(token, "explicit-secret-value"))
        self.assertEqual([u for u in fake.created if u.startswith("file://")], [])

    def test_only_the_novnc_address_is_enough(self) -> None:
        self._cdp()
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223",
            vnc_url="http://HOST:6082/vnc.html",
            data_dir=self.data_dir,
        )
        payload = self.run_async(client.start_interactive_auth())
        self.assertTrue(payload["browser_url"].startswith("http://HOST:6081/v/"))
        self.assertTrue(
            _verify_token(payload["browser_url"].rsplit("/", 1)[-1], self.SECRET)
        )

    def test_no_address_at_all_says_which_field_to_fill(self) -> None:
        self._cdp()
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223", data_dir=self.data_dir
        )
        with self.assertRaises(bridge_client.BridgeError) as ctx:
            self.run_async(client.start_interactive_auth())
        message = str(ctx.exception)
        self.assertIn("browser_gateway_url", message)
        self.assertIn("6081", message)

    def test_unreadable_secret_reports_it_instead_of_a_dead_link(self) -> None:
        self._cdp(file_text="")
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223", gateway_url="http://HOST:6081", data_dir=self.data_dir
        )
        with self.assertRaises(bridge_client.BridgeError) as ctx:
            self.run_async(client.start_interactive_auth())
        self.assertIn("interactive_link_secret", str(ctx.exception))

    def test_vnc_url_is_the_fallback_when_no_key_can_be_found(self) -> None:
        self._cdp(file_text="")
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223",
            gateway_url="http://HOST:6081",
            vnc_url="http://HOST:6082/vnc.html",
            data_dir=self.data_dir,
        )
        payload = self.run_async(client.start_interactive_auth())
        self.assertEqual(payload["browser_url"], "http://HOST:6082/vnc.html")


class ZeroConfigDirectTests(DirectTransportCase):
    """Nothing filled in at all: the address comes from the deployment itself.

    ``setup.sh`` writes the public gateway URL into the browser's state
    directory, which the plugin can read over CDP.  That is the difference
    between "flip one switch" and "go find the server IP and paste it".
    """

    SECRET = "9f8e7d6c" * 8
    GATEWAY_FILE = "file:///data/browser/gateway-url.txt"

    def setUp(self) -> None:
        super().setUp()
        arena_direct._LINK_SECRETS.clear()
        arena_direct._GATEWAY_URLS.clear()

    def _cdp(self, files: dict) -> FakeCDP:
        fake = FakeCDP(file_text=self.SECRET, files=files)
        self._patch(arena_direct, "CDPWebSocket", lambda *_a, **_k: fake)
        return fake

    def test_address_and_key_both_come_from_the_browser(self) -> None:
        fake = self._cdp({self.GATEWAY_FILE: "http://198.51.100.7:6081\n"})
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223", data_dir=self.data_dir
        )
        payload = self.run_async(client.start_interactive_auth())
        link = payload["browser_url"]
        self.assertTrue(link.startswith("http://198.51.100.7:6081/v/"))
        self.assertTrue(_verify_token(link.rsplit("/", 1)[-1], self.SECRET))
        self.assertIn(self.GATEWAY_FILE, fake.created)

    def test_a_novnc_address_in_the_file_is_normalised_to_the_gateway_port(self) -> None:
        self._cdp({self.GATEWAY_FILE: "  http://198.51.100.7:6082/vnc.html  \n"})
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223", data_dir=self.data_dir
        )
        payload = self.run_async(client.start_interactive_auth())
        self.assertTrue(payload["browser_url"].startswith("http://198.51.100.7:6081/v/"))

    def test_configured_address_still_wins_over_the_file(self) -> None:
        fake = self._cdp({self.GATEWAY_FILE: "http://198.51.100.7:6081"})
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223",
            gateway_url="http://operator-choice:6081",
            data_dir=self.data_dir,
        )
        payload = self.run_async(client.start_interactive_auth())
        self.assertTrue(payload["browser_url"].startswith("http://operator-choice:6081/v/"))
        self.assertNotIn(self.GATEWAY_FILE, fake.created)

    def test_junk_in_the_file_is_ignored_rather_than_producing_a_dead_link(self) -> None:
        self._cdp({self.GATEWAY_FILE: "<!DOCTYPE html><h1>Index of /data/browser</h1>"})
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223", data_dir=self.data_dir
        )
        with self.assertRaises(bridge_client.BridgeError) as ctx:
            self.run_async(client.start_interactive_auth())
        self.assertIn("browser_gateway_url", str(ctx.exception))

    def test_the_address_is_read_once_and_then_cached(self) -> None:
        fake = self._cdp({self.GATEWAY_FILE: "http://198.51.100.7:6081"})
        client = arena_direct.ArenaDirectClient(
            "http://arena-browser:9223", data_dir=self.data_dir
        )
        self.run_async(client.start_interactive_auth())
        self.run_async(client.start_interactive_auth())
        self.assertEqual(fake.created.count(self.GATEWAY_FILE), 1)


class ForeignNetworkTests(DirectTransportCase):
    """AstrBot deployed outside the compose project, so `arena-browser` is unknown.

    The container name only resolves for containers sharing the network.  Rather
    than turning that into a configuration question, an endpoint that does not
    answer is retried against the addresses the same host answers on.
    """

    WS = "ws://127.0.0.1:9223/devtools/browser/abc"

    def _reachable(self, *endpoints: str) -> list[str]:
        """Patch the probe so only `endpoints` answer; returns the probe log."""
        seen: list[str] = []
        allowed = set(endpoints)

        async def _probe(endpoint, *, timeout=arena_direct.CDP_PROBE_TIMEOUT):
            seen.append(endpoint)
            assert timeout > 0
            if endpoint in allowed:
                return {"webSocketDebuggerUrl": self.WS, "Browser": "Chrome/140"}
            return {}

        self._patch(arena_direct, "probe_cdp_endpoint", _probe)
        return seen

    def test_candidates_keep_the_scheme_port_and_path(self) -> None:
        self.assertEqual(
            arena_direct.cdp_fallback_endpoints("http://arena-browser:9223"),
            [
                "http://host.docker.internal:9223",
                "http://172.17.0.1:9223",
                "http://127.0.0.1:9223",
            ],
        )
        self.assertEqual(
            arena_direct.cdp_fallback_endpoints("https://arena-browser:9999/cdp"),
            [
                "https://host.docker.internal:9999/cdp",
                "https://172.17.0.1:9999/cdp",
                "https://127.0.0.1:9999/cdp",
            ],
        )

    def test_an_address_the_operator_typed_is_never_second_guessed(self) -> None:
        # A deliberate IP that is down means "that box is down", not "try another".
        self.assertEqual(arena_direct.cdp_fallback_endpoints("http://10.0.0.9:9223"), [])
        self.assertEqual(arena_direct.cdp_fallback_endpoints("http://127.0.0.1:9223"), [])
        self.assertEqual(arena_direct.cdp_fallback_endpoints(""), [])

    def test_a_working_address_is_never_slowed_down_by_probing_others(self) -> None:
        seen = self._reachable("http://arena-browser:9223")
        socket = arena_direct.CDPWebSocket("http://arena-browser:9223")
        self.assertEqual(self.run_async(socket._websocket_url()), self.WS)
        self.assertEqual(seen, ["http://arena-browser:9223"])

    def test_loopback_is_found_when_the_container_name_does_not_resolve(self) -> None:
        seen = self._reachable("http://127.0.0.1:9223")
        socket = arena_direct.CDPWebSocket("http://arena-browser:9223")
        self.assertEqual(self.run_async(socket._websocket_url()), self.WS)
        # The reachable address replaces the configured one for this socket, so
        # the loopback-host rewrite in `connect` points at something reachable.
        self.assertEqual(socket.endpoint, "http://127.0.0.1:9223")
        self.assertIn("http://host.docker.internal:9223", seen)

    def test_the_discovery_is_cached_so_later_commands_go_straight_there(self) -> None:
        seen = self._reachable("http://172.17.0.1:9223")
        first = arena_direct.CDPWebSocket("http://arena-browser:9223")
        self.run_async(first._websocket_url())
        second = arena_direct.CDPWebSocket("http://arena-browser:9223")
        seen.clear()
        self.assertEqual(self.run_async(second._websocket_url()), self.WS)
        self.assertEqual(seen, ["http://172.17.0.1:9223"])

    def test_nothing_reachable_names_the_one_command_that_fixes_it(self) -> None:
        self._reachable()
        socket = arena_direct.CDPWebSocket("http://arena-browser:9223")
        with self.assertRaises(bridge_client.BridgeError) as ctx:
            self.run_async(socket._websocket_url())
        message = str(ctx.exception)
        self.assertIn("docker network connect", message)
        self.assertIn("astrbot", message)
        self.assertIn("docker ps", message)
        # A stale cache entry must not survive a total outage.
        self.assertEqual(arena_direct._CDP_ENDPOINTS, {})

    def test_a_stale_cache_entry_falls_back_to_the_configured_address(self) -> None:
        arena_direct._CDP_ENDPOINTS["http://arena-browser:9223"] = (
            "http://127.0.0.1:9223",
            time.time(),
        )
        seen = self._reachable("http://arena-browser:9223")
        socket = arena_direct.CDPWebSocket("http://arena-browser:9223")
        self.assertEqual(self.run_async(socket._websocket_url()), self.WS)
        self.assertEqual(seen[0], "http://127.0.0.1:9223")
        self.assertIn("http://arena-browser:9223", seen)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()











