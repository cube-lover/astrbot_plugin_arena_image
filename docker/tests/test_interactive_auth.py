from __future__ import annotations

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_plugin_arena_image import bridge_client
from src.interactive_auth import (
    InteractiveAuthManager,
    _safe_cookie_records,
)


class InteractiveAuthHelpersTest(unittest.TestCase):
    def test_cookie_filter_keeps_only_bridge_session_cookies(self) -> None:
        records = _safe_cookie_records(
            [
                {"name": "cf_clearance", "value": "cf"},
                {"name": "arena-auth-prod-v1.0", "value": "part0"},
                {"name": "arena-auth-prod-v1.1", "value": "part1"},
                {"name": "unrelated", "value": "discard"},
                {"name": "cf_bm", "value": ""},
            ]
        )
        self.assertEqual(
            records,
            [
                {"name": "cf_clearance", "value": "cf"},
                {"name": "arena-auth-prod-v1.0", "value": "part0"},
                {"name": "arena-auth-prod-v1.1", "value": "part1"},
            ],
        )

    def test_manager_public_result_does_not_include_cookie_values(self) -> None:
        session = type(
            "Session",
            (),
            {
                "session_id": "session-1",
                "status": "waiting",
                "browser_url": "http://HOST:6080/vnc.html",
                "expires_at": 123,
                "has_cf_clearance": True,
                "has_arena_auth": False,
                "has_logged_in": False,
            },
        )()
        payload = InteractiveAuthManager._public_result(session)
        self.assertEqual(payload["session_id"], "session-1")
        self.assertTrue(payload["has_cf_clearance"])
        self.assertNotIn("value", payload)

    def test_stale_session_returns_expired_marker(self) -> None:
        manager = InteractiveAuthManager()
        payload = asyncio.run(manager.status("stale-session", {}))
        self.assertEqual(payload["session_id"], "stale-session")
        self.assertEqual(payload["status"], "expired")
        self.assertEqual(payload["browser_url"], "")

    def test_live_session_requires_current_browser_cookies(self) -> None:
        from src.interactive_auth import _live_session_is_verified

        has_cf, has_auth, logged_in, verified = _live_session_is_verified([])
        self.assertFalse(has_cf)
        self.assertFalse(has_auth)
        self.assertFalse(logged_in)
        self.assertFalse(verified)

        has_cf, has_auth, logged_in, verified = _live_session_is_verified(
            [
                {"name": "cf_clearance", "value": "cf"},
                {
                    "name": "arena-auth-prod-v1",
                    "value": (
                        "base64-eyJhY2Nlc3NfdG9rZW4iOiJhLmV5SmxlSEFpT2lBeE56ZzRNamN4"
                        "T1RjM2ZRLmIiLCJyZWZyZXNoX3Rva2VuIjoiciIsImV4cGlyZXNfYXQiOjk5"
                        "OTk5OTk5OTksInRva2VuX3R5cGUiOiJiZWFyZXIifQ"
                    ),
                },
            ],
            title="Just a moment...",
            url="https://arena.ai/cdn-cgi/challenge-platform",
        )
        self.assertTrue(has_cf)
        self.assertTrue(has_auth)
        self.assertFalse(logged_in)
        self.assertFalse(verified)

    def test_anonymous_arena_cookie_is_not_verified(self) -> None:
        import base64
        import json
        import time

        from src.interactive_auth import _live_session_is_verified

        access = (
            "eyJhbGciOiJFUzI1NiIsImtpZCI6InRlc3QifQ."
            + base64.urlsafe_b64encode(json.dumps({
                "role": "authenticated",
                "is_anonymous": True,
                "email": "",
                "exp": int(time.time()) + 3600,
            }).encode()).decode().rstrip("=")
            + ".sig"
        )
        token = "base64-" + base64.b64encode(json.dumps({
            "access_token": access,
            "refresh_token": "r",
            "expires_at": int(time.time()) + 3600,
        }).encode()).decode().rstrip("=")
        has_cf, has_auth, logged_in, verified = _live_session_is_verified(
            [
                {"name": "cf_clearance", "value": "cf"},
                {"name": "arena-auth-prod-v1", "value": token},
            ],
            title="Arena",
            url="https://arena.ai/?mode=direct",
        )
        self.assertTrue(has_cf)
        self.assertTrue(has_auth)
        self.assertFalse(logged_in)
        self.assertFalse(verified)

    def test_revoked_cookie_on_login_page_is_not_verified(self) -> None:
        from src.interactive_auth import _live_session_is_verified

        has_cf, has_auth, logged_in, verified = _live_session_is_verified(
            [
                {"name": "cf_clearance", "value": "cf"},
                {
                    "name": "arena-auth-prod-v1",
                    "value": (
                        "base64-eyJhY2Nlc3NfdG9rZW4iOiJhLmV5SmxlSEFpT2lBeE56ZzRNamN4"
                        "T1RjM2ZRLmIiLCJyZWZyZXNoX3Rva2VuIjoiciIsImV4cGlyZXNfYXQiOjk5"
                        "OTk5OTk5OTksInRva2VuX3R5cGUiOiJiZWFyZXIifQ"
                    ),
                },
            ],
            title="Arena",
            url="https://arena.ai/login",
            body_text="Please sign in to continue",
        )
        self.assertTrue(has_cf)
        self.assertTrue(has_auth)
        self.assertFalse(logged_in)
        self.assertFalse(verified)

    def test_explicit_sync_uses_live_browser_flags(self) -> None:
        from src.interactive_auth import InteractiveAuthManager

        manager = InteractiveAuthManager()
        live_cookie = {
            "name": "arena-auth-prod-v1",
            "value": "base64-live",
        }

        async def fake_snapshot(cdp_url, target_id):  # noqa: ARG001
            return (
                "target",
                "Arena",
                "https://arena.ai/?mode=direct",
                "UA",
                "Generate image",
                [{"name": "cf_clearance", "value": "cf"}, live_cookie],
            )

        async def fake_persist(cookies, user_agent):  # noqa: ARG001
            return True, True

        with (
            patch.object(manager, "_target_snapshot", fake_snapshot),
            patch.object(manager, "_persist_cookies", fake_persist),
        ):
            result = asyncio.run(
                manager.sync_live_browser_session(
                    {"interactive_browser_cdp_url": "http://arena-browser:9223"}
                )
            )

        self.assertTrue(result["has_cf_clearance"])
        self.assertTrue(result["has_arena_auth"])


