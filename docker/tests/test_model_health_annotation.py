"""The 500/403 annotation must survive both idle time and a bridge restart.

The mechanism itself never broke: every upstream result is still recorded.  What
broke was its lifetime -- the table lived in process memory with a 15 minute TTL,
so `/竞技场画图模型` showed nothing unless you ran it right after a failure, and a
bridge update wiped the whole thing.  These tests pin the two properties that
make the annotation trustworthy: it is written to disk, and it is restored with
per-table expiry.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_arena_image_hardening import _plugin_module


class _BridgeHealthCase(unittest.TestCase):
    """Point the bridge's health file at a scratch directory."""

    def setUp(self) -> None:
        from src import constants, main

        self.constants = constants
        self.main = main

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.health_path = Path(temp.name) / "model_health.json"

        patcher = patch.object(constants, "MODEL_HEALTH_FILE", str(self.health_path))
        patcher.start()
        self.addCleanup(patcher.stop)

        self.addCleanup(main.MODEL_HEALTH.clear)
        self.addCleanup(main.MODEL_VARIANT_HEALTH.clear)
        main.MODEL_HEALTH.clear()
        main.MODEL_VARIANT_HEALTH.clear()

    def _written(self) -> dict:
        return json.loads(self.health_path.read_text(encoding="utf-8"))


class TestTheBridgeRemembersFailures(_BridgeHealthCase):
    def test_recording_a_failure_writes_it_to_disk(self) -> None:
        self.main._record_model_health(
            "gpt-image-2 (medium)",
            500,
            model_variant_id="11111111-1111-7111-8111-111111111111",
            message="upstream exploded",
            source="completion",
        )

        payload = self._written()
        self.assertEqual(payload["models"]["gpt-image-2 (medium)"]["status_code"], 500)
        self.assertEqual(
            payload["variants"]["11111111-1111-7111-8111-111111111111"]["status_code"],
            500,
        )

    def test_a_success_clears_only_the_variant_blame(self) -> None:
        variant = "11111111-1111-7111-8111-111111111111"
        self.main._record_model_health("luna-lisa-alpha", 500, model_variant_id=variant)
        self.main._record_model_health("luna-lisa-alpha", 200, model_variant_id=variant)

        payload = self._written()
        self.assertEqual(payload["models"]["luna-lisa-alpha"]["status_code"], 200)
        self.assertNotIn(variant, payload["variants"])

    def test_a_restart_restores_what_is_still_fresh(self) -> None:
        self.main._record_model_health("gemini-3-pro-image", 403, message="Forbidden")
        # Simulate the process dying and coming back up.
        self.main.MODEL_HEALTH.clear()
        self.main.MODEL_VARIANT_HEALTH.clear()

        self.main._load_model_health()

        entry = self.main.MODEL_HEALTH["gemini-3-pro-image"]
        self.assertEqual(entry["status_code"], 403)
        self.assertEqual(entry["message"], "Forbidden")
        # The snapshot the plugin reads must show it too.
        snapshot = self.main._model_health_snapshot()
        self.assertEqual(
            [item["id"] for item in snapshot["models"]],
            ["gemini-3-pro-image"],
        )
        self.assertEqual(snapshot["models"][0]["source"], "completion")

    def test_an_entry_without_a_source_is_marked_restored(self) -> None:
        self.health_path.write_text(
            json.dumps({"models": {"old-format": {"status_code": 500, "checked_at": time.time()}}}),
            encoding="utf-8",
        )

        self.main._load_model_health()

        self.assertEqual(self.main.MODEL_HEALTH["old-format"]["source"], "restored")

    def test_the_two_tables_expire_on_their_own_clocks(self) -> None:
        # Models are worth showing for hours; variant avoidance must lapse fast so
        # a backend node gets another chance.
        now = time.time()
        self.health_path.write_text(
            json.dumps(
                {
                    "models": {
                        "kept-model": {"status_code": 500, "checked_at": now - 3600},
                        "stale-model": {"status_code": 500, "checked_at": now - 7 * 3600},
                    },
                    "variants": {
                        "kept-variant": {"status_code": 500, "checked_at": now - 60},
                        "stale-variant": {"status_code": 500, "checked_at": now - 20 * 60},
                    },
                }
            ),
            encoding="utf-8",
        )

        self.main._load_model_health()

        self.assertEqual(list(self.main.MODEL_HEALTH), ["kept-model"])
        self.assertEqual(list(self.main.MODEL_VARIANT_HEALTH), ["kept-variant"])

    def test_the_model_table_outlives_a_quarter_hour_of_idling(self) -> None:
        # The bug report: nothing was annotated because the old TTL was 15 minutes.
        self.assertGreaterEqual(self.main.MODEL_HEALTH_TTL_SECONDS, 6 * 3600)
        self.assertLessEqual(self.main.MODEL_VARIANT_FAILURE_TTL_SECONDS, 15 * 60)

        self.main.MODEL_HEALTH["idle-model"] = {
            "status_code": 500,
            "checked_at": time.time() - 20 * 60,
            "message": "",
            "source": "completion",
        }

        snapshot = self.main._model_health_snapshot()
        self.assertEqual([item["id"] for item in snapshot["models"]], ["idle-model"])

    def test_junk_on_disk_is_ignored_rather_than_fatal(self) -> None:
        for blob in ("not json at all", "[]", '{"models": 7}', '{"models": {"x": 1}}'):
            with self.subTest(blob=blob):
                self.health_path.write_text(blob, encoding="utf-8")
                self.main.MODEL_HEALTH.clear()
                self.main._load_model_health()
                self.assertEqual(self.main.MODEL_HEALTH, {})

    def test_a_missing_file_is_not_an_error(self) -> None:
        self.health_path.unlink(missing_ok=True)
        self.main._load_model_health()
        self.assertEqual(self.main.MODEL_HEALTH, {})

    def test_an_unwritable_path_does_not_break_generation(self) -> None:
        with patch.object(self.constants, "MODEL_HEALTH_FILE", str(self.health_path / "nested" / "x.json")):
            self.main._record_model_health("gpt-image-1", 500)
        self.assertEqual(self.main.MODEL_HEALTH["gpt-image-1"]["status_code"], 500)


