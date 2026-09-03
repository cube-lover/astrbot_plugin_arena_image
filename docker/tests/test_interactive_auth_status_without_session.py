import unittest
from unittest.mock import AsyncMock, patch

from src.interactive_auth import InteractiveAuthError, InteractiveAuthManager

_DEAD_BROWSER = {
    "has_cf_clearance": False,
    "has_arena_auth": False,
    "has_logged_in": False,
    "verified": False,
}


class TestInteractiveAuthStatusWithoutSession(unittest.IsolatedAsyncioTestCase):
    """Verification sessions expire after 15 minutes; the cookies they minted do not."""

    async def test_latest_still_raises_so_status_keeps_its_expired_marker(self) -> None:
        manager = InteractiveAuthManager()
        with self.assertRaises(InteractiveAuthError):
            await manager.latest({})

    async def test_logged_in_browser_is_reported_as_verified(self) -> None:
        manager = InteractiveAuthManager()
        live = {
            "has_cf_clearance": True,
            "has_arena_auth": True,
            "has_logged_in": True,
            "verified": True,
        }

        with patch.object(manager, "sync_live_browser_session", AsyncMock(return_value=live)):
            payload = await manager.latest_or_snapshot({})

        self.assertEqual(payload["status"], "verified")
        self.assertTrue(payload["verified"])
        self.assertTrue(payload["has_logged_in"])
        self.assertEqual(payload["session_id"], "")
        self.assertEqual(payload["browser_url"], "")
        self.assertNotIn("value", payload)

    async def test_persisted_cookie_is_reported_when_the_browser_is_unreachable(self) -> None:
        manager = InteractiveAuthManager()

        with patch.object(
            manager,
            "sync_live_browser_session",
            AsyncMock(return_value=dict(_DEAD_BROWSER)),
        ):
            payload = await manager.latest_or_snapshot({"auth_tokens": ["auth-token-1"]})

        self.assertEqual(payload["status"], "idle")
        self.assertFalse(payload["verified"])
        self.assertTrue(payload["has_arena_auth"])
        self.assertIn("已保存的 Arena 登录态", payload["message"])

    async def test_empty_state_asks_the_operator_to_verify(self) -> None:
        manager = InteractiveAuthManager()

        with patch.object(
            manager,
            "sync_live_browser_session",
            AsyncMock(return_value=dict(_DEAD_BROWSER)),
        ):
            payload = await manager.latest_or_snapshot({})

        self.assertEqual(payload["status"], "idle")
        self.assertFalse(payload["has_arena_auth"])
        self.assertIn("/竞技场验证", payload["message"])


if __name__ == "__main__":
    unittest.main()
