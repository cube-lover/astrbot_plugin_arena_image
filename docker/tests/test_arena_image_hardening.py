"""Regression tests for the 0.3.0 reliability fixes.

These cover the failures observed on the deployed instance: the 10 MB output
cap discarding finished generations, upstream rate limits that were never
retried, prose citations mistaken for images, and the verification link that
any group member could request.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import io
import logging
import random
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_arena_image import bridge_client

# The repository root is the AstrBot plugin directory itself (plugin-market layout).
PLUGIN_ROOT = Path(__file__).resolve().parents[2]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_DATA_DIR: list[str] = [""]


async def _no_sleep(seconds):  # noqa: ARG001 - drop-in for asyncio.sleep
    return None


class FakeResponse:
    def __init__(self, payload, *, status_code: int = 200, headers=None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class ScriptedAsyncClient:
    """httpx.AsyncClient stand-in that replays one queued response per call."""

    queue: list[FakeResponse] = []
    calls: list[tuple[str, str]] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method: str, url: str, **kwargs):  # noqa: ARG002
        type(self).calls.append((method, url))
        if len(type(self).queue) > 1:
            return type(self).queue.pop(0)
        return type(self).queue[0]


class StreamResponse:
    def __init__(self, chunks, *, status_code: int = 200, headers=None) -> None:
        self._chunks = list(chunks)
        self.status_code = status_code
        self.headers = headers or {}

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class StreamingClient:
    """httpx.AsyncClient stand-in for the generated-image download path."""

    response: StreamResponse | None = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str, **kwargs):  # noqa: ARG002
        return type(self).response


class _StubPermissionType:
    ADMIN = "admin"
    MEMBER = "member"


class _StubFilter:
    """Records the decorators main.py applies instead of registering handlers."""

    PermissionType = _StubPermissionType

    @staticmethod
    def command(name, alias=None, **kwargs):  # noqa: ARG004
        def decorator(func):
            func.arena_command = name
            func.arena_aliases = set(alias or ())
            return func

        return decorator

    @staticmethod
    def permission_type(value):
        def decorator(func):
            func.arena_permission = value
            return func

        return decorator


class _StubStar:
    def __init__(self, context=None, config=None) -> None:
        self.context = context
        self.config = config or {}


class _StubStarTools:
    @staticmethod
    def get_data_dir(name: str) -> str:  # noqa: ARG004
        return _DATA_DIR[0]


class _StubImage:
    """Minimal Image component: identity plus convert_to_base64()."""

    def __init__(
        self,
        file: str = "",
        url: str = "",
        base64_value: str = "",
        fail: bool = False,
    ) -> None:
        self.file = file
        self.url = url
        self._base64 = base64_value
        self._fail = fail

    @classmethod
    def fromURL(cls, url: str):  # noqa: N802 - mirrors AstrBot's component API
        return cls(url=url)

    @classmethod
    def fromFileSystem(cls, path: str):  # noqa: N802 - mirrors AstrBot's API
        return cls(file=path)

    async def convert_to_base64(self) -> str:
        if self._fail or not self._base64:
            raise RuntimeError("cannot read image bytes")
        return self._base64


def _install_astrbot_stubs() -> None:
    """Register a minimal astrbot surface so main.py is importable offline."""
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot.logger = logging.getLogger("arena-image-test")
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event_mod.filter = _StubFilter
    components = types.ModuleType("astrbot.api.message_components")
    components.At = type("At", (), {})
    components.Image = _StubImage
    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = type("Context", (), {})
    star_mod.Star = _StubStar
    star_mod.StarTools = _StubStarTools
    star_mod.register = lambda *args, **kwargs: (lambda cls: cls)  # noqa: ARG005
    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    core_star = types.ModuleType("astrbot.core.star")
    core_star.__path__ = []
    core_filter = types.ModuleType("astrbot.core.star.filter")
    core_filter.__path__ = []
    command_mod = types.ModuleType("astrbot.core.star.filter.command")
    command_mod.GreedyStr = type("GreedyStr", (str,), {})
    for name, module in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event_mod),
        ("astrbot.api.message_components", components),
        ("astrbot.api.star", star_mod),
        ("astrbot.core", core),
        ("astrbot.core.star", core_star),
        ("astrbot.core.star.filter", core_filter),
        ("astrbot.core.star.filter.command", command_mod),
    ):
        sys.modules.setdefault(name, module)


def _plugin_module():
    _install_astrbot_stubs()
    return importlib.import_module("astrbot_plugin_arena_image.main")


class FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.chain: list = []


class FakeEvent:
    def __init__(self, private: bool = False) -> None:
        self._private = private

    def plain_result(self, text: str) -> FakeResult:
        return FakeResult(text)

    def is_private_chat(self) -> bool:
        return self._private

    def get_group_id(self) -> str:
        return "" if self._private else "group-1"


def _make_plugin(test_case, config=None):
    main = _plugin_module()
    temp = tempfile.TemporaryDirectory()
    test_case.addCleanup(temp.cleanup)
    _DATA_DIR[0] = temp.name
    return main, main.ArenaImagePlugin(context=object(), config=dict(config or {}))


def _collect(agen):
    async def runner():
        return [item async for item in agen]

    return asyncio.run(runner())


def _noisy_png(size: int = 900) -> bytes:
    from PIL import Image as PILImage

    rnd = random.Random(20260903)
    image = PILImage.new("RGB", (size, size))
    image.putdata(
        [
            (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            for _ in range(size * size)
        ]
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class RateLimitRetryTest(unittest.TestCase):
    def test_detection_covers_status_code_error_code_and_chinese_text(self) -> None:
        self.assertTrue(
            bridge_client.BridgeError("slow down", status_code=429).is_rate_limited
        )
        self.assertTrue(
            bridge_client.BridgeError(
                "nope",
                payload={"error": {"code": "rate_limit_exceeded"}},
            ).is_rate_limited
        )
        self.assertTrue(
            bridge_client.BridgeError("请求过于频繁，请稍后再试").is_rate_limited
        )
        self.assertFalse(
            bridge_client.BridgeError("Invalid API Key.", status_code=401).is_rate_limited
        )

    def test_retry_after_is_read_from_headers_then_error_body(self) -> None:
        self.assertEqual(bridge_client._parse_retry_after("7"), 7.0)
        self.assertIsNone(bridge_client._parse_retry_after("soon"))
        self.assertIsNone(bridge_client._parse_retry_after("0"))
        self.assertEqual(
            bridge_client._retry_after_from(
                FakeResponse({}, headers={"retry-after": "12"}),
                {},
            ),
            12.0,
        )
        self.assertEqual(
            bridge_client._retry_after_from(
                FakeResponse({}, headers={"x-ratelimit-reset-after": "4"}),
                {},
            ),
            4.0,
        )
        self.assertEqual(
            bridge_client._retry_after_from(
                FakeResponse({}),
                {"error": {"retry_after": 9}},
            ),
            9.0,
        )

    def test_request_retries_and_waits_exactly_retry_after(self) -> None:
        ScriptedAsyncClient.calls.clear()
        ScriptedAsyncClient.queue = [
            FakeResponse(
                {"error": {"code": "rate_limit_exceeded", "message": "too many requests"}},
                status_code=429,
                headers={"retry-after": "3"},
            ),
            FakeResponse({"status": "ok"}),
        ]
        client = bridge_client.ArenaBridgeClient(
            "http://arena-bridge:8000/api/v1",
            rate_limit_retries=2,
        )
        delays: list[float] = []

        async def capture_sleep(seconds):
            delays.append(seconds)

        with (
            patch.object(bridge_client.httpx, "AsyncClient", ScriptedAsyncClient),
            patch.object(bridge_client.asyncio, "sleep", capture_sleep),
        ):
            payload = asyncio.run(client.health())

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(ScriptedAsyncClient.calls), 2)
        self.assertEqual(delays, [3.0])

    def test_request_gives_up_after_configured_retries_and_clamps_wait(self) -> None:
        ScriptedAsyncClient.calls.clear()
        ScriptedAsyncClient.queue = [
            FakeResponse({"detail": "Rate limit exceeded"}, status_code=429)
        ]
        client = bridge_client.ArenaBridgeClient(
            "http://arena-bridge:8000",
            rate_limit_retries=1,
            rate_limit_max_wait=5,
        )
        delays: list[float] = []

        async def capture_sleep(seconds):
            delays.append(seconds)

        with (
            patch.object(bridge_client.httpx, "AsyncClient", ScriptedAsyncClient),
            patch.object(bridge_client.asyncio, "sleep", capture_sleep),
            self.assertRaises(bridge_client.BridgeError) as caught,
        ):
            asyncio.run(client.health())

        self.assertEqual(len(ScriptedAsyncClient.calls), 2)
        self.assertEqual(delays, [5.0])
        self.assertTrue(caught.exception.is_rate_limited)

    def test_non_rate_limited_error_is_not_retried(self) -> None:
        ScriptedAsyncClient.calls.clear()
        ScriptedAsyncClient.queue = [
            FakeResponse({"detail": "Invalid API Key."}, status_code=401)
        ]
        client = bridge_client.ArenaBridgeClient("http://arena-bridge:8000")
        with (
            patch.object(bridge_client.httpx, "AsyncClient", ScriptedAsyncClient),
            patch.object(bridge_client.asyncio, "sleep", _no_sleep),
            self.assertRaises(bridge_client.BridgeError),
        ):
            asyncio.run(client.health())
        self.assertEqual(len(ScriptedAsyncClient.calls), 1)


class ImageUrlPreferenceTest(unittest.TestCase):
    def test_prose_citation_is_dropped_when_a_real_image_exists(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "content": (
                            "参考 https://lmarena.ai/docs/terms 之后生成：\n"
                            "![out](https://img.example/real.png)"
                        )
                    }
                }
            ]
        }
        self.assertEqual(
            bridge_client.image_urls(payload),
            ["https://img.example/real.png"],
        )

    def test_bare_link_with_image_suffix_is_a_strong_candidate(self) -> None:
        payload = {
            "choices": [
                {"message": {"content": "结果：https://img.example/fallback.jpg"}}
            ]
        }
        self.assertEqual(
            bridge_client.image_urls(payload),
            ["https://img.example/fallback.jpg"],
        )

    def test_prose_link_survives_only_as_last_resort(self) -> None:
        prose_only = {
            "choices": [{"message": {"content": "详见 https://lmarena.ai/leaderboard"}}]
        }
        self.assertEqual(
            bridge_client.image_urls(prose_only),
            ["https://lmarena.ai/leaderboard"],
        )

        with_structured = {
            "choices": [{"message": {"content": "详见 https://lmarena.ai/leaderboard"}}],
            "images": [{"url": "https://img.example/structured.png"}],
        }
        self.assertEqual(
            bridge_client.image_urls(with_structured),
            ["https://img.example/structured.png"],
        )

    def test_looks_like_image_url(self) -> None:
        self.assertTrue(bridge_client.looks_like_image_url("https://cdn.example/a/b.PNG"))
        self.assertTrue(bridge_client.looks_like_image_url("https://cdn.example/x.jpg?sig=k"))
        self.assertTrue(bridge_client.looks_like_image_url("data:image/png;base64,AAAA"))
        self.assertFalse(bridge_client.looks_like_image_url("https://example.com/page"))
        self.assertFalse(bridge_client.looks_like_image_url(""))


class OutputSizeLimitTest(unittest.TestCase):
    def test_input_output_and_send_limits_are_independent(self) -> None:
        main, plugin = _make_plugin(self)
        self.assertEqual(plugin._input_max_bytes(), 10 * 1024 * 1024)
        self.assertEqual(plugin._output_max_bytes(), main.DEFAULT_MAX_OUTPUT_IMAGE_BYTES)
        self.assertEqual(plugin._send_max_bytes(), main.DEFAULT_SEND_IMAGE_MAX_BYTES)
        self.assertGreater(plugin._output_max_bytes(), plugin._input_max_bytes())

        _, tuned = _make_plugin(
            self,
            {
                "max_image_bytes": 2 * 1024 * 1024,
                "max_output_image_bytes": 999 * 1024 * 1024,
                "send_image_max_bytes": 1024,
            },
        )
        self.assertEqual(tuned._input_max_bytes(), 2 * 1024 * 1024)
        self.assertEqual(tuned._output_max_bytes(), 256 * 1024 * 1024)
        self.assertEqual(tuned._send_max_bytes(), 256 * 1024)

    def test_download_accepts_images_over_the_old_10mb_cap(self) -> None:
        main, plugin = _make_plugin(self)
        payload = PNG_MAGIC + b"\x00" * (11 * 1024 * 1024)
        chunk = 1024 * 1024
        StreamingClient.response = StreamResponse(
            [payload[index : index + chunk] for index in range(0, len(payload), chunk)],
            headers={
                "content-type": "image/png",
                "content-length": str(len(payload)),
            },
        )
        with patch.object(main.httpx, "AsyncClient", StreamingClient):
            path = asyncio.run(plugin._materialize_output("https://img.example/big.png"))
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_size, len(payload))

    def test_oversized_download_names_the_output_config_key(self) -> None:
        main, plugin = _make_plugin(self, {"max_output_image_bytes": 65536})
        payload = PNG_MAGIC + b"\x00" * (256 * 1024)
        StreamingClient.response = StreamResponse(
            [payload],
            headers={
                "content-type": "image/png",
                "content-length": str(len(payload)),
            },
        )
        with (
            patch.object(main.httpx, "AsyncClient", StreamingClient),
            self.assertRaises(bridge_client.BridgeError) as caught,
        ):
            asyncio.run(plugin._materialize_output("https://img.example/big.png"))
        self.assertIn("max_output_image_bytes", str(caught.exception))


class ImageShrinkTest(unittest.TestCase):
    def test_images_below_the_limit_are_untouched(self) -> None:
        main = _plugin_module()
        raw = PNG_MAGIC + b"payload"
        self.assertEqual(
            main._shrink_image_bytes(raw, "image/png", 1024),
            (raw, "image/png"),
        )

    def test_undecodable_bytes_are_returned_unchanged(self) -> None:
        main = _plugin_module()
        raw = PNG_MAGIC + b"\x00" * 4096
        self.assertEqual(
            main._shrink_image_bytes(raw, "image/png", 16),
            (raw, "image/png"),
        )

    def test_large_image_is_reencoded_below_the_send_limit(self) -> None:
        main = _plugin_module()
        if main.PILImage is None:
            self.skipTest("Pillow is not installed")
        raw = _noisy_png()
        limit = 400 * 1024
        self.assertGreater(len(raw), limit)
        data, mime = main._shrink_image_bytes(raw, "image/png", limit)
        self.assertLessEqual(len(data), limit)
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_human_bytes_formatting(self) -> None:
        main = _plugin_module()
        self.assertEqual(main._human_bytes(64 * 1024 * 1024), "64.0 MB")
        self.assertEqual(main._human_bytes(512 * 1024), "512 KB")


class AdminAndPrivateChatTest(unittest.TestCase):
    ADMIN_ONLY = (
        "switch_model",
        "interactive_verify",
        "interactive_rebind",
        "interactive_verify_status",
    )
    OPEN_TO_MEMBERS = (
        "list_models",
        "unified_image",
        "text_to_image",
        "image_to_image",
        "status",
    )

    def test_state_changing_commands_require_admin(self) -> None:
        main = _plugin_module()
        for name in self.ADMIN_ONLY:
            handler = getattr(main.ArenaImagePlugin, name)
            self.assertEqual(
                getattr(handler, "arena_permission", None),
                main.filter.PermissionType.ADMIN,
                name,
            )

    def test_drawing_commands_stay_open_to_members(self) -> None:
        main = _plugin_module()
        for name in self.OPEN_TO_MEMBERS:
            handler = getattr(main.ArenaImagePlugin, name)
            self.assertIsNone(getattr(handler, "arena_permission", None), name)

    def test_verification_link_is_never_rendered_for_groups(self) -> None:
        main = _plugin_module()
        payload = {
            "status": "waiting",
            "browser_url": "https://vnc.example/session?token=PLACEHOLDER",
        }
        group_text = main.ArenaImagePlugin._interactive_auth_message(payload)
        self.assertNotIn("vnc.example", group_text)
        self.assertIn("私聊", group_text)
        private_text = main.ArenaImagePlugin._interactive_auth_message(
            payload,
            reveal_url=True,
        )
        self.assertIn(payload["browser_url"], private_text)

    def test_group_verify_refuses_before_touching_the_bridge(self) -> None:
        main, plugin = _make_plugin(self)

        def explode():
            raise AssertionError("a group request must not reach the bridge")

        plugin._client = explode
        results = _collect(plugin.interactive_verify(FakeEvent(private=False)))
        self.assertEqual(len(results), 1)
        self.assertIn("只在私聊发放", results[0].text)


class GenerationQueueTest(unittest.TestCase):
    PROMPT = "画一只猫"

    def test_full_queue_is_refused_without_calling_the_bridge(self) -> None:
        main, plugin = _make_plugin(self, {"max_queue_depth": 2})

        def explode():
            raise AssertionError("a refused request must not reach the bridge")

        plugin._client = explode
        plugin._active_generations = 2
        results = _collect(
            plugin._generate(FakeEvent(), self.PROMPT, include_input_images=False)
        )
        self.assertEqual(len(results), 1)
        self.assertIn("排队已满（2/2）", results[0].text)

    def test_queued_request_reports_eta_and_releases_the_slot(self) -> None:
        main, plugin = _make_plugin(self)
        plugin._active_generations = 1
        plugin._last_generation_seconds = 30.0

        def boom():
            raise RuntimeError("bridge offline")

        plugin._client = boom
        with patch.object(main.logger, "exception"):
            results = _collect(
                plugin._generate(
                    FakeEvent(),
                    self.PROMPT,
                    include_input_images=False,
                    model_id="gpt-image-2 (medium)",
                )
            )
        self.assertIn("前面还有 1 个出图任务", results[0].text)
        self.assertIn("预计还要等 30 秒左右", results[0].text)
        self.assertIn("生成失败", results[-1].text)
        self.assertEqual(plugin._active_generations, 1)

    def test_rate_limited_generation_returns_a_wait_hint(self) -> None:
        main, plugin = _make_plugin(self)

        def rate_limited():
            raise bridge_client.BridgeError(
                "Rate limit exceeded",
                status_code=429,
                retry_after=12,
            )

        plugin._client = rate_limited
        with patch.object(main.logger, "exception"):
            results = _collect(
                plugin._generate(
                    FakeEvent(),
                    self.PROMPT,
                    include_input_images=False,
                    model_id="gpt-image-2 (medium)",
                )
            )
        self.assertEqual(len(results), 1)
        self.assertIn("限流", results[0].text)
        self.assertIn("12 秒", results[0].text)
        self.assertIn("/竞技场画图模型", results[0].text)


class InputImageDedupTest(unittest.TestCase):
    def test_identical_pictures_from_two_components_are_sent_once(self) -> None:
        main, plugin = _make_plugin(self)
        same = base64.b64encode(PNG_MAGIC + b"same-bytes").decode("ascii")
        event = FakeEvent()
        event.message_obj = types.SimpleNamespace(
            message=[
                main.Image(file="/tmp/inline.png", base64_value=same),
                main.Image(file="/tmp/quoted.png", base64_value=same),
            ]
        )
        images = asyncio.run(plugin._collect_input_images(event))
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith("data:image/png;base64,"))

    def test_different_pictures_are_both_kept(self) -> None:
        main, plugin = _make_plugin(self)
        event = FakeEvent()
        event.message_obj = types.SimpleNamespace(
            message=[
                main.Image(
                    file="/tmp/a.png",
                    base64_value=base64.b64encode(PNG_MAGIC + b"a").decode("ascii"),
                ),
                main.Image(
                    file="/tmp/b.png",
                    base64_value=base64.b64encode(PNG_MAGIC + b"b").decode("ascii"),
                ),
            ]
        )
        images = asyncio.run(plugin._collect_input_images(event))
        self.assertEqual(len(images), 2)

    def test_unreadable_remote_image_falls_back_to_its_url_once(self) -> None:
        main, plugin = _make_plugin(self)
        event = FakeEvent()
        event.message_obj = types.SimpleNamespace(
            message=[
                main.Image(url="https://img.example/ref.png", fail=True),
                main.Image(url="https://img.example/ref.png", fail=True),
            ]
        )
        with patch.object(main.logger, "warning"):
            images = asyncio.run(plugin._collect_input_images(event))
        self.assertEqual(images, ["https://img.example/ref.png"])


class SchemaAndMetadataTest(unittest.TestCase):
    def test_new_config_keys_are_declared_with_matching_defaults(self) -> None:
        import json

        root = PLUGIN_ROOT
        schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
        main = _plugin_module()
        self.assertEqual(
            schema["max_output_image_bytes"]["default"],
            main.DEFAULT_MAX_OUTPUT_IMAGE_BYTES,
        )
        self.assertEqual(
            schema["send_image_max_bytes"]["default"],
            main.DEFAULT_SEND_IMAGE_MAX_BYTES,
        )
        for key in ("max_queue_depth", "rate_limit_retries", "rate_limit_max_wait"):
            self.assertIn(key, schema)
        self.assertIn("输入", schema["max_image_bytes"]["description"])
        metadata = (root / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 0.4.1", metadata)
        self.assertIn("author: cube-lover", metadata)

    def test_repository_root_is_the_installable_plugin_directory(self) -> None:
        """AstrBot installs the repo into ``data/plugins/<repo_name>`` and then
        requires ``metadata.yaml`` plus ``main.py`` at that top level, so the
        plugin entry point must never move back into a subdirectory."""
        import yaml

        for required in ("metadata.yaml", "main.py", "_conf_schema.json"):
            with self.subTest(file=required):
                self.assertTrue(
                    (PLUGIN_ROOT / required).is_file(),
                    f"{required} must sit at the repository root",
                )
        self.assertFalse(
            (PLUGIN_ROOT / "astrbot_plugin_arena_image").exists(),
            "the plugin must not be nested in a same-named subdirectory again",
        )

        metadata = yaml.safe_load((PLUGIN_ROOT / "metadata.yaml").read_text("utf-8"))
        for field in ("name", "desc", "version", "author"):
            with self.subTest(field=field):
                self.assertIsInstance(metadata.get(field), str)
                self.assertTrue(metadata[field].strip())
        self.assertEqual(metadata["name"], "astrbot_plugin_arena_image")
        self.assertTrue(metadata["display_name"].strip())
        self.assertEqual(metadata["cover"], "cover.png")
        self.assertTrue((PLUGIN_ROOT / metadata["cover"]).is_file())


if __name__ == "__main__":
    unittest.main()
