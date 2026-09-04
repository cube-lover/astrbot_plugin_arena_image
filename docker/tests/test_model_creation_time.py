"""Where the model list's creation date comes from, and when it must stay silent.

Arena ships no `createdAt` field: the only timestamp in the model table is the
one embedded in a UUIDv7 `id`.  Roughly a tenth of the rows are UUIDv4 instead,
and decoding those random bytes yields years like 7220 -- so every consumer has
to distinguish "created then" from "no idea", and the plugin must render nothing
rather than a fabricated date.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from astrbot_plugin_arena_image import bridge_client
from tests.test_arena_image_hardening import _plugin_module


def _uuid7(when: datetime) -> str:
    """Build a UUIDv7 whose 48-bit prefix encodes ``when``, as Arena's ids do."""
    millis = int(when.timestamp() * 1000)
    tail = uuid.uuid4().hex[13:]
    raw = f"{millis:012x}7{tail}"
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def _image_model(model_id: str, public_name: str, **extra) -> dict:
    return {
        "id": model_id,
        "publicName": public_name,
        "organization": "openai",
        "capabilities": {
            "outputCapabilities": {"image": True},
            "inputCapabilities": {"image": True},
        },
        **extra,
    }


class TestBridgeDecodesTheModelId(unittest.TestCase):
    def setUp(self) -> None:
        from src import main

        self.main = main

    def test_a_uuid7_id_yields_its_creation_second(self) -> None:
        when = datetime(2026, 8, 19, 18, 26, 5, tzinfo=timezone.utc)

        created = self.main.model_created_timestamp({"id": _uuid7(when)})

        self.assertEqual(created, int(when.timestamp()))

    def test_a_uuid4_id_has_no_timestamp(self) -> None:
        # Arena's pre-migration rows: `gpt-image-1` decodes to the year 5820.
        self.assertIsNone(
            self.main.model_created_timestamp({"id": "69f90b32-4b1e-4b7a-9f4d-2c8f0b1d5a3e"})
        )

    def test_ids_that_cannot_be_a_timestamp_are_rejected(self) -> None:
        for model_id in (
            "",
            None,
            "image-internal",
            "not-a-uuid-at-all",
            "0000000000007fffffffffffffffffff",  # decodes to 1970
        ):
            with self.subTest(model_id=model_id):
                self.assertIsNone(self.main.model_created_timestamp({"id": model_id}))

    def test_a_future_id_is_rejected_as_random_bytes(self) -> None:
        far_future = datetime(2400, 1, 1, tzinfo=timezone.utc)

        self.assertIsNone(self.main.model_created_timestamp({"id": _uuid7(far_future)}))

    def test_the_iso_form_is_utc(self) -> None:
        when = datetime(2026, 9, 1, 4, 13, 0, tzinfo=timezone.utc)

        self.assertEqual(
            self.main._model_created_iso({"id": _uuid7(when)}),
            "2026-09-01T04:13:00Z",
        )


class TestModelsEndpointExposesCreationTime(unittest.IsolatedAsyncioTestCase):
    async def _list_models(self, models: list[dict]) -> list[dict]:
        from src import constants, main

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            models_path = root / "models.json"
            config_path.write_text(
                json.dumps({"api_keys": [{"name": "test", "key": "test-key", "rpm": 999}]}),
                encoding="utf-8",
            )
            models_path.write_text(json.dumps(models), encoding="utf-8")

            with (
                patch.object(main, "CONFIG_FILE", str(config_path)),
                patch.object(constants, "MODELS_FILE", str(models_path)),
            ):
                main.api_key_usage.clear()
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.get(
                        "/api/v1/models",
                        headers={"Authorization": "Bearer test-key"},
                    )

        self.assertEqual(response.status_code, 200)
        return response.json()["data"]

    async def test_created_at_is_the_row_creation_time(self) -> None:
        when = datetime(2026, 8, 19, 18, 26, 5, tzinfo=timezone.utc)

        data = await self._list_models([_image_model(_uuid7(when), "luna-lisa-alpha")])

        entry = next(item for item in data if item["id"] == "luna-lisa-alpha")
        self.assertEqual(entry["created_at"], int(when.timestamp()))
        # OpenAI clients require an int here, so `created` mirrors it.
        self.assertEqual(entry["created"], int(when.timestamp()))

    async def test_an_undatable_model_reports_null_not_today(self) -> None:
        data = await self._list_models([_image_model("legacy-internal-id", "gpt-image-1")])

        entry = next(item for item in data if item["id"] == "gpt-image-1")
        self.assertIsNone(entry["created_at"])
        # The OpenAI-compatible field still has to be an int.
        self.assertAlmostEqual(entry["created"], int(time.time()), delta=120)

    async def test_the_existing_capability_contract_still_holds(self) -> None:
        data = await self._list_models(
            [_image_model(_uuid7(datetime.now(timezone.utc)), "gpt-image-2 (medium)")]
        )

        entry = next(item for item in data if item["id"] == "gpt-image-2 (medium)")
        self.assertTrue(entry["output_image"])
        self.assertTrue(entry["input_image"])
        self.assertEqual(entry["owned_by"], "openai")


class TestPluginRendersTheCreationDate(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = _plugin_module().ArenaImagePlugin

    def test_created_at_is_read_and_created_is_ignored(self) -> None:
        stamp = int(datetime(2026, 8, 19, 18, 26, 5, tzinfo=timezone.utc).timestamp())

        self.assertEqual(bridge_client.model_created_at({"created_at": stamp}), stamp)
        self.assertEqual(bridge_client.model_created_at({"createdAt": str(stamp)}), stamp)
        # Bridges older than 0.4.4 send `created: now` for every model; trusting
        # it would date the whole list to today.
        self.assertIsNone(bridge_client.model_created_at({"created": int(time.time())}))
        for payload in ({}, {"created_at": None}, {"created_at": ""}, {"created_at": "soon"}):
            with self.subTest(payload=payload):
                self.assertIsNone(bridge_client.model_created_at(payload))

    def test_the_list_line_carries_the_date(self) -> None:
        stamp = int(datetime(2026, 8, 19, 18, 26, 5, tzinfo=timezone.utc).timestamp())
        expected = time.strftime("%Y-%m-%d", time.localtime(stamp))

        rendered = self.plugin._model_created_text({"created_at": stamp})

        self.assertEqual(rendered, f" 创建 {expected}")

    def test_an_undatable_model_renders_nothing(self) -> None:
        self.assertEqual(self.plugin._model_created_text({"created": int(time.time())}), "")
        self.assertEqual(self.plugin._model_created_text({}), "")


if __name__ == "__main__":
    unittest.main()
