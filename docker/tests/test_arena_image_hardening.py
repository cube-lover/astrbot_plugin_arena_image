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


class ForeignNetworkHintTest(unittest.TestCase):
    """A DNS failure on a container name means "wrong Docker network", not "down".

    Left as a bare httpx error it reads as an English traceback, and the operator
    starts restarting containers -- which is how NapCat logins get lost.
    """

    DNS = "[Errno -2] Name or service not known"

    def test_a_container_name_that_does_not_resolve_names_the_fix(self) -> None:
        hint = bridge_client._network_hint(
            RuntimeError(self.DNS), "http://arena-bridge:8000/api/v1/health"
        )
        self.assertIn("docker network connect", hint)
        self.assertIn("arena-bridge", hint)
        self.assertIn("astrbot", hint)
        self.assertIn("不用重启任何容器", hint)

    def test_other_failures_are_left_alone(self) -> None:
        # A timeout or a refused connection is a different problem; adding network
        # advice there would send people down the wrong path.
        self.assertEqual(
            bridge_client._network_hint(
                RuntimeError("timed out"), "http://arena-bridge:8000/api/v1/health"
            ),
            "",
        )
        # An address the operator typed themselves is not a container name.
        self.assertEqual(
            bridge_client._network_hint(
                RuntimeError(self.DNS), "http://10.0.0.9:8000/api/v1/health"
            ),
            "",
        )


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


class AnimatedInputFrameTest(unittest.TestCase):
    """GIF references reached Arena as animations, which failed the whole draw."""

    @staticmethod
    def _animated_gif(size: int = 24) -> bytes:
        from PIL import Image as PILImage

        frames = [PILImage.new("P", (size, size), color=index) for index in (1, 2, 3)]
        buffer = io.BytesIO()
        frames[0].save(
            buffer,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=80,
        )
        return buffer.getvalue()

    def _event(self, main, payload: bytes, name: str):
        event = FakeEvent()
        event.message_obj = types.SimpleNamespace(
            message=[
                main.Image(
                    file=name,
                    base64_value=base64.b64encode(payload).decode("ascii"),
                )
            ]
        )
        return event

    def test_animated_containers_are_detected(self) -> None:
        main = _plugin_module()
        self.assertTrue(main._is_animated_upload(b"GIF89a" + b"\x00" * 8, "image/gif"))
        self.assertTrue(main._is_animated_upload(PNG_MAGIC + b"\x00\x00\x00\x08acTL", "image/png"))
        self.assertTrue(
            main._is_animated_upload(b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"ANIM", "image/webp")
        )
        self.assertFalse(main._is_animated_upload(PNG_MAGIC + b"IDAT", "image/png"))
        self.assertFalse(main._is_animated_upload(b"\xff\xd8\xff\xe0", "image/jpeg"))
        self.assertFalse(
            main._is_animated_upload(b"RIFF\x00\x00\x00\x00WEBPVP8 still", "image/webp")
        )

    def test_first_frame_is_a_single_frame_png(self) -> None:
        main = _plugin_module()
        if main.PILImage is None:
            self.skipTest("Pillow is not installed")
        data, mime = main._first_frame_bytes(self._animated_gif(), "image/gif")
        self.assertEqual(mime, "image/png")
        self.assertTrue(data.startswith(PNG_MAGIC))
        with main.PILImage.open(io.BytesIO(data)) as flattened:
            self.assertEqual(getattr(flattened, "n_frames", 1), 1)
            self.assertEqual(flattened.size, (24, 24))

    def test_undecodable_animation_is_left_to_the_bridge(self) -> None:
        main = _plugin_module()
        raw = b"GIF89a" + b"\x00" * 32
        self.assertEqual(main._first_frame_bytes(raw, "image/gif"), (raw, "image/gif"))

    def test_gif_reference_is_uploaded_as_png(self) -> None:
        main, plugin = _make_plugin(self)
        if main.PILImage is None:
            self.skipTest("Pillow is not installed")
        event = self._event(main, self._animated_gif(), "/tmp/ref.gif")
        with patch.object(main.logger, "info"):
            images = asyncio.run(plugin._collect_input_images(event))
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith("data:image/png;base64,"))

    def test_still_png_reference_is_passed_through_untouched(self) -> None:
        main, plugin = _make_plugin(self)
        payload = PNG_MAGIC + b"still-bytes"
        event = self._event(main, payload, "/tmp/ref.png")
        images = asyncio.run(plugin._collect_input_images(event))
        encoded = base64.b64encode(payload).decode("ascii")
        self.assertEqual(images, [f"data:image/png;base64,{encoded}"])

    def test_apng_keeps_its_png_mime_and_is_not_reported_as_a_failure(self) -> None:
        main, plugin = _make_plugin(self)
        if main.PILImage is None:
            self.skipTest("Pillow is not installed")
        frames = [main.PILImage.new("RGB", (16, 16), color=(index * 40, 0, 0)) for index in (1, 2)]
        buffer = io.BytesIO()
        frames[0].save(buffer, format="PNG", save_all=True, append_images=frames[1:])
        payload = buffer.getvalue()
        self.assertTrue(main._is_animated_upload(payload, "image/png"))
        event = self._event(main, payload, "/tmp/ref.png")
        with (
            patch.object(main.logger, "info"),
            patch.object(main.logger, "warning") as warned,
        ):
            images = asyncio.run(plugin._collect_input_images(event))
        self.assertFalse(warned.called)
        encoded = base64.b64encode(payload).decode("ascii")
        self.assertNotIn(encoded, images[0])
        flattened = base64.b64decode(images[0].split(",", 1)[1])
        with main.PILImage.open(io.BytesIO(flattened)) as still:
            self.assertEqual(getattr(still, "n_frames", 1), 1)

    def test_without_pillow_the_gif_still_goes_out_with_a_warning(self) -> None:
        main, plugin = _make_plugin(self)
        event = self._event(main, b"GIF89a" + b"\x00" * 16, "/tmp/ref.gif")
        with (
            patch.object(main, "PILImage", None),
            patch.object(main.logger, "warning") as warned,
        ):
            images = asyncio.run(plugin._collect_input_images(event))
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith("data:image/gif;base64,"))
        self.assertTrue(warned.called)


