"""Auth-token persistence: a fresh capture must survive `save_config`.

`save_config(preserve_auth_tokens=True)` used to restore `auth_tokens` from disk
unconditionally, so the pool kept the very first token ever written and every
later `/竞技场验证` was silently discarded.  These tests pin the three-way merge:
caller intent wins, other writers' additions survive, deletions stay deleted.
"""

import json
import unittest

from tests._stream_test_utils import BaseBridgeTest


class AuthTokenPersistenceTest(BaseBridgeTest):
    def _disk(self) -> dict:
        return json.loads(self._config_path.read_text(encoding="utf-8"))

    def _write_disk_tokens(self, tokens: list) -> None:
        """Simulate another writer (dashboard, refresh task) touching config.json."""
        payload = self._disk()
        payload["auth_tokens"] = list(tokens)
        self._config_path.write_text(json.dumps(payload), encoding="utf-8")

    async def test_captured_browser_cookie_replaces_the_stale_pool_entry(self) -> None:
        self.setup_config({"auth_tokens": ["stale-token"], "persist_arena_auth_cookie": True})

        config = self.main.get_config()
        changed = self.main._upsert_browser_session_into_config(
            config,
            [{"name": "arena-auth-prod-v1", "value": "fresh-token"}],
        )
        self.main.save_config(config)

        self.assertTrue(changed)
        self.assertEqual(self._disk()["auth_tokens"], ["fresh-token"])
        self.assertEqual(
            self._disk()["browser_cookies"]["arena-auth-prod-v1"],
            "fresh-token",
        )

    async def test_untouched_pool_still_yields_to_disk(self) -> None:
        """The original protection: a background writer must not clobber the pool."""
        self.setup_config({"auth_tokens": ["stale-token"]})

        config = self.main.get_config()
        self._write_disk_tokens(["stale-token", "dashboard-token"])
        config["cf_clearance"] = "cf-value"
        self.main.save_config(config)

        self.assertEqual(self._disk()["auth_tokens"], ["stale-token", "dashboard-token"])
        self.assertEqual(self._disk()["cf_clearance"], "cf-value")

    async def test_concurrent_dashboard_addition_survives_a_fresh_capture(self) -> None:
        self.setup_config({"auth_tokens": ["stale-token"], "persist_arena_auth_cookie": True})

        config = self.main.get_config()
        self._write_disk_tokens(["stale-token", "dashboard-token"])
        self.main._upsert_browser_session_into_config(
            config,
            [{"name": "arena-auth-prod-v1", "value": "fresh-token"}],
        )
        self.main.save_config(config)

        # Fresh session first (round-robin must not start on a dead token), the
        # dashboard entry kept as a fallback, the replaced one gone.
        self.assertEqual(self._disk()["auth_tokens"], ["fresh-token", "dashboard-token"])

    async def test_deletion_by_another_writer_is_not_resurrected(self) -> None:
        self.setup_config({"auth_tokens": ["token-a", "token-b"]})

        config = self.main.get_config()
        self._write_disk_tokens(["token-a"])
        config["user_agent"] = "UA/1.0"
        self.main.save_config(config)

        self.assertEqual(self._disk()["auth_tokens"], ["token-a"])

    async def test_explicit_removal_by_the_caller_is_honoured(self) -> None:
        self.setup_config({"auth_tokens": ["token-a", "token-b"]})

        config = self.main.get_config()
        config["auth_tokens"] = ["token-a"]
        self.main.save_config(config)

        self.assertEqual(self._disk()["auth_tokens"], ["token-a"])

    async def test_config_copy_without_snapshot_unions_both_views(self) -> None:
        """A hand-built/copied dict carries no snapshot: never lose either side."""
        self.setup_config({"auth_tokens": ["disk-token"]})

        config = dict(self.main.get_config())
        config["auth_tokens"] = ["fresh-token"]
        self.main.save_config(config)

        self.assertEqual(self._disk()["auth_tokens"], ["fresh-token", "disk-token"])

    async def test_interactive_verification_persists_the_new_session(self) -> None:
        """End-to-end shape of the reported bug: re-verifying must update config.json."""
        self.setup_config({"auth_tokens": ["stale-token"], "persist_arena_auth_cookie": True})

        await self.main.interactive_auth_manager._persist_cookies(
            [
                {"name": "arena-auth-prod-v1", "value": "relogin-token"},
                {"name": "cf_clearance", "value": "cf-token"},
            ],
            "UA/2.0",
        )

        disk = self._disk()
        self.assertEqual(disk["auth_tokens"], ["relogin-token"])
        self.assertEqual(disk["cf_clearance"], "cf-token")

    async def test_pool_selection_prefers_the_freshly_persisted_token(self) -> None:
        self.setup_config({"auth_tokens": ["stale-token"], "persist_arena_auth_cookie": True})

        config = self.main.get_config()
        self.main._upsert_browser_session_into_config(
            config,
            [{"name": "arena-auth-prod-v1", "value": "fresh-token"}],
        )
        self.main.save_config(config)

        self.assertEqual(self.main.get_next_auth_token(), "fresh-token")


if __name__ == "__main__":
    unittest.main()
