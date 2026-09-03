"""Both token validations: which Arena session we hold, and which action we mint.

A fresh third-party deployment loses its `auth_tokens` entry within hours: the
live session then exists only in `browser_cookies`.  Every consumer of the
question "do we have a session?" has to agree on the answer.  While they
disagreed, the bridge minted a `sign_up` reCAPTCHA token for a signed-in
request (Arena compares it with the `X-Recaptcha-Action` header and answers
403), then told the operator their cookie had expired -- sending them through a
re-bind that changed nothing and dropped the fresh cookie again.
"""

from __future__ import annotations

import base64
import json
import time
import unittest
from unittest.mock import patch

from astrbot_plugin_arena_image import bridge_client

from tests._stream_test_utils import BaseBridgeTest
from tests.test_arena_image_hardening import _plugin_module

COOKIE_NAME = "arena-auth-prod-v1"


def _jwt(claims: dict) -> str:
    def segment(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = segment({"alg": "HS256", "typ": "JWT"})
    return f"{header}.{segment(claims)}.fixture-signature"


def _envelope(*, logged_in: bool, ttl_seconds: int) -> dict:
    expires_at = int(time.time()) + int(ttl_seconds)
    claims: dict = {"exp": expires_at, "is_anonymous": not logged_in}
    if logged_in:
        claims["email"] = "fixture@example.com"
    return {
        "access_token": _jwt(claims),
        "refresh_token": "fixture-refresh-token",
        "expires_at": expires_at,
        "user": {"id": "fixture-user", "is_anonymous": not logged_in},
    }


def _cookie(*, logged_in: bool = True, ttl_seconds: int = 3600) -> str:
    raw = json.dumps(_envelope(logged_in=logged_in, ttl_seconds=ttl_seconds))
    return "base64-" + base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _url_safe_cookie(*, logged_in: bool = True, ttl_seconds: int = 3600) -> str:
    """An unpadded cookie in the URL-safe alphabet, as Supabase's helpers emit."""
    envelope = _envelope(logged_in=logged_in, ttl_seconds=ttl_seconds)
    for pad in range(1, 32):
        envelope["nonce"] = "ÿ" * pad
        raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        if "-" in encoded or "_" in encoded:
            return "base64-" + encoded
    raise AssertionError("could not build a URL-safe fixture cookie")


class _SessionTest(BaseBridgeTest):
    """Isolate the in-memory ephemeral token so ordering cannot leak state."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self._orig_ephemeral = getattr(self.main, "EPHEMERAL_ARENA_AUTH_TOKEN", "")
        self.main.EPHEMERAL_ARENA_AUTH_TOKEN = ""

    async def asyncTearDown(self) -> None:
        self.main.EPHEMERAL_ARENA_AUTH_TOKEN = self._orig_ephemeral
        await super().asyncTearDown()

    def no_session(self, **extra) -> None:
        self.setup_config(
            {
                "auth_token": "",
                "auth_tokens": [],
                "browser_cookies": {},
                "recaptcha_action": "",
                **extra,
            }
        )

    def live_session(self, cookie: str | None = None, **extra) -> str:
        token = cookie or _cookie()
        self.setup_config(
            {
                "auth_token": "",
                "auth_tokens": [],
                "browser_cookies": {COOKIE_NAME: token},
                "recaptcha_action": "",
                **extra,
            }
        )
        return token


class TestArenaSessionResolution(_SessionTest):
    def test_live_browser_cookie_wins_over_an_expired_pool(self) -> None:
        live = self.live_session(auth_tokens=[_cookie(ttl_seconds=-3600)])

        state = self.main.resolve_arena_session()

        self.assertTrue(state["usable"])
        self.assertTrue(state["logged_in"])
        self.assertEqual(state["source"], "browser_cookies")
        self.assertEqual(state["token"], live)
        self.assertEqual(state["expired_candidates"], 1)
        self.assertEqual(state["usable_candidates"], 1)

    def test_url_safe_cookie_decodes_instead_of_looking_invalid(self) -> None:
        self.live_session(_url_safe_cookie())

        state = self.main.resolve_arena_session()

        self.assertTrue(state["usable"], "a base64url cookie must still decode")
        self.assertTrue(state["logged_in"])

    def test_split_google_oauth_cookie_is_recombined(self) -> None:
        cookie = _cookie()
        half = len(cookie) // 2
        self.no_session(
            browser_cookies={
                f"{COOKIE_NAME}.0": cookie[:half],
                f"{COOKIE_NAME}.1": cookie[half:],
            }
        )

        state = self.main.resolve_arena_session()

        self.assertTrue(state["usable"])
        self.assertEqual(state["source"], "browser_cookies_split")

    def test_a_signed_in_session_outranks_a_longer_lived_anonymous_one(self) -> None:
        self.live_session(
            _cookie(logged_in=True, ttl_seconds=600),
            auth_tokens=[_cookie(logged_in=False, ttl_seconds=7200)],
        )

        state = self.main.resolve_arena_session()

        self.assertEqual(state["source"], "browser_cookies")
        self.assertTrue(state["logged_in"])
        self.assertFalse(state["anonymous"])

    def test_only_expired_candidates_stay_refreshable_but_unusable(self) -> None:
        self.no_session(auth_tokens=[_cookie(ttl_seconds=-60)])

        state = self.main.resolve_arena_session()

        self.assertFalse(state["usable"])
        self.assertFalse(state["logged_in"])
        self.assertTrue(state["refreshable"])
        self.assertEqual(state["usable_candidates"], 0)

    def test_the_default_fixture_pool_is_not_a_session(self) -> None:
        # `auth_tokens: ["auth-token-1"]` is the shared fixture: an implausible
        # placeholder must never register as a usable session.
        self.no_session(auth_tokens=["auth-token-1"])

        self.assertFalse(self.main.has_usable_arena_session())
        self.assertFalse(self.main.has_logged_in_arena_session())


class TestRecaptchaActionContract(_SessionTest):
    def test_a_session_outside_the_pool_still_selects_chat_submit(self) -> None:
        # The exact third-party failure: the pool froze at the first captured
        # value while the live cookie sits in `browser_cookies`.
        self.live_session(auth_tokens=["auth-token-1"])

        sitekey, action = self.main.get_recaptcha_settings()

        self.assertEqual(action, "chat_submit")
        self.assertTrue(sitekey)

    def test_without_any_session_the_action_is_sign_up(self) -> None:
        self.no_session()

        self.assertEqual(self.main.get_recaptcha_settings()[1], "sign_up")

    def test_a_pinned_sign_up_action_is_upgraded_for_a_live_session(self) -> None:
        # `recaptcha_action` is scraped from Arena's JS chunks, which also carry
        # the signup literal.  Pinning it must not downgrade a signed-in request.
        self.live_session(recaptcha_action="sign_up")

        self.assertEqual(self.main.get_recaptcha_settings()[1], "chat_submit")

    def test_a_pinned_action_is_otherwise_respected(self) -> None:
        self.live_session(recaptcha_action="chat_submit_v2")

        self.assertEqual(self.main.get_recaptcha_settings()[1], "chat_submit_v2")

    def test_request_headers_and_the_mint_agree(self) -> None:
        token = self.live_session(auth_tokens=["auth-token-1"])

        headers = self.main.get_request_headers_with_token(token, "fixture-recaptcha-token")

        self.assertEqual(headers.get("X-Recaptcha-Action"), "chat_submit")
        self.assertEqual(
            headers.get("X-Recaptcha-Action"),
            self.main.get_recaptcha_settings()[1],
        )

    async def test_interactive_browser_mints_with_the_resolved_action(self) -> None:
        self.live_session(
            auth_tokens=["auth-token-1"],
            interactive_browser_cdp_url="ws://127.0.0.1:9222/devtools/browser/fixture",
        )
        recorded: dict[str, str] = {}

        class _FakeCDP:
            def __init__(self, url: str) -> None:
                self.url = url

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
                return False

            async def command(self, name, params=None, session_id=None):  # noqa: ANN001, ARG002
                if name == "Target.getTargets":
                    return {
                        "targetInfos": [
                            {
                                "type": "page",
                                "url": "https://lmarena.ai/",
                                "targetId": "target-1",
                            }
                        ]
                    }
                if name == "Target.attachToTarget":
                    return {"sessionId": "session-1"}
                if name == "Runtime.evaluate":
                    recorded["expression"] = str((params or {}).get("expression") or "")
                    return {"result": {"value": "fixture-recaptcha-token"}}
                return {}

        from src import interactive_auth

        with patch.object(interactive_auth, "_CDPWebSocket", _FakeCDP):
            token = await self.main.get_recaptcha_v3_token_via_interactive_browser(
                self.main.get_config()
            )

        self.assertEqual(token, "fixture-recaptcha-token")
        expression = recorded.get("expression", "")
        self.assertIn('{action: "chat_submit"}', expression)
        self.assertNotIn('{action: "submit"}', expression)
        self.assertNotIn("sign_up", expression)


class TestArenaRejectionIsNotAnExpiredLogin(_SessionTest):
    def test_403_while_the_session_is_alive_is_reported_as_a_rejection(self) -> None:
        self.live_session(auth_tokens=["auth-token-1"])

        self.assertFalse(
            self.main._is_arena_verification_failure(403, "recaptcha validation failed")
        )
        payload = self.main._arena_auth_error_payload()
        self.assertEqual(payload["code"], "arena_session_rejected")
        self.assertNotIn("已失效", payload["message"])
        self.assertIn("browser_cookies", payload["message"])

    def test_403_without_a_session_still_asks_for_a_rebind(self) -> None:
        self.no_session()

        self.assertTrue(self.main._is_arena_verification_failure(403, "forbidden"))
        payload = self.main._arena_auth_error_payload()
        self.assertEqual(payload["code"], "arena_auth_expired")
        self.assertIn("重新绑定", payload["message"])
        self.assertIn("本地没有保存任何 Arena 会话", payload["message"])

    def test_an_expired_session_is_described_as_expired(self) -> None:
        self.no_session(auth_tokens=[_cookie(ttl_seconds=-60)])

        payload = self.main._arena_auth_error_payload()
        self.assertEqual(payload["code"], "arena_auth_expired")
        self.assertIn("已过期", payload["message"])

    def test_a_cloudflare_challenge_always_needs_verification(self) -> None:
        self.live_session()

        self.assertTrue(
            self.main._is_arena_verification_failure(
                403,
                "<title>Just a moment...</title><div class='cf-chl-bypass'>",
            )
        )

    def test_a_plain_200_body_is_never_a_verification_failure(self) -> None:
        self.no_session()

        self.assertFalse(self.main._is_arena_verification_failure(200, "ok"))


class TestStaleAuthTokenPoolSelfHeals(_SessionTest):
    def test_the_expired_pool_is_replaced_by_the_live_browser_session(self) -> None:
        live = self.live_session(
            auth_tokens=[_cookie(ttl_seconds=-3600)],
            persist_arena_auth_cookie=True,
        )

        with patch("src.main.print"):
            token = self.main.get_next_auth_token()

        self.assertEqual(token, live)
        saved = json.loads(self._config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved.get("auth_tokens"), [live])
        # And the reCAPTCHA action now agrees with the healed pool.
        self.assertEqual(self.main.get_recaptcha_settings()[1], "chat_submit")

    def test_a_pool_with_a_live_token_is_left_alone(self) -> None:
        pool = [_cookie(ttl_seconds=1200)]
        self.live_session(auth_tokens=list(pool), persist_arena_auth_cookie=True)

        with patch("src.main.print"):
            self.main.get_next_auth_token()

        saved = json.loads(self._config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved.get("auth_tokens"), pool)


class TestPluginRendersTheNewContract(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = _plugin_module().ArenaImagePlugin

    @staticmethod
    def _error(code: str, message: str) -> bridge_client.BridgeError:
        return bridge_client.BridgeError(
            message,
            payload={"error": {"code": code, "message": message}},
        )

    def test_a_rejected_session_does_not_claim_the_cookie_expired(self) -> None:
        exc = self._error(
            "arena_session_rejected",
            "Arena 拒绝了这次请求，但本地 Arena 会话仍然有效"
            "（已登录，来源 browser_cookies，还有约 31 分钟）。",
        )

        self.assertTrue(exc.requires_interactive_auth)
        hint = self.plugin._verification_hint(exc)

        self.assertIsNotNone(hint)
        self.assertNotIn("Cookie 已失效", hint)
        self.assertIn("/竞技场验证", hint)
        self.assertIn("reCAPTCHA", hint)

    def test_an_expired_session_still_asks_for_a_rebind(self) -> None:
        exc = self._error(
            "arena_auth_expired",
            "Arena 会话 Cookie 已失效或无效（本地没有保存任何 Arena 会话）。",
        )

        hint = self.plugin._verification_hint(exc)

        self.assertIsNotNone(hint)
        self.assertIn("/竞技场重新绑定", hint)

    def test_status_surfaces_the_action_and_the_session_source(self) -> None:
        text = self.plugin._format_verification_status(
            {
                "status": "verified",
                "verified": True,
                "has_cf_clearance": True,
                "has_arena_auth": True,
                "has_logged_in": True,
                "session_source": "browser_cookies",
                "session_expires_in": 1875,
                "session_logged_in": True,
                "recaptcha_action": "chat_submit",
            }
        )

        self.assertIn("browser_cookies", text)
        self.assertIn("31 分钟", text)
        self.assertIn("chat_submit", text)

    def test_status_flags_a_sign_up_action_on_a_signed_in_session(self) -> None:
        text = self.plugin._format_verification_status(
            {
                "status": "verified",
                "verified": True,
                "has_logged_in": True,
                "session_logged_in": True,
                "recaptcha_action": "sign_up",
            }
        )

        self.assertIn("异常", text)

    def test_status_without_the_new_fields_is_unchanged(self) -> None:
        text = self.plugin._format_verification_status({"status": "waiting"})

        self.assertNotIn("会话来源", text)
        self.assertNotIn("reCAPTCHA 动作", text)


if __name__ == "__main__":
    unittest.main()