class CDPExposureTest(unittest.TestCase):
    """CDP is unauthenticated control of a browser logged into a real account.

    It also reads local files inside the container, which is how the signing key
    is discovered, so publishing it on 0.0.0.0 hands over the Arena session and
    the link secret to anyone who scans the port.
    """

    def _compose(self) -> str:
        return (PLUGIN_ROOT / "docker" / "docker-compose.arena.yml").read_text(
            encoding="utf-8"
        )

    def test_the_cdp_port_defaults_to_loopback_and_is_never_wide_open(self) -> None:
        compose = self._compose()
        self.assertIn('"${ARENA_CDP_BIND:-127.0.0.1}:9223:9223"', compose)
        for line in compose.splitlines():
            if "9223:9223" in line:
                self.assertNotIn("0.0.0.0", line)

    def test_the_shipped_env_example_keeps_the_safe_default(self) -> None:
        env = (PLUGIN_ROOT / "docker" / ".env.example").read_text(encoding="utf-8")
        self.assertIn("ARENA_CDP_BIND=127.0.0.1", env)
        self.assertNotIn("ARENA_CDP_BIND=0.0.0.0", env)


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
        self.assertIn("version: 0.6.3", metadata)
        self.assertIn("author: cube-lover", metadata)
        # The @register version used to drift behind metadata.yaml, which made the
        # AstrBot console show a stale plugin version after an upgrade.
        source = (root / "main.py").read_text(encoding="utf-8")
        self.assertIn('    "0.6.3",\n)', source)

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
