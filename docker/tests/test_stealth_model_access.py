"""Gray-test checkpoints are models, not an entitlement tier.

Arena marks a checkpoint as "stealth" by shipping its row with no
`organization`: that is how a model gets A/B tested without revealing whose it
is.  Upstream LMArenaBridge reads the blank field as a paywall and answers 403
for the whole class.  It is not one -- those rows carry `userSelectable: true`
and a request for one returns a real image, which is the only reason
`luna-lisa-alpha` ever needed a hardcoded exception.

The rows Arena itself refuses are the `userSelectable: false` ones ("Selected
model is not available for user selection"); no local policy makes those work,
so they stay hidden.

These tests pin that split, plus the ordering that keeps the list readable once
the ~120 hidden checkpoints join it: newest first.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from tests.test_arena_image_hardening import _make_plugin
from tests.test_model_creation_time import _uuid7

_AUTH = {"Authorization": "Bearer test-key"}


def _row(public_name: str, **extra) -> dict:
    """One image-capable row, shaped like Arena's own model table."""
    row = {
        "id": _uuid7(datetime.now(timezone.utc)),
        "publicName": public_name,
        "userSelectable": True,
        "capabilities": {
            "outputCapabilities": {"image": {"aspectRatios": ["1:1"]}},
            "inputCapabilities": {"text": True},
        },
    }
    row.update(extra)
    return row