class InteractiveAuthClientTest(unittest.TestCase):
    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code

        def json(self):
            return self.payload

    class FakeAsyncClient:
        response = None
        instances = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.requests = []
            self.__class__.instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, **kwargs):
            self.requests.append((method, url, kwargs))
            return self.response

    def test_interactive_auth_endpoints(self) -> None:
        self.FakeAsyncClient.instances.clear()
        self.FakeAsyncClient.response = self.FakeResponse(
            {
                "session_id": "s1",
                "status": "waiting",
                "browser_url": "http://HOST:6080/vnc.html",
            }
        )
        client = bridge_client.ArenaBridgeClient("http://arena-bridge:8000", "KEY")
        with patch.object(bridge_client.httpx, "AsyncClient", self.FakeAsyncClient):
            start = asyncio.run(client.start_interactive_auth())
            status = asyncio.run(client.interactive_auth_status("s1"))

        self.assertEqual(start["session_id"], "s1")
        self.assertEqual(status["status"], "waiting")
        self.assertEqual(
            self.FakeAsyncClient.instances[0].requests[0][1],
            "http://arena-bridge:8000/api/v1/interactive-auth/start",
        )
        self.assertEqual(
            self.FakeAsyncClient.instances[1].requests[0][1],
            "http://arena-bridge:8000/api/v1/interactive-auth/status/s1",
        )

    def test_error_marks_structured_challenge(self) -> None:
        error = bridge_client.BridgeError(
            "服务器需要完成浏览器验证",
            status_code=503,
            payload={
                "detail": {
                    "code": "arena_verification_required",
                    "message": "验证",
                }
            },
        )
        self.assertEqual(error.code, "arena_verification_required")
        self.assertTrue(error.requires_interactive_auth)

    def test_expired_arena_auth_error_requests_rebind(self) -> None:
        cases = (
            bridge_client.BridgeError(
                "Arena 会话 Cookie 已失效或无效。",
                payload={
                    "detail": {
                        "code": "arena_auth_expired",
                        "message": "Arena 会话 Cookie 已失效或无效。",
                    }
                },
            ),
            bridge_client.BridgeError(
                "Unauthorized: Your LMArena auth token has expired or is invalid.",
                payload={
                    "error": {
                        "code": "http_401",
                        "message": (
                            "Unauthorized: Your LMArena auth token has expired or is invalid."
                        ),
                    }
                },
            ),
            bridge_client.BridgeError(
                "LMArena API error 401: Unauthorized.",
                payload={
                    "error": {
                        "code": 401,
                        "message": "LMArena API error 401: Unauthorized.",
                    }
                },
            ),
        )
        for error in cases:
            self.assertTrue(error.requires_interactive_auth)

        # A 401 generated by the Bridge API-key layer is a different failure
        # and must not send the operator through Arena Cookie re-binding.
        self.assertFalse(
            bridge_client.BridgeError(
                "Invalid API Key.",
                status_code=401,
                payload={"detail": "Invalid API Key."},
            ).requires_interactive_auth
        )


