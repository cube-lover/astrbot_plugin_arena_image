import unittest

from tests._stream_test_utils import BaseBridgeTest


class TestBrowserCookiePriority(BaseBridgeTest):
    async def test_live_browser_cookie_is_preferred_over_stale_pool_entry(self) -> None:
        self.setup_config(
            {
                "auth_token": "",
                "auth_tokens": ["legacy-token"],
                "persist_arena_auth_cookie": True,
                "browser_cookies": {"arena-auth-prod-v1": "cookie-token-1"},
            }
        )
        self.main.current_token_index = 1

        token = self.main.get_next_auth_token()

        self.assertEqual(token, "cookie-token-1")


if __name__ == "__main__":
    unittest.main()
