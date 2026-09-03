from __future__ import annotations

import base64
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_arena_image import bridge_client

# The repository root is the AstrBot plugin directory itself (plugin-market layout).
PLUGIN_ROOT = Path(__file__).resolve().parents[2]

PNG_BYTES = b"\x89PNG\r\n\x1a\nfixture-image"
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


class FakeJsonResponse:
    def __init__(
        self,
        payload,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeAsyncClient:
    response: FakeJsonResponse = FakeJsonResponse({})
    instances: list[FakeAsyncClient] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.requests: list[tuple[str, str, dict]] = []
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method: str, url: str, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.response


class BridgeClientPureFunctionsTest(unittest.TestCase):
    def test_normalize_api_base(self) -> None:
        self.assertEqual(
            bridge_client.normalize_api_base("http://arena-bridge:8000"),
            "http://arena-bridge:8000/api/v1",
        )
        self.assertEqual(
            bridge_client.normalize_api_base("https://example.test/prefix/api/v1/"),
            "https://example.test/prefix/api/v1",
        )
        with self.assertRaises(ValueError):
            bridge_client.normalize_api_base("arena-bridge:8000")
        with self.assertRaises(ValueError):
            bridge_client.normalize_api_base("http://example.test/api?v=1")

    def test_image_data_decoding_and_size_limit(self) -> None:
        uri = bridge_client.data_uri_from_base64(PNG_B64)
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        raw, mime = bridge_client.decode_image_value(uri)
        self.assertEqual(raw, PNG_BYTES)
        self.assertEqual(mime, "image/png")

        raw_svg, svg_mime = bridge_client.decode_image_value(
            "data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3C%2Fsvg%3E"
        )
        self.assertTrue(raw_svg.startswith(b"<svg"))
        self.assertEqual(svg_mime, "image/svg+xml")

        with self.assertRaises(bridge_client.BridgeError):
            bridge_client.decode_image_value(PNG_BYTES, max_bytes=4)
        with self.assertRaises(bridge_client.BridgeError):
            bridge_client.decode_image_value("data:image/png")

    def test_image_urls_extracts_chat_and_generation_shapes(self) -> None:
        second_uri = bridge_client.data_uri_from_base64(PNG_B64)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "![one](https://img.example/one.png)",
                    }
                },
                {
                    "message": {
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://img.example/two.webp"},
                            },
                            {"type": "image", "image": "https://img.example/three.jpg"},
                            {"type": "text", "text": "caption"},
                        ]
                    }
                },
            ],
            "data": [{"b64_json": PNG_B64, "mime_type": "image/png"}],
            "images": [{"url": second_uri}],
        }

        urls = bridge_client.image_urls(payload)
        self.assertEqual(urls[0], "https://img.example/one.png")
        self.assertIn("https://img.example/two.webp", urls)
        self.assertIn("https://img.example/three.jpg", urls)
        self.assertEqual(sum(item.startswith("data:image/png;base64,") for item in urls), 1)

    def test_model_capability_variants(self) -> None:
        self.assertEqual(
            bridge_client.model_is_image_capable(
                {
                    "output_image": True,
                    "input_image": False,
                }
            ),
            (True, False),
        )
        self.assertEqual(
            bridge_client.model_is_image_capable(
                {
                    "capabilities": {
                        "output_capabilities": {"image": "true"},
                        "input_capabilities": {"image": 1},
                    }
                }
            ),
            (True, True),
        )

    def test_client_sends_auth_and_image_content(self) -> None:
        FakeAsyncClient.instances.clear()
        FakeAsyncClient.response = FakeJsonResponse({"choices": [{"message": {"content": "ok"}}]})
        client = bridge_client.ArenaBridgeClient(
            "http://arena-bridge:8000/api/v1",
            "TOKEN",
        )

        with patch.object(bridge_client.httpx, "AsyncClient", FakeAsyncClient):
            result = self._run(
                client.complete(
                    model="gpt-image-2 (medium)",
                    prompt="make it brighter",
                    images=[second_uri := bridge_client.data_uri_from_base64(PNG_B64)],
                )
            )

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        request_client = FakeAsyncClient.instances[0]
        self.assertEqual(
            request_client.kwargs["headers"]["Authorization"],
            "Bearer TOKEN",
        )
        body = request_client.requests[0][2]["json"]
        self.assertEqual(body["model"], "gpt-image-2 (medium)")
        self.assertEqual(body["messages"][0]["content"][1]["image_url"]["url"], second_uri)

    def test_client_maps_http_errors(self) -> None:
        FakeAsyncClient.instances.clear()
        FakeAsyncClient.response = FakeJsonResponse(
            {"detail": "invalid key"},
            status_code=401,
        )
        client = bridge_client.ArenaBridgeClient("http://arena-bridge:8000")
        with (
            patch.object(bridge_client.httpx, "AsyncClient", FakeAsyncClient),
            self.assertRaises(bridge_client.BridgeError) as caught,
        ):
            self._run(client.health())
        self.assertEqual(caught.exception.status_code, 401)
        self.assertIn("invalid key", str(caught.exception))

    def test_arena_auth_expiry_requests_rebind_but_bridge_key_error_does_not(self) -> None:
        expired = bridge_client.BridgeError(
            "Unauthorized: Your LMArena auth token has expired or is invalid.",
            payload={
                "error": {
                    "code": "http_401",
                    "message": "Unauthorized: Your LMArena auth token has expired or is invalid.",
                }
            },
        )
        self.assertTrue(expired.requires_interactive_auth)

        plugin_source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("/竞技场重新绑定", plugin_source)

        invalid_bridge_key = bridge_client.BridgeError(
            "Invalid API Key.",
            status_code=401,
            payload={"detail": "Invalid API Key."},
        )
        self.assertFalse(invalid_bridge_key.requires_interactive_auth)

    def test_internal_token_failure_does_not_ask_for_reverification(self) -> None:
        internal = bridge_client.BridgeError(
            "服务器浏览器会话仍然有效，但 Bridge 这次没能生成出图所需的一次性令牌（内部错误）。",
            status_code=503,
            payload={
                "detail": {
                    "code": "recaptcha_mint_failed",
                    "message": "内部错误，请稍后重试。",
                }
            },
        )
        self.assertFalse(internal.requires_interactive_auth)
        self.assertFalse(internal.is_rate_limited)

        # The same status without the explicit code still escalates.
        challenge = bridge_client.BridgeError(
            "Cloudflare challenge detected.",
            status_code=503,
            payload={"detail": {"code": "arena_verification_required"}},
        )
        self.assertTrue(challenge.requires_interactive_auth)

    def test_model_health_endpoint_contract(self) -> None:
        FakeAsyncClient.instances.clear()
        FakeAsyncClient.response = FakeJsonResponse(
            {
                "models": [
                    {"id": "gpt-image-2 (medium)", "status_code": 500, "checked_at": 1},
                    {"id": "ignored", "status_code": 0},
                ],
                "variants": [],
            }
        )
        client = bridge_client.ArenaBridgeClient(
            "http://arena-bridge:8000/api/v1",
            "TOKEN",
        )
        with patch.object(bridge_client.httpx, "AsyncClient", FakeAsyncClient):
            result = self._run(client.model_health())
        self.assertEqual(result["models"][0]["id"], "gpt-image-2 (medium)")
        self.assertEqual(result["models"][0]["status_code"], 500)

    @staticmethod
    def _run(awaitable):
        import asyncio

        return asyncio.run(awaitable)


class DockerEntrypointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).parents[1] / "docker-entrypoint.py"
        spec = importlib.util.spec_from_file_location("test_docker_entrypoint", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("failed to load docker-entrypoint.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.module = module

    def test_initialize_creates_config_and_non_json_key_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "LM_BRIDGE_DATA_DIR": temp_dir,
                    "LM_BRIDGE_ADMIN_PASSWORD": "ADMIN",
                    "LM_BRIDGE_API_KEY": "sk-test",
                    "LM_BRIDGE_RPM": "bad",
                },
                clear=False,
            ):
                self.module.initialize()

            root = Path(temp_dir)
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["password"], "ADMIN")
            self.assertEqual(config["api_keys"][0]["key"], "sk-test")
            self.assertEqual(config["api_keys"][0]["rpm"], 30)
            self.assertEqual(
                (root / "api-key.txt").read_text(encoding="utf-8"),
                "sk-test\n",
            )
            self.assertEqual(json.loads((root / "models.json").read_text()), [])

    def test_initialize_repairs_malformed_api_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.mkdir(exist_ok=True)
            (root / "config.json").write_text(
                json.dumps({"api_keys": [{"name": "broken"}], "auth_tokens": "bad"}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "LM_BRIDGE_DATA_DIR": temp_dir,
                    "LM_BRIDGE_API_KEY": "sk-repaired",
                },
                clear=False,
            ):
                self.module.initialize()
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["api_keys"][0]["key"], "sk-repaired")
            self.assertEqual(config["auth_tokens"], [])


class PluginMetadataTest(unittest.TestCase):
    def test_plugin_files_are_present_and_valid_json(self) -> None:
        root = PLUGIN_ROOT
        schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["default_model"]["default"], "gpt-image-2 (medium)")
        self.assertTrue(schema["bridge_api_key"]["secret"])
        metadata = (root / "metadata.yaml").read_text(encoding="utf-8")
        for field in ("name:", "desc:", "author:", "version:", "repo:"):
            self.assertIn(field, metadata)
        self.assertTrue((root / "requirements.txt").read_text(encoding="utf-8").strip())

    def test_unified_jjc_command_is_present_and_reports_start(self) -> None:
        source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@filter.command("jjc"', source)
        self.assertIn("开始{mode_text}", source)
        self.assertIn("include_input_images=is_image_to_image", source)
        self.assertIn("at-avatar:", source)
