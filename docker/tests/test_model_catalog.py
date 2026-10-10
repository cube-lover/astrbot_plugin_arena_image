from __future__ import annotations

import ast
import asyncio
import copy
import importlib
import json
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
package = types.ModuleType("_arena_catalog_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
catalog = importlib.import_module(package.__name__ + ".model_catalog")
direct = importlib.import_module(package.__name__ + ".arena_direct")
bridge = importlib.import_module(package.__name__ + ".bridge_client")


def uid(number: int) -> str:
    return str(uuid.UUID(int=number))


def model(number=1, name="luna-lisa-alpha", *, owner=None, selectable=True):
    result = {
        "id": uid(number),
        "publicName": name,
        "userSelectable": selectable,
        "capabilities": {
            "inputCapabilities": {"text": True, "image": {"multipleImages": True}},
            "outputCapabilities": {"image": {"aspectRatios": ["1:1"]}},
        },
    }
    if owner:
        result["organization"] = owner
    return result


def entry(number, model_id, *, owner=None):
    return {
        "id": uid(number),
        "modelAId": model_id,
        "modelBId": None,
        "modelAOrganization": owner,
        "title": "PRIVATE PROMPT",
    }


def history(entries, cursor=None):
    return {
        "status": 200,
        "body": {
            "entries": entries,
            "pagination": {"hasMore": cursor is not None, "cursor": cursor},
        },
    }


def revealed(*models):
    return {"status": 200, "body": {"revealedModels": list(models)}}


class MetadataTests(unittest.TestCase):
    def test_private_session_fields_are_not_persisted(self):
        raw = model()
        raw.update(title="PRIVATE PROMPT", userId="PRIVATE USER", cookie="SECRET")
        cleaned = catalog.historical_image_model(raw)
        self.assertEqual(cleaned["id"], uid(1))
        self.assertTrue(cleaned["history_discovered"])
        self.assertTrue(cleaned["capabilities"]["inputCapabilities"]["image"]["multipleImages"])
        self.assertNotIn("PRIVATE", json.dumps(cleaned))
        self.assertNotIn("SECRET", json.dumps(cleaned))

    def test_rejects_public_nonimage_and_invalid_records(self):
        for value in (None, [], {}, model(owner="openai"), {**model(), "id": "bad"}):
            self.assertIsNone(catalog.historical_image_model(value))
        value = model()
        value["capabilities"]["outputCapabilities"] = {"text": True}
        self.assertIsNone(catalog.historical_image_model(value))

    def test_explicitly_disabled_model_is_not_enabled(self):
        value = catalog.historical_image_model(model(selectable=False))
        self.assertIs(value["userSelectable"], False)
        client = direct.ArenaDirectClient("http://test.invalid")
        self.assertFalse(client._keep_model(value))

    def test_nonboolean_selectability_is_rejected(self):
        self.assertIsNone(catalog.historical_image_model(model(selectable="false")))

    def test_current_public_identity_wins_over_history(self):
        store = catalog.HistoryModelCatalog()
        store.models[uid(1)] = catalog.historical_image_model(model())
        current = model(2, owner="openai")
        self.assertEqual(store.merge([current]), [current])

    def test_current_disabled_row_wins_over_historical_variant(self):
        store = catalog.HistoryModelCatalog()
        store.models[uid(1)] = catalog.historical_image_model(model())
        current = model(2, selectable=False)
        self.assertEqual(store.merge([current]), [current])

    def test_merge_retains_multiple_variant_ids_without_duplicates(self):
        store = catalog.HistoryModelCatalog()
        for number in (1, 2):
            store.models[uid(number)] = catalog.historical_image_model(model(number))
        rows = store.merge([store.models[uid(1)]])
        self.assertEqual({row["id"] for row in rows}, {uid(1), uid(2)})
        self.assertEqual(len(rows), 2)
        rows[0]["capabilities"].clear()
        self.assertTrue(store.models[uid(1)]["capabilities"])

    def test_persisted_cache_survives_restart_without_session_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            store = catalog.HistoryModelCatalog(path)
            store.models[uid(1)] = catalog.historical_image_model(model())
            store._cursor = "PRIVATE CURSOR"
            store._pending[uid(99)] = {uid(2)}
            store._save()
            text = path.read_text()
            self.assertNotIn("PRIVATE", text)
            self.assertNotIn(uid(99), text)
            self.assertEqual(catalog.HistoryModelCatalog(path).models, store.models)

    def test_corrupt_cache_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text("not JSON")
            self.assertEqual(catalog.HistoryModelCatalog(path).models, {})


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sleep_patch = patch.object(catalog.asyncio, "sleep", AsyncMock())
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.store = catalog.HistoryModelCatalog()

    async def test_same_model_in_many_sessions_only_reads_once(self):
        self.store._read = AsyncMock(side_effect=[
            history([entry(100 + n, uid(1)) for n in range(20)]),
            revealed(model()),
        ])
        status = await self.store.recover(None, [])
        self.assertEqual(self.store._read.await_count, 2)
        self.assertEqual(status["session_reads"], 1)
        self.assertTrue(status["history_scan_complete"])
        self.assertIn(uid(1), self.store.models)

    async def test_skips_models_already_in_current_catalog(self):
        self.store._read = AsyncMock(return_value=history([entry(100, uid(1))]))
        await self.store.recover(None, [model()])
        self.assertEqual(self.store._read.await_count, 1)
        self.assertFalse(self.store.models)

    async def test_pagination_resumes_without_repeating_newest_page(self):
        self.store._read = AsyncMock(side_effect=[
            history([entry(100, uid(1))], "next-page"),
            revealed(model()),
            history([entry(101, uid(2))]),
            revealed(model(2, "mosa-f")),
        ])
        await self.store.recover(None, [], max_pages=1)
        self.assertFalse(self.store.complete)
        await self.store.recover(None, [], force=True)
        self.assertIn("cursor=next-page", self.store._read.await_args_list[2].args[1])
        self.assertEqual(len(self.store.models), 2)
        self.assertTrue(self.store.complete)

    async def test_session_budget_retains_pending_work(self):
        self.store._read = AsyncMock(side_effect=[
            history([entry(100, uid(1)), entry(101, uid(2))]),
            revealed(model()),
            revealed(model(2, "mosa-f")),
        ])
        await self.store.recover(None, [], max_sessions=1)
        self.assertEqual(len(self.store.models), 1)
        await self.store.recover(None, [], force=True)
        self.assertEqual(len(self.store.models), 2)
        self.assertEqual(self.store._read.await_count, 3)

    async def test_empty_refresh_does_not_delete_recovered_models(self):
        self.store.models[uid(1)] = catalog.historical_image_model(model())
        self.store._read = AsyncMock(return_value=history([]))
        await self.store.recover(None, [])
        self.assertIn(uid(1), self.store.models)

    async def test_failures_preserve_cached_models(self):
        for status in (0, 401, 403, 500):
            with self.subTest(status=status):
                store = catalog.HistoryModelCatalog()
                store.models[uid(1)] = catalog.historical_image_model(model())
                store._read = AsyncMock(return_value={"status": status, "body": None})
                await store.recover(None, [])
                self.assertIn(uid(1), store.models)
                self.assertEqual(store._read.await_count, 1)
                self.assertGreater(store.next_refresh_at, time.time())

    async def test_429_stops_and_force_does_not_skip_retry_after(self):
        self.store._read = AsyncMock(return_value={"status": 429, "retry_after": "7200"})
        await self.store.recover(None, [])
        self.assertGreater(self.store.next_refresh_at, time.time() + 7190)
        await self.store.recover(None, [], force=True)
        self.assertEqual(self.store._read.await_count, 1)

    async def test_429_backoff_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            store = catalog.HistoryModelCatalog(path)
            store._read = AsyncMock(return_value={"status": 429, "retry_after": "7200"})
            await store.recover(None, [])
            restarted = catalog.HistoryModelCatalog(path)
            restarted._read = AsyncMock()
            await restarted.recover(None, [], force=True)
            restarted._read.assert_not_awaited()

    async def test_request_exception_is_nonfatal(self):
        self.store._read = AsyncMock(side_effect=RuntimeError("network error"))
        await self.store.recover(None, [])
        self.assertEqual(self.store.last_status, 0)
        self.assertGreater(self.store.next_refresh_at, time.time())

    async def test_session_404_does_not_stop_other_candidates(self):
        self.store._read = AsyncMock(side_effect=[
            history([entry(100, uid(1)), entry(101, uid(2))]),
            {"status": 404},
            revealed(model(2)),
        ])
        await self.store.recover(None, [])
        self.assertIn(uid(2), self.store.models)
        self.assertTrue(self.store.complete)

    async def test_metadata_success_does_not_mark_generation_health_success(self):
        before = copy.deepcopy(direct._HEALTH)
        self.store._read = AsyncMock(side_effect=[
            history([entry(100, uid(1))]), revealed(model())
        ])
        await self.store.recover(None, [])
        self.assertEqual(direct._HEALTH, before)
        self.assertNotIn("status_code", self.store.models[uid(1)])

    async def test_reader_is_get_only_and_rejects_generation_endpoint(self):
        page = types.SimpleNamespace(evaluate=AsyncMock(return_value=history([])))
        await self.store._read(page, "/api/history/unified?limit=50", 2)
        expression = page.evaluate.await_args.args[0]
        self.assertIn("method: 'GET'", expression)
        self.assertIn("AbortSignal.timeout(2000)", expression)
        self.assertNotIn("title:", expression)
        with self.assertRaises(ValueError):
            await self.store._read(page, "/nextjs-api/stream/create-evaluation", 2)


class DirectIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_models, self.old_time = direct._MODELS, direct._MODELS_AT
        direct._MODELS, direct._MODELS_AT = [], 0
        self.client = direct.ArenaDirectClient("http://test-integration.invalid")
        self.client._history_catalog = catalog.HistoryModelCatalog()
        self.client._history_catalog.models[uid(1)] = catalog.historical_image_model(model())
        self.client._history_catalog.next_refresh_at = time.time() + 1000
        self.public = [model(n + 10, f"public-{n}", owner="vendor") for n in range(10)]
        self.page = types.SimpleNamespace(model_table=AsyncMock(return_value=self.public))

    async def asyncTearDown(self):
        direct._MODELS, direct._MODELS_AT = self.old_models, self.old_time

    async def test_recovered_model_is_resolved_to_original_uuid(self):
        row, variants = await self.client._resolve(self.page, "luna-lisa-alpha")
        self.assertEqual(row["id"], uid(1))
        self.assertEqual(len(variants), 1)
        public = direct.public_model_entry(row)
        self.assertTrue(public["history_discovered"])
        self.assertEqual(public["owned_by"], "lmarena")
        self.assertTrue(public["input_image"])

    async def test_cached_public_refresh_keeps_history(self):
        await self.client._models(self.page)
        rows = await self.client._models(self.page)
        self.assertEqual(self.page.model_table.await_count, 1)
        self.assertIn(uid(1), {m["id"] for m in rows})
        self.assertNotIn(uid(1), {m["id"] for m in direct._MODELS})

    async def test_generation_resolution_does_not_scan_history(self):
        self.client._history_catalog.recover = AsyncMock()
        await self.client._resolve(self.page, "luna-lisa-alpha")
        self.client._history_catalog.recover.assert_not_awaited()

    async def test_discovery_can_continue_while_public_table_is_cached(self):
        self.client._history_catalog.recover = AsyncMock()
        await self.client._models(self.page, discover=True)
        await self.client._models(self.page, discover=True)
        self.assertEqual(self.page.model_table.await_count, 1)
        self.assertEqual(self.client._history_catalog.recover.await_count, 2)

    async def test_current_disabled_model_cannot_use_historical_variant(self):
        self.public.append(model(2, selectable=False))
        with self.assertRaises(bridge.BridgeError) as raised:
            await self.client._resolve(self.page, "luna-lisa-alpha")
        self.assertEqual(raised.exception.code, "model_not_found")


def command_class():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    source = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ArenaImagePlugin")
    wanted = {
        "_fetch_models", "_resolve_model", "_model_id", "_model_kind",
        "_model_created_text", "_health_age_text", "_model_health_text",
        "_model_is_stealth", "_model_list_text",
    }
    source.decorator_list = []
    source.bases = []
    source.body = [n for n in source.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]
    helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_as_int")
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        helper, source,
    ], type_ignores=[])
    namespace = {
        "time": time,
        "asyncio": asyncio,
        "GLOBAL_SELECTION_KEY": "__global__",
        "BridgeError": bridge.BridgeError,
        "model_created_at": bridge.model_created_at,
        "model_is_image_capable": bridge.model_is_image_capable,
    }
    exec(compile(ast.fix_missing_locations(module), "actual_plugin_commands", "exec"), namespace)
    return namespace["ArenaImagePlugin"]


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_gray_command_and_numeric_selection_share_restored_models(self):
        plugin = command_class()()
        plugin.config = {}
        plugin._models_cache = []
        plugin._models_cached_at = 0
        plugin._models_lock = asyncio.Lock()
        plugin._selected_models = {}
        rows = [
            direct.public_model_entry(model(10, "public", owner="vendor")),
            direct.public_model_entry(catalog.historical_image_model(model(1))),
            direct.public_model_entry(catalog.historical_image_model(model(2))),
        ]
        client = types.SimpleNamespace(list_models=AsyncMock(return_value=rows))
        plugin._client = lambda: client
        plugin._fetch_model_health = AsyncMock(return_value={})
        text = await plugin._model_list_text(None, stealth=True)
        self.assertIn("竞技场灰测模型（1 个", text)
        self.assertIn("2. luna-lisa-alpha", text)
        self.assertIn("[历史发现]", text)
        self.assertNotIn("✅", text)
        chosen, _ = await plugin._resolve_model(None, "2")
        self.assertEqual(chosen["id"], "luna-lisa-alpha")
        chosen, _ = await plugin._resolve_model(None, "LUNA-LISA-ALPHA")
        self.assertEqual(chosen["id"], "luna-lisa-alpha")


if __name__ == "__main__":
    unittest.main()
