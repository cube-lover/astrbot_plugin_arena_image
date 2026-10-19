from __future__ import annotations

import base64
import asyncio
import unittest

from aiohttp import FormData
from aiohttp.test_utils import AioHTTPTestCase

from astrbot_plugin_arena_image.openai_proxy import OpenAIProxyServer
from astrbot_plugin_arena_image.bridge_client import BridgeError


PNG = b"\x89PNG\r\n\x1a\nfixture"


class FakeClient:
    async def complete(self, *, model, prompt, images=None):
        self.last = (model, prompt, images)
        return {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}


class FakePlugin:
    config = {
        "openai_proxy_enabled": True,
        "openai_proxy_api_key": "test-key",
        "openai_proxy_host": "127.0.0.1",
        "openai_proxy_port": 18081,
        "default_model": "image-model",
        "max_output_images": 2,
        "max_image_bytes": 1024 * 1024,
    }

    def __init__(self):
        self.config = dict(type(self).config)
        self._selected_models = {}
        self.client = FakeClient()
        self._generation_lock = asyncio.Lock()
        self._active_generations = 0
        self._last_generation_seconds = 0.0

    async def _fetch_models(self):
        return [{"id": "image-model", "organization": "openai", "output_image": True}]

    @staticmethod
    def _model_id(model):
        return str(model.get("id") or "")

    def _input_max_bytes(self):
        return 1024 * 1024

    def _client(self):
        return self.client

    def _prune_outputs(self):
        return None

    async def _materialize_output(self, value):
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mktemp(suffix=".png"))
        path.write_bytes(PNG)
        return path


class OpenAIProxyTest(AioHTTPTestCase):
    async def get_application(self):
        self.plugin = FakePlugin()
        server = OpenAIProxyServer(self.plugin)
        self.proxy = server
        server._web = __import__("aiohttp.web", fromlist=["web"])
        app = server._web.Application()
        app.router.add_get("/v1/models", server._handle_models)
        app.router.add_post("/v1/images/generations", server._handle_generations)
        app.router.add_post("/v1/images/edits", server._handle_edits)
        return app

    async def test_requires_bearer_key(self):
        response = await self.client.get("/v1/models")
        self.assertEqual(response.status, 401)

    async def test_stream_failure_preserves_actual_arena_status_not_private_payload(self):
        class FailedClient:
            async def complete(self, **kwargs):
                raise BridgeError(
                    "Arena returned HTTP 200 but generation failed",
                    status_code=502,
                    payload={"error": {
                        "code": "stream_generation_failed",
                        "transport_status_code": 200,
                        "reported_upstream_status_code": None,
                        "private_cookie": "PRIVATE_FIXTURE",
                    }},
                )
        self.plugin.client = FailedClient()
        response = await self.client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "image-model", "prompt": "a red apple"},
        )
        self.assertEqual(response.status, 502)
        data = await response.json()
        self.assertEqual(data["error"]["code"], "stream_generation_failed")
        self.assertEqual(data["error"]["transport_status_code"], 200)
        self.assertIsNone(data["error"]["reported_upstream_status_code"])
        self.assertNotIn("PRIVATE_FIXTURE", str(data))

    async def test_real_gateway_failure_is_not_reclassified_as_stream_failure(self):
        class FailedClient:
            async def complete(self, **kwargs):
                raise BridgeError(
                    "502 Bad Gateway", status_code=502,
                    payload={"error": {"code": "http_502"}},
                )
        self.plugin.client = FailedClient()
        response = await self.client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "image-model", "prompt": "a red apple"},
        )
        self.assertEqual(response.status, 502)
        data = await response.json()
        self.assertEqual(data["error"]["code"], "upstream_error")
        self.assertNotIn("transport_status_code", data["error"])

    async def test_models_generation_and_json_edit(self):
        headers = {"Authorization": "Bearer test-key"}
        response = await self.client.get("/v1/models", headers=headers)
        self.assertEqual(response.status, 200)
        model_ids = [item["id"] for item in (await response.json())["data"]]
        self.assertIn("image-model", model_ids)

        response = await self.client.post(
            "/v1/images/generations",
            headers=headers,
            json={"prompt": "a test", "model": "image-model"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["data"][0]["mime_type"], "image/png")

        response = await self.client.post(
            "/v1/images/edits",
            headers=headers,
            json={
                "prompt": "edit",
                "model": "image-model",
                "images": [f"data:image/png;base64,{base64.b64encode(PNG).decode()}"],
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(len((await response.json())["data"]), 1)

    async def test_renamed_mona_model_alias_resolves_to_luna(self):
        self.plugin._fetch_models = lambda: _models_with_luna()
        self.proxy._web = __import__("aiohttp.web", fromlist=["web"])
        self.assertEqual(await self.proxy._resolve_model("mona-lisa-alpha"), "luna-lisa-alpha")
        response = await self.client.get("/v1/models", headers={"Authorization": "Bearer test-key"})
        self.assertEqual(response.status, 200)
        self.assertEqual(
            [item["id"] for item in (await response.json())["data"]], ["luna-lisa-alpha"]
        )

    async def test_multipart_edit(self):
        form = FormData()
        form.add_field("model", "image-model")
        form.add_field("prompt", "edit")
        form.add_field("image", PNG, filename="input.png", content_type="image/png")
        response = await self.client.post(
            "/v1/images/edits",
            headers={"Authorization": "Bearer test-key"},
            data=form,
        )
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["data"][0]["b64_json"])


async def _models_with_luna():
    return [{"id": "luna-lisa-alpha", "organization": "", "output_image": True}]


if __name__ == "__main__":
    unittest.main()
