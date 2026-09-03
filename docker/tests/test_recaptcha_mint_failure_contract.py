import unittest
from unittest.mock import AsyncMock, patch

import httpx

from tests._stream_test_utils import BaseBridgeTest

_MODEL = "test-search-model"


class TestRecaptchaMintFailureContract(BaseBridgeTest):
    """A bridge-side token minting crash must not be sold as an expired session."""

    async def _post(self, live_browser_state: dict) -> httpx.Response:
        with patch.object(self.main, "get_models") as get_models_mock, patch.object(
            self.main,
            "get_recaptcha_v3_token_via_interactive_browser",
            AsyncMock(return_value=None),
        ), patch.object(
            self.main,
            "refresh_recaptcha_token",
            AsyncMock(return_value=None),
        ), patch.object(
            self.main.interactive_auth_manager,
            "sync_live_browser_session",
            AsyncMock(return_value=live_browser_state),
        ), patch("src.main.print"):
            get_models_mock.return_value = [
                {
                    "publicName": _MODEL,
                    "id": "model-id",
                    "organization": "test-org",
                    "capabilities": {
                        "inputCapabilities": {"text": True},
                        "outputCapabilities": {"search": True},
                    },
                }
            ]

            transport = httpx.ASGITransport(app=self.main.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/api/v1/chat/completions",
                    headers={"Authorization": "Bearer test-key"},
                    json={
                        "model": _MODEL,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": False,
                    },
                    timeout=30.0,
                )

    async def test_healthy_browser_reports_an_internal_token_failure(self) -> None:
        response = await self._post(
            {
                "has_cf_clearance": True,
                "has_arena_auth": True,
                "has_logged_in": True,
                "verified": True,
            }
        )

        self.assertEqual(response.status_code, 503)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "recaptcha_mint_failed")
        # The plugin escalates 5xx responses that mention a challenge to
        # "operator must re-verify", which is exactly the wrong advice here.
        message = detail["message"].casefold()
        for term in ("cloudflare", "turnstile", "recaptcha", "arena auth", "登录账号"):
            self.assertNotIn(term, message)

    async def test_unverified_browser_still_asks_for_manual_verification(self) -> None:
        response = await self._post(
            {
                "has_cf_clearance": False,
                "has_arena_auth": False,
                "has_logged_in": False,
                "verified": False,
            }
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "arena_verification_required")


if __name__ == "__main__":
    unittest.main()