class TestThePluginAnnotatesTheList(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = _plugin_module().ArenaImagePlugin

    def test_a_failure_shows_the_code_and_how_long_ago(self) -> None:
        now = time.time()
        cases = {
            0: " ⚠500 刚刚",
            5 * 60: " ⚠500 5分钟前",
            3 * 3600: " ⚠500 3小时前",
            2 * 86400: " ⚠500 2天前",
        }
        for age, expected in cases.items():
            with self.subTest(age=age):
                entry = {"status_code": 500, "checked_at": now - age}
                self.assertEqual(self.plugin._model_health_text(entry), expected)

    def test_a_success_stays_short(self) -> None:
        entry = {"status_code": 200, "checked_at": time.time() - 3 * 3600}
        self.assertEqual(self.plugin._model_health_text(entry), " ✅")

    def test_an_untested_model_is_not_annotated(self) -> None:
        for entry in (None, {}, {"status_code": 0}, {"status_code": "nope"}, 0):
            with self.subTest(entry=entry):
                self.assertEqual(self.plugin._model_health_text(entry), "")

    def test_an_old_bridge_without_checked_at_still_shows_the_code(self) -> None:
        self.assertEqual(self.plugin._model_health_text({"status_code": 403}), " ⚠403")
        self.assertEqual(self.plugin._model_health_text(403), " ⚠403")

    def test_a_clock_skewed_timestamp_drops_the_age_not_the_code(self) -> None:
        entry = {"status_code": 502, "checked_at": time.time() + 600}
        self.assertEqual(self.plugin._model_health_text(entry), " ⚠502")


class TestThePluginReadsTheSnapshot(unittest.TestCase):
    def _plugin(self, payload: dict):
        from tests.test_arena_image_hardening import _make_plugin

        main, plugin = _make_plugin(self, {"model_health_cache_seconds": 0})

        class FakeClient:
            async def model_health(self) -> dict:
                return payload

        plugin._client = lambda: FakeClient()
        return main, plugin

    def test_checked_at_is_carried_through_not_dropped(self) -> None:
        import asyncio

        stamp = time.time() - 1800
        _, plugin = self._plugin(
            {
                "models": [
                    {"id": "gpt-image-2 (medium)", "status_code": 500, "checked_at": stamp},
                    {"id": "luna-lisa-alpha", "status_code": 200},
                    {"id": "untested", "status_code": 0},
                    {"id": "", "status_code": 500},
                    "not-a-dict",
                ],
                "variants": [],
            }
        )

        health = asyncio.run(plugin._fetch_model_health(force=True))

        self.assertEqual(
            health["gpt-image-2 (medium)"],
            {"status_code": 500, "checked_at": stamp},
        )
        self.assertEqual(health["luna-lisa-alpha"], {"status_code": 200, "checked_at": 0.0})
        self.assertNotIn("untested", health)
        self.assertNotIn("", health)
        self.assertEqual(
            plugin._model_health_text(health["gpt-image-2 (medium)"]),
            " ⚠500 30分钟前",
        )


if __name__ == "__main__":
    unittest.main()
