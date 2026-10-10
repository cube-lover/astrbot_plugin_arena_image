"""Run the real in-page parser against synthetic Next.js responses (no network)."""

import asyncio
import json
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_arena_image import arena_direct as direct


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for JavaScript fixtures")
class ModelHtmlTests(unittest.TestCase):
    def run_parser(self, html, status=200):
        program = """
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
globalThis.fetch = async () => ({
  ok: input.status >= 200 && input.status < 300,
  status: input.status,
  text: async () => input.html
});
eval(input.expression).then(value => process.stdout.write(JSON.stringify(value)));
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", program],
            input=json.dumps({
                "html": html, "status": status,
                "expression": direct._MODEL_TABLE_JS % {"path": "/text/direct"},
            }),
            text=True, capture_output=True, encoding="utf-8", timeout=10,
            check=True,
        )
        return json.loads(result.stdout)

    @staticmethod
    def rows():
        return [{
            "id": "11111111-1111-4111-8111-111111111111",
            "publicName": '中文 ] "quoted" \\ model',
            "capabilities": {"outputCapabilities": {"image": {"ratios": ["1:1"]}}},
        }]

    @staticmethod
    def flight(chunks):
        return "".join(
            '<script nonce="fixture">self.__next_f.push(' +
            json.dumps([1, chunk], ensure_ascii=False) + ')</script>'
            for chunk in chunks
        )

    def assert_rows(self, html):
        result = self.run_parser(html)
        self.assertTrue(result["ok"], result)
        self.assertEqual(direct.parse_model_table(result["json"]), self.rows())

    def test_new_catalog_complete_field_after_array(self):
        self.assert_rows(self.flight([json.dumps({
            "initialModels": self.rows(), "initialCatalogComplete": False,
            "initialModelAId": None,
        }, ensure_ascii=False)]))

    def test_old_adjacent_id_field(self):
        self.assert_rows(self.flight([json.dumps({
            "initialModels": self.rows(), "initialModelAId": None,
        })]))

    def test_no_neighbor_id_field_required(self):
        self.assert_rows(json.dumps({"initialModels": self.rows(), "other": True}))

    def test_split_flight_array_and_split_field_name(self):
        text = '9:' + json.dumps({"initialModels": self.rows()}, ensure_ascii=False)
        self.assert_rows(self.flight([text[:12], text[12:56], text[56:]]))

    def test_nested_arrays_quotes_unicode_and_backslashes(self):
        self.assert_rows(self.flight([json.dumps({"initialModels": self.rows()})]))

    def test_malformed_or_missing_models_do_not_succeed(self):
        for text in ('{}', '{"initialModels":[]}', '{"initialModels":[{"id":"x"}]}',
                     '{"initialModels":[{"id":"x","publicName":"truncated"}'):
            self.assertFalse(self.run_parser(text)["ok"])

    def test_http_error_never_becomes_model_success(self):
        for status in (401, 403, 429, 500):
            result = self.run_parser(json.dumps({"initialModels": self.rows()}), status)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], status)

    def test_scripts_are_parsed_as_data_not_executed(self):
        html = '<script>throw new Error("must not execute");</script>'
        self.assert_rows(html + self.flight([json.dumps({"initialModels": self.rows()})]))


class PageParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_method_consumes_extracted_json(self):
        rows = ModelHtmlTests.rows()
        page = SimpleNamespace(evaluate=AsyncMock(return_value={
            "ok": True, "status": 200, "json": json.dumps(rows),
        }))
        self.assertEqual(await direct.ArenaPage.model_table(page), rows)
