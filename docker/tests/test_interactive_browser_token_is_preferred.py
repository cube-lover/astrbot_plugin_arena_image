import unittest
from unittest.mock import AsyncMock, patch

from tests._stream_test_utils import BaseBridgeTest

_MODEL = "test-chat-model"
_SENTINEL_STATUS = 599


class _Request:
    def __init__(self, stream: bool = False) -> None:
        self._stream = bool(stream)

    async def json(self):
        return {
            "model": _MODEL,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": self._stream,
        }


class TestInteractiveBrowserTokenIsPreferred(BaseBridgeTest):
    """A token from the signed-in browser must not be thrown away by the side channel."""

    def _model_row(self) -> list[dict]:
        return [
            {
                "publicName": _MODEL,
                "id": "model-id",
                "organization": "test-org",
                "capabilities": {
                    "inputCapabilities": {"text": True},
                    "outputCapabilities": {"text": True},
                },
            }
        ]

    async def _call(self, browser_token, refresh_mock):
        """Run the endpoint until auth-token selection and report how it got there."""
        with (
            patch.object(self.main, "get_models", return_value=self._model_row()),
            patch.object(
                self.main,
                "get_recaptcha_v3_token_via_interactive_browser",
                AsyncMock(return_value=browser_token),
            ),
            patch.object(self.main, "refresh_recaptcha_token", refresh_mock),
            patch.object(
                self.main,
                "maybe_refresh_expired_auth_tokens",
                AsyncMock(return_value=None),
            ),
            patch.object(
                self.main,
                "get_next_auth_token",
                side_effect=self.main.HTTPException(
                    status_code=_SENTINEL_STATUS,
                    detail="reached-auth-token-selection",
                ),
            ),
            patch.object(
                self.main.interactive_auth_manager,
                "sync_live_browser_session",
                AsyncMock(return_value={"has_logged_in": True, "verified": True}),
            ),
            patch("src.main.print"),
            self.assertRaises(self.main.HTTPException) as caught,
        ):
            await self.main.api_chat_completions(_Request(), {"key": "test-key"})
        return caught.exception

    async def test_browser_token_skips_the_side_channel_refresh(self) -> None:
        # A refresh that fails used to turn this healthy request into a 503.
        refresh = AsyncMock(return_value=None)

        error = await self._call("browser-token", refresh)

        refresh.assert_not_awaited()
        # The request got past the reCAPTCHA gate: it now fails on the fixture's
        # placeholder Arena cookie instead of the mint.
        self.assertEqual(error.detail.get("code"), "arena_auth_expired")

    async def test_without_a_browser_token_the_side_channel_still_runs(self) -> None:
        refresh = AsyncMock(return_value="side-channel-token")

        error = await self._call("", refresh)

        refresh.assert_awaited_once()
        self.assertEqual(error.detail.get("code"), "arena_auth_expired")

    async def test_side_channel_failure_without_a_browser_token_reports_internal_error(self) -> None:
        error = await self._call("", AsyncMock(return_value=None))

        self.assertEqual(error.status_code, 503)
        self.assertEqual(error.detail.get("code"), "recaptcha_mint_failed")


if __name__ == "__main__":
    unittest.main()