class ExpiredArenaAuthBridgeFlowTest(unittest.IsolatedAsyncioTestCase):
    """Exercise the single-expired-token path without touching the live service."""

    async def test_single_expired_token_returns_structured_rebind_error(self) -> None:
        from src import main

        class _Request:
            async def json(self):
                return {
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": False,
                }

        class _CloudscraperResponse:
            status_code = 401
            headers = {}
            text = ""

        class _Cloudscraper:
            class exceptions:
                class CloudflareException(Exception):
                    pass

            @staticmethod
            def create_scraper():
                class _Scraper:
                    @staticmethod
                    def post(*args, **kwargs):  # noqa: ARG004
                        return _CloudscraperResponse()

                return _Scraper()

        original_config_file = main.CONFIG_FILE
        original_tokens = getattr(main, "current_token_index", 0)
        try:
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "auth_tokens": ["expired-token"],
                            "api_keys": [
                                {"name": "test", "key": "test-key", "rpm": 999}
                            ],
                            "persist_arena_auth_cookie": True,
                        }
                    ),
                    encoding="utf-8",
                )
                main.CONFIG_FILE = str(config_path)
                main.current_token_index = 0
                main.chat_sessions.clear()
                main.api_key_usage.clear()

                with (
                    patch.object(
                        main,
                        "get_models",
                        return_value=[
                            {
                                "id": "test-model-id",
                                "publicName": "test-model",
                                "organization": "test",
                                "capabilities": {
                                    "outputCapabilities": {"text": True}
                                },
                            }
                        ],
                    ),
                    patch.object(
                        main,
                        "refresh_recaptcha_token",
                        new=AsyncMock(return_value="recaptcha-token"),
                    ),
                    patch.object(
                        main,
                        "maybe_refresh_expired_auth_tokens",
                        new=AsyncMock(return_value=None),
                    ),
                    patch.object(
                        main,
                        "get_next_auth_token",
                        side_effect=[
                            "expired-token",
                            main.HTTPException(status_code=500, detail="no more tokens"),
                        ],
                    ),
                    patch.dict(sys.modules, {"cloudscraper": _Cloudscraper}),
                ):
                    with self.assertRaises(main.HTTPException) as caught:
                        await main.api_chat_completions(
                            _Request(),
                            {"key": "test-key"},
                        )

                self.assertEqual(caught.exception.status_code, 503)
                detail = caught.exception.detail
                self.assertIsInstance(detail, dict)
                self.assertEqual(detail.get("code"), "arena_auth_expired")
                self.assertIn("/竞技场重新绑定", detail.get("message", ""))
        finally:
            main.CONFIG_FILE = original_config_file
            main.current_token_index = original_tokens