class TestTheBridgeAllowsStealthModels(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from src import constants, main

        self.main = main
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps({"api_keys": [{"name": "test", "key": "test-key", "rpm": 999}]}),
            encoding="utf-8",
        )
        self.models_path = root / "models.json"
        for patcher in (
            patch.object(main, "CONFIG_FILE", str(config_path)),
            patch.object(constants, "MODELS_FILE", str(self.models_path)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        main.api_key_usage.clear()
        self.addCleanup(main.model_usage_stats.clear)

    def _write(self, rows: list[dict]) -> None:
        self.models_path.write_text(json.dumps(rows), encoding="utf-8")

    async def _listed(self) -> list[dict]:
        transport = httpx.ASGITransport(app=self.main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/models", headers=_AUTH)
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]

    async def _request(self, model_name: str) -> httpx.Response:
        """POST a completion that fails loudly the moment it clears both guards."""

        async def _marker(*_args, **_kwargs):
            raise RuntimeError("PAST-THE-GUARDS")

        transport = httpx.ASGITransport(app=self.main.app)
        with patch.object(self.main, "process_message_content", _marker):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/api/v1/chat/completions",
                    headers=_AUTH,
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )

    async def test_a_stealth_row_is_listed(self) -> None:
        self._write([_row("lychee-v0"), _row("gpt-image-2 (medium)", organization="openai")])

        listed = {item["id"]: item for item in await self._listed()}

        self.assertIn("lychee-v0", listed)
        self.assertEqual(listed["lychee-v0"]["owned_by"], "lmarena")
        self.assertTrue(listed["lychee-v0"]["output_image"])

    async def test_a_battle_only_row_stays_hidden(self) -> None:
        self._write(
            [
                _row("muse-image", organization="meta", userSelectable=False),
                _row("lychee-v0"),
            ]
        )

        self.assertEqual([item["id"] for item in await self._listed()], ["lychee-v0"])

    async def test_one_selectable_row_keeps_a_duplicated_name_usable(self) -> None:
        # Arena ships several rows per publicName; the filter runs per row.
        name = "gpt-image-2 (medium)"
        self._write(
            [
                _row(name, organization="openai", userSelectable=False),
                _row(name, organization="openai"),
            ]
        )

        self.assertEqual([item["id"] for item in await self._listed()], [name])

    async def test_turning_the_flag_off_restores_the_upstream_whitelist(self) -> None:
        self._write([_row("lychee-v0"), _row("luna-lisa-alpha")])

        with patch.object(self.main, "ALLOW_STEALTH_IMAGE_MODELS", False):
            listed = [item["id"] for item in await self._listed()]

        self.assertEqual(listed, ["luna-lisa-alpha"])

    async def test_a_stealth_model_can_actually_be_requested(self) -> None:
        self._write([_row("lychee-v0")])

        response = await self._request("lychee-v0")

        self.assertNotEqual(response.status_code, 403)
        self.assertNotIn("stealth", response.text.lower())
        self.assertIn("PAST-THE-GUARDS", response.text)

    async def test_the_flag_off_makes_it_403_again(self) -> None:
        self._write([_row("lychee-v0")])

        with patch.object(self.main, "ALLOW_STEALTH_IMAGE_MODELS", False):
            response = await self._request("lychee-v0")

        self.assertEqual(response.status_code, 403)
        self.assertIn("stealth", response.text.lower())

    async def test_a_battle_only_model_is_still_404(self) -> None:
        self._write([_row("muse-image", organization="meta", userSelectable=False)])

        response = await self._request("muse-image")

        self.assertEqual(response.status_code, 404)


class TestTheStealthGate(unittest.TestCase):
    def setUp(self) -> None:
        from src import main

        self.main = main

    def test_by_default_every_name_passes(self) -> None:
        self.assertTrue(self.main.ALLOW_STEALTH_IMAGE_MODELS)
        self.assertTrue(self.main._stealth_model_allowed("lychee-v0"))
        self.assertTrue(self.main._stealth_model_allowed(""))

    def test_with_the_flag_off_only_the_whitelist_passes(self) -> None:
        with patch.object(self.main, "ALLOW_STEALTH_IMAGE_MODELS", False):
            self.assertTrue(self.main._stealth_model_allowed("luna-lisa-alpha"))
            self.assertFalse(self.main._stealth_model_allowed("lychee-v0"))
            self.assertFalse(self.main._stealth_model_allowed(None))

class TestThePluginListsTheNewestFirst(unittest.TestCase):
    @staticmethod
    def _entry(model_id: str, created_at: int | None = None) -> dict:
        entry = {"id": model_id, "output_image": True, "input_image": True}
        if created_at is not None:
            entry["created_at"] = created_at
        return entry

    def _plugin(self, entries: list[dict]):
        _, plugin = _make_plugin(self, {"model_cache_seconds": 0})

        class FakeClient:
            async def list_models(_self) -> list[dict]:
                return entries

        plugin._client = lambda: FakeClient()
        return plugin

    def test_the_newest_comes_first_and_undatable_rows_keep_arena_order(self) -> None:
        base = 1_770_000_000
        plugin = self._plugin(
            [
                self._entry("month-old", base - 30 * 86400),
                self._entry("legacy-a"),  # UUIDv4 id: the bridge sends created_at null
                self._entry("newest", base),
                self._entry("legacy-b"),
                self._entry("yesterday", base - 86400),
            ]
        )

        models = asyncio.run(plugin._fetch_models(force=True))

        self.assertEqual(
            [model["id"] for model in models],
            ["newest", "yesterday", "month-old", "legacy-a", "legacy-b"],
        )

    def test_the_number_you_type_matches_the_number_you_saw(self) -> None:
        base = 1_770_000_000
        plugin = self._plugin(
            [self._entry("older", base - 86400), self._entry("newest", base)]
        )

        selected, _ = asyncio.run(plugin._resolve_model(object(), "1"))

        self.assertEqual(selected["id"], "newest")


    def test_a_duplicated_name_shows_up_once_as_its_newest_row(self) -> None:
        """Arena ships five `gpt-image-2 (medium)` rows; one line is enough.

        Each row collapses to the same name in a request, so the duplicates only
        padded the list with identical lines under different numbers.
        """
        base = 1_770_000_000
        plugin = self._plugin(
            [
                self._entry("dup", base - 86400),
                self._entry("solo", base - 2 * 86400),
                self._entry("dup", base),
            ]
        )

        models = asyncio.run(plugin._fetch_models(force=True))

        self.assertEqual([model["id"] for model in models], ["dup", "solo"])
        self.assertEqual(models[0]["created_at"], base)


class TestThePluginSplitsTheModelList(unittest.TestCase):
    """Arena exposes 170+ image models, too many for one chat message.

    `/竞技场画图模型` keeps the branded rows and `/竞技场灰测模型` takes the
    gray-test ones, but both must keep printing the row's position in the full
    list -- otherwise `/竞技场切换模型 <number>` would resolve a different model
    than the one the number was printed next to.
    """

    @staticmethod
    def _entry(model_id: str, owner: str, created_at: int) -> dict:
        return {
            "id": model_id,
            "owned_by": owner,
            "created_at": created_at,
            "output_image": True,
            "input_image": True,
        }

    def _plugin(self):
        base = 1_770_000_000
        entries = [
            self._entry("stealth-newest", "lmarena", base),
            self._entry("gpt-image-2 (medium)", "openai", base - 86400),
            self._entry("luna-lisa-alpha", "", base - 2 * 86400),
            self._entry("nano-banana", "google", base - 3 * 86400),
        ]
        main, plugin = _make_plugin(self, {"model_cache_seconds": 0})

        class FakeClient:
            async def list_models(_self) -> list[dict]:
                return entries

            async def model_health(_self) -> dict:
                return {"models": []}

        plugin._client = lambda: FakeClient()
        return main, plugin

    def _text(self, plugin, command: str) -> str:
        from tests.test_arena_image_hardening import FakeEvent, _collect

        results = _collect(getattr(plugin, command)(FakeEvent()))
        self.assertEqual(len(results), 1)
        return results[0].text

    def test_the_branded_list_leaves_out_the_gray_test_rows(self) -> None:
        _, plugin = self._plugin()

        text = self._text(plugin, "list_models")

        self.assertIn("2. gpt-image-2 (medium)", text)
        self.assertIn("4. nano-banana", text)
        self.assertNotIn("stealth-newest", text)
        self.assertNotIn("luna-lisa-alpha", text)
        self.assertIn("竞技场画图模型（2 个，画图模型共 4 个）", text)

    def test_the_gray_test_list_keeps_the_positions_of_the_full_list(self) -> None:
        _, plugin = self._plugin()

        text = self._text(plugin, "list_stealth_models")

        # Position 1 and 3 of the full list: skipped numbers are intentional.
        self.assertIn("1. stealth-newest", text)
        self.assertIn("3. luna-lisa-alpha", text)
        self.assertNotIn("gpt-image-2", text)
        self.assertNotIn("nano-banana", text)

    def test_a_number_from_either_list_resolves_that_row(self) -> None:
        _, plugin = self._plugin()

        self._text(plugin, "list_stealth_models")
        selected, _ = asyncio.run(plugin._resolve_model(object(), "3"))

        self.assertEqual(selected["id"], "luna-lisa-alpha")

    def test_a_blank_owner_counts_as_gray_test(self) -> None:
        _, plugin = self._plugin()

        self.assertTrue(plugin._model_is_stealth({"owned_by": "lmarena"}))
        self.assertTrue(plugin._model_is_stealth({"owned_by": ""}))
        self.assertTrue(plugin._model_is_stealth({}))
        self.assertFalse(plugin._model_is_stealth({"owned_by": "openai"}))

    def test_each_list_points_at_the_other_one(self) -> None:
        _, plugin = self._plugin()

        self.assertIn("/竞技场灰测模型", self._text(plugin, "list_models"))
        self.assertIn("/竞技场画图模型", self._text(plugin, "list_stealth_models"))


if __name__ == "__main__":
    unittest.main()
