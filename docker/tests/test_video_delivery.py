"""Offline safety/receipt tests. No sockets, QQ calls or real media-tool launches.

AstrBot is not installed in this test environment: the Video fixture models its
native fromFileSystem + to_dict file-URI preservation, not live QQ acceptance.
"""
import asyncio
import base64
import copy
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_arena_image import video_delivery as delivery


MP4 = (24).to_bytes(4, "big") + b"ftypisom" + b"\0\0\0\0" + b"isommp42" + (12).to_bytes(4, "big") + b"mdatDATA"
SIGNED_URL = "https://storage.example/video.mp4?token=PRIVATE_SIGNED_VALUE"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
REJECTION = RuntimeError('EventChecker failed NodeIKernelMsgService/sendMsg {"result": 120}')


class FakeContent:
    def __init__(self, chunks, delay=0, error=None):
        self.chunks = chunks
        self.delay = delay
        self.error = error
        self.started = asyncio.Event()

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            self.started.set()
            await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            yield chunk


class FakeResponse:
    def __init__(self, chunks=None, mime="video/mp4", status=200, headers=None, delay=0, error=None):
        self.status = status
        self.headers = {"Content-Type": mime, **(headers or {})}
        self.content = FakeContent([MP4] if chunks is None else chunks, delay, error)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.get = Mock(return_value=response)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class MaterializationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT, prefix=".test-video-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.keep = self.root / "unrelated.txt"
        self.keep.write_text("keep")
        self.response = FakeResponse()
        self.session = FakeSession(self.response)
        self.resolver = SimpleNamespace(close=AsyncMock())
        self.session_factory = Mock(return_value=self.session)
        self.connector = Mock()
        for target, value in (
            ("aiohttp.ClientSession", self.session_factory),
            ("aiohttp.TCPConnector", self.connector),
            ("_PublicResolver", Mock(return_value=self.resolver)),
            ("shutil.which", Mock(side_effect=lambda name: name + "-fixture")),
        ):
            patcher = patch.object(delivery, target, value) if "." not in target else patch(
                "astrbot_plugin_arena_image.video_delivery." + target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def response_with(self, **kwargs):
        self.response = FakeResponse(**kwargs)
        self.session.response = self.response
        self.session.get.return_value = self.response

    def assert_clean(self):
        self.assertEqual(list(self.root.iterdir()), [self.keep])
        self.assertEqual(self.keep.read_text(), "keep")

    async def assert_failure(self, code, **kwargs):
        with self.assertRaises(delivery.VideoMaterializationError) as caught:
            await delivery.materialize_video_output(SIGNED_URL, self.root, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assert_clean()
        return caught.exception

    async def test_success_streams_scoped_random_name_no_signed_url(self):
        self.response_with(chunks=[MP4[:5], MP4[5:17], MP4[17:]],
                           headers={"Content-Length": str(len(MP4))})
        path = await delivery.materialize_video_output(
            {"url": SIGNED_URL, "mimeType": "video/mp4", "name": "prompt-secret.mp4"}, self.root)
        self.assertEqual(path.read_bytes(), MP4)
        self.assertRegex(path.name, r"^[a-f0-9]{32}\.mp4$")
        self.assertEqual(path.parent.parent, self.root)
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertTrue(self.response.closed)
        self.assertTrue(self.session.closed)
        self.resolver.close.assert_awaited_once()
        self.assertIs(self.connector.call_args.kwargs["resolver"], self.resolver)
        self.assertFalse(self.connector.call_args.kwargs["use_dns_cache"])
        session_options = self.session_factory.call_args.kwargs
        self.assertFalse(session_options["trust_env"])
        self.assertFalse(session_options["auto_decompress"])
        self.assertIsInstance(session_options["cookie_jar"], delivery.aiohttp.DummyCookieJar)
        self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])

    async def test_invalid_and_unsafe_urls_never_open_client(self):
        for url in (
            "http://storage.example/video.mp4", "file:///private.mp4", "https://u:p@storage.example/a",
            "https://storage.example/a#fragment", "https://storage.example:0/a",
            "https://storage.example:/a", "https://storage.example/a\n", "https://storage.example/a%0D",
            "https://storage.example/a%GG", "https://storage.example\\@elsewhere.example/a",
            "https://127.0.0.1/a", "https://10.0.0.1/a", "https://192.168.1.2/a",
            "https://169.254.169.254/a", "https://100.64.0.1/a", "https://0.0.0.0/a",
            "https://224.0.0.1/a", "https://[::1]/a", "https://[fe80::1]/a",
            "https://[::ffff:8.8.8.8]/a", "https://[2002:0808:0808::1]/a",
            "https://localhost/a", "https://host.local/a", "https://host.internal/a",
            "https://storage.example/" + "a" * 16384, {}, {"url": SIGNED_URL, "mimeType": "image/png"},
        ):
            with self.subTest(url_type=type(url).__name__):
                with self.assertRaises(delivery.VideoMaterializationError):
                    await delivery.materialize_video_output(url, self.root)
                self.assert_clean()
        self.session_factory.assert_not_called()

    async def test_invalid_limits_fail_before_download(self):
        for options in ({"max_bytes": 0}, {"max_bytes": True}, {"max_bytes": 65 * 1024 * 1024},
                        {"timeout": float("nan")}, {"timeout": 0}, {"timeout": float("inf")},
                        {"ffprobe": object()}):
            with self.subTest(options=options):
                await self.assert_failure("video_invalid_input", **options)
        self.session_factory.assert_not_called()

    async def test_mime_redirect_status_and_encoding_fail_closed(self):
        for mime in ("text/html", "application/octet-stream", "video/webm", ""):
            self.response_with(mime=mime)
            await self.assert_failure("video_invalid_mime")
        self.session.get.reset_mock()
        for status in (301, 302, 307, 308, 404, 500):
            self.response_with(status=status, headers={"Location": "https://127.0.0.1/private"})
            await self.assert_failure("video_download_failed")
            self.assertEqual(self.session.get.call_count, 1)
            self.session.get.reset_mock()
        self.response_with(headers={"Content-Encoding": "gzip"})
        await self.assert_failure("video_invalid_mp4")

    async def test_size_limit_declared_and_actual(self):
        self.response_with(headers={"Content-Length": "100"})
        await self.assert_failure("video_size_limit", max_bytes=len(MP4))
        self.response_with(chunks=[MP4, b"x"])
        await self.assert_failure("video_size_limit", max_bytes=len(MP4))
        self.response_with(headers={"Content-Length": str(len(MP4))})
        path = await delivery.materialize_video_output(SIGNED_URL, self.root, max_bytes=len(MP4))
        self.assertEqual(path.stat().st_size, len(MP4))

    async def test_invalid_ftyp_empty_truncated_mismatch(self):
        for body in (b"", b"not an MP4", b"\0\0\0\0ftypisom" + b"x" * 32,
                     MP4.replace(b"ftyp", b"moov", 1), MP4[:24],
                     MP4.replace(b"isom", b"zzzz").replace(b"mp42", b"zzzz")):
            self.response_with(chunks=[body])
            await self.assert_failure("video_invalid_mp4")
        for declared in ("0", "garbage", "-1", str(len(MP4) + 1)):
            self.response_with(headers={"Content-Length": declared})
            await self.assert_failure("video_invalid_mp4")

    async def test_timeout_download_failure_and_cancel_cleanup(self):
        self.response_with(delay=1)
        await self.assert_failure("video_download_timeout", timeout=0.01)
        self.response_with(error=RuntimeError(SIGNED_URL))
        await self.assert_failure("video_download_failed")
        self.response_with(delay=30)
        task = asyncio.create_task(delivery.materialize_video_output(SIGNED_URL, self.root))
        await self.response.content.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_clean()
        self.assertTrue(self.response.closed)
        self.resolver.close.assert_awaited()

    async def test_ffprobe_opt_in_validates_and_uses_safe_arguments(self):
        process = FakeProcess(probe_json())
        decode = FakeProcess(b"")
        with patch.object(delivery.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[process, decode])) as spawn:
            path = await delivery.materialize_video_output(SIGNED_URL, self.root, ffprobe="ffprobe-fixture")
        self.assertTrue(path.exists())
        self.assertEqual(spawn.await_count, 2)
        probe_call, decode_call = spawn.call_args_list
        args = probe_call.args
        self.assertEqual(args[0], "ffprobe-fixture")
        self.assertIn("file", args)
        self.assertIn("mov", args)
        self.assertNotIn(SIGNED_URL, args)
        self.assertEqual(probe_call.kwargs["stderr"], asyncio.subprocess.DEVNULL)
        args = decode_call.args
        self.assertEqual(args[0], "ffmpeg-fixture")
        for flag, value in (("-v", "error"), ("-err_detect", "explode"),
                            ("-protocol_whitelist", "file"), ("-format_whitelist", "mov"),
                            ("-enable_drefs", "0"), ("-use_absolute_path", "0"),
                            ("-map", "0:v:0"), ("-f", "mov"), ("-threads", "1"),
                            ("-abort_on", "empty_output+empty_output_stream")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("-xerror", args)
        self.assertIn("-nostdin", args)
        self.assertEqual(args[-3:], ("-f", "null", "-"))
        self.assertNotIn("-t", args)
        self.assertNotIn("-frames:v", args)
        self.assertNotIn(SIGNED_URL, args)
        self.assertEqual(args[args.index("-i") + 1], probe_call.args[probe_call.args.index("-i") + 1])
        self.assertEqual(decode_call.kwargs["stdout"], asyncio.subprocess.DEVNULL)
        self.assertEqual(decode_call.kwargs["stderr"], asyncio.subprocess.PIPE)
        self.assertLessEqual(delivery.FFMPEG_DECODE_TIMEOUT, 30)
        self.assertFalse(process.killed)
        self.assertFalse(decode.killed)

    async def test_ffprobe_invalid_exit_json_metadata_output_and_missing_executable(self):
        for raw, returncode in ((b"not json", 0), (probe_json(), 1), (b"x" * (delivery.MAX_PROBE_BYTES + 1), 0),
                                (probe_json(width=0), 0), (probe_json(height=-1), 0),
                                (probe_json(duration="NaN"), 0), (probe_json(duration="Infinity"), 0),
                                (probe_json(duration="181"), 0), (probe_json(duration="0"), 0),
                                (probe_json(codec_type="audio"), 0),
                                (json.dumps({"streams": []}).encode(), 0)):
            with patch.object(delivery.asyncio, "create_subprocess_exec",
                              AsyncMock(return_value=FakeProcess(raw, returncode))):
                await self.assert_failure("video_probe_failed", ffprobe="fixture")
        with patch.object(delivery.shutil, "which", return_value=None):
            await self.assert_failure("video_probe_failed", ffprobe=True)
        with patch.object(delivery.asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError(SIGNED_URL))):
            await self.assert_failure("video_probe_failed", ffprobe="fixture")

    async def test_probe_timeout_and_cancellation_kill_and_reap_child(self):
        for cancel in (False, True):
            process = FakeProcess(b"", hang=True)
            with patch.object(delivery.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), \
                    patch.object(delivery, "FFPROBE_TIMEOUT", 0.02):
                task = asyncio.create_task(delivery.materialize_video_output(SIGNED_URL, self.root, ffprobe="fixture"))
                await process.started.wait()
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaises(delivery.VideoMaterializationError) as caught:
                        await task
                    self.assertEqual(caught.exception.code, "video_probe_timeout")
            self.assertTrue(process.killed)
            self.assertGreaterEqual(process.wait_calls, 1)
            self.assert_clean()

    async def test_ffprobe_true_parent_signature_and_spawn_cancellation_cleanup(self):
        process = FakeProcess(probe_json())
        with patch.object(delivery.shutil, "which", side_effect=lambda name: name + "-fixture"), \
                patch.object(delivery.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[process, FakeProcess(b"")])):
            path = await delivery.materialize_video_output(
                {"url": SIGNED_URL, "contentType": "video/mp4"}, output_dir=self.root,
                max_bytes=delivery.DEFAULT_MAX_VIDEO_BYTES, timeout=90, ffprobe=True)
        self.assertTrue(path.exists())
        # Remove only the successful output owned by this test before cancellation.
        path.unlink()
        path.parent.rmdir()
        process = FakeProcess(b"", hang=True)
        started = asyncio.Event()

        async def spawn(*args, **kwargs):
            started.set()
            await asyncio.sleep(0.01)
            return process

        with patch.object(delivery.asyncio, "create_subprocess_exec", spawn):
            task = asyncio.create_task(delivery.materialize_video_output(SIGNED_URL, self.root, ffprobe="fixture"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.killed)
        self.assertGreaterEqual(process.wait_calls, 1)
        self.assert_clean()

    async def test_metadata_success_corrupt_samples_fail_decode_and_clean(self):
        # ftyp/length/ffprobe metadata all succeed; actual sample decoding fails.
        # Even an exit-0 decoder with loglevel=error diagnostics is rejected.
        for returncode, stderr in ((1, b"corrupt sample " + SIGNED_URL.encode()),
                                   (0, b"partial file at offset 5000"),
                                   (1, b""), (0, b"x" * (delivery.MAX_DECODE_STDERR_BYTES + 1))):
            probe = FakeProcess(probe_json())
            decode = FakeProcess(b"", returncode=returncode, stderr=stderr)
            with patch.object(delivery.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[probe, decode])) as spawn:
                error = await self.assert_failure("video_decode_failed", ffprobe="fixture")
            self.assertEqual(spawn.await_count, 2)
            self.assertEqual(probe.wait_calls, 1)
            self.assertNotIn("offset", str(error))
            self.assertNotIn("corrupt sample", str(error))

    async def test_missing_decoder_fails_before_download_and_spawn(self):
        with patch.object(delivery.shutil, "which", side_effect=lambda name: "probe-fixture" if name == "ffprobe" else None), \
                patch.object(delivery.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
            await self.assert_failure("video_decode_failed", ffprobe=True)
        self.session_factory.assert_not_called()
        spawn.assert_not_awaited()

    async def test_decoder_spawn_failure_is_opaque_and_cleans(self):
        with patch.object(delivery.asyncio, "create_subprocess_exec",
                          AsyncMock(side_effect=[FakeProcess(probe_json()), OSError(SIGNED_URL)])):
            await self.assert_failure("video_decode_failed", ffprobe="fixture")

    async def test_decode_timeout_cancel_and_outer_deadline_kill_reap_clean(self):
        for mode in ("decode-timeout", "cancel", "outer-timeout"):
            probe = FakeProcess(probe_json())
            decode = FakeProcess(b"", hang=True)
            deadline = 0.1 if mode == "decode-timeout" else 10
            outer = 0.1 if mode == "outer-timeout" else 90
            with patch.object(delivery.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[probe, decode])), \
                    patch.object(delivery, "FFMPEG_DECODE_TIMEOUT", deadline):
                task = asyncio.create_task(delivery.materialize_video_output(
                    SIGNED_URL, self.root, timeout=outer, ffprobe="fixture"))
                await asyncio.wait_for(decode.started.wait(), 1)
                if mode == "cancel":
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaises(delivery.VideoMaterializationError) as caught:
                        await task
                    self.assertEqual(caught.exception.code, "video_decode_timeout" if mode == "decode-timeout"
                                     else "video_download_timeout")
            self.assertTrue(decode.killed)
            self.assertGreaterEqual(decode.wait_calls, 1)
            self.assert_clean()

    async def test_decoder_cancellation_during_spawn_does_not_orphan_child(self):
        decode = FakeProcess(b"", hang=True)
        started = asyncio.Event()
        spawn_calls = 0

        async def spawn(*args, **kwargs):
            nonlocal spawn_calls
            spawn_calls += 1
            if spawn_calls == 1:
                return FakeProcess(probe_json())
            started.set()
            await asyncio.sleep(0.01)
            return decode

        with patch.object(delivery.asyncio, "create_subprocess_exec", spawn):
            task = asyncio.create_task(delivery.materialize_video_output(SIGNED_URL, self.root, ffprobe="fixture"))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(spawn_calls, 2)
        self.assertTrue(decode.killed)
        self.assertGreaterEqual(decode.wait_calls, 1)
        self.assert_clean()

    async def test_stderr_overflow_kills_running_decoder(self):
        decode = FakeProcess(b"", hang=True)
        decode.stderr.feed_data(b"x" * (delivery.MAX_DECODE_STDERR_BYTES + 1))
        with patch.object(delivery.asyncio, "create_subprocess_exec",
                          AsyncMock(side_effect=[FakeProcess(probe_json()), decode])):
            await self.assert_failure("video_decode_failed", ffprobe="fixture")
        self.assertTrue(decode.killed)
        self.assertGreaterEqual(decode.wait_calls, 1)

    async def test_sibling_ffmpeg_discovery(self):
        sibling = self.root / ("ffmpeg.exe" if delivery.os.name == "nt" else "ffmpeg")
        sibling.write_bytes(b"test executable placeholder")
        probe = self.root / ("ffprobe.exe" if delivery.os.name == "nt" else "ffprobe")
        with patch.object(delivery.os, "access", return_value=True), \
                patch.object(delivery.shutil, "which", return_value=None) as which:
            self.assertEqual(delivery._ffmpeg_executable(str(probe)), str(sibling))
        which.assert_not_called()

    async def test_optional_probe_has_no_subprocess(self):
        with patch.object(delivery.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
            await delivery.materialize_video_output(SIGNED_URL, self.root)
        spawn.assert_not_awaited()


class ResolverTest(unittest.IsolatedAsyncioTestCase):
    async def test_guard_rejects_mixed_dns_results_and_passes_only_public_addresses(self):
        underlying = SimpleNamespace(resolve=AsyncMock(), close=AsyncMock())
        with patch.object(delivery.aiohttp.resolver, "DefaultResolver", return_value=underlying):
            resolver = delivery._PublicResolver()
        public = {"host": "8.8.8.8", "hostname": "storage.example", "port": 443,
                  "family": socket.AF_INET, "proto": 0, "flags": 0}
        for bad in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "ff02::1", "100.64.0.1"):
            underlying.resolve.return_value = [public, {**public, "host": bad}]
            with self.assertRaises(delivery.VideoMaterializationError):
                await resolver.resolve("storage.example", 443)
        underlying.resolve.return_value = []
        with self.assertRaises(delivery.VideoMaterializationError):
            await resolver.resolve("storage.example", 443)
        underlying.resolve.return_value = [public]
        self.assertEqual(await resolver.resolve("storage.example", 443), [public])
        await resolver.close()
        underlying.close.assert_awaited_once()


def probe_json(**kwargs):
    return json.dumps({"streams": [{"codec_type": "video", "width": 512, "height": 288,
                                   "duration": "5.5", **kwargs}], "format": {"duration": "5.5"}}).encode()


class FakeProcess:
    def __init__(self, raw, returncode=0, hang=False, stderr=b""):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        if not hang:
            self.stdout.feed_data(raw)
            self.stdout.feed_eof()
            self.stderr.feed_data(stderr)
            self.stderr.feed_eof()
        self.returncode = None if hang else returncode
        self.killed = False
        self.wait_calls = 0
        self.started = asyncio.Event()
        for stream in (self.stdout, self.stderr):
            original_read = stream.read

            async def read(size, original_read=original_read):
                self.started.set()
                return await original_read(size)
            stream.read = read

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self):
        self.wait_calls += 1
        return self.returncode


class Plain:
    def __init__(self, text):
        self.text = text

    def to_dict(self):
        return {"type": "text", "data": {"text": self.text}}


class Video:
    def __init__(self, file):
        self.file = file

    @classmethod
    def fromFileSystem(cls, path):
        return cls(Path(path).resolve().as_uri())

    def to_dict(self):
        # Deliberately preserves URI: this is the cross-container hazard.
        return {"type": "video", "data": {"file": self.file}}


class MessageChain:
    def __init__(self, chain):
        self.chain = chain


def expected_forward(first=MP4, second=MP4, footer=delivery.VIDEO_VOTE_FOOTER, self_id=456):
    return [
        {"type": "node", "data": {"user_id": self_id, "nickname": "视频 Battle A", "content": [
            {"type": "text", "data": {"text": delivery.VIDEO_HEADER + "\nA\n"}},
            {"type": "video", "data": {"file": "base64://" + base64.b64encode(first).decode("ascii")}},
        ]}},
        {"type": "node", "data": {"user_id": self_id, "nickname": "视频 Battle B", "content": [
            {"type": "text", "data": {"text": "\nB\n" + footer}},
            {"type": "video", "data": {"file": "base64://" + base64.b64encode(second).decode("ascii")}},
        ]}},
    ]


def forward_nodes(payload):
    nodes = [{"sender": {"user_id": node["data"]["user_id"], "nickname": node["data"]["nickname"]},
              "message": copy.deepcopy(node["data"]["content"])} for node in payload]
    for lane, node in enumerate(nodes):
        video = node["message"][1]["data"]
        raw = base64.b64decode(video["file"][9:], validate=True)
        video.update(file="native-file-" + str(lane), file_size=str(len(raw)))
    return nodes


def serialized_forward_request(action, params):
    return json.dumps({"action": action, "params": params}, ensure_ascii=True)


class LeafApi:
    def __init__(self):
        self._timeout_sec = 0.01
        self.calls = []
        self.readbacks = []
        self.readback_options = {"delay": 0, "error": None, "result": "auto"}
        self.forward_options = {"delay": 0, "error": None, "result": "auto"}
        self.options = {"receipt": {"message_id": 42}, "error": None, "delay": 0}
        self.started = asyncio.Event()
        self.outcomes = []
        self.on_call = None

    async def call_action(self, action, **params):
        if action in ("get_msg", "get_forward_msg"):
            self.readbacks.append((action, params, self._timeout_sec))
            options = self.readback_options if action == "get_msg" else self.forward_options
            await asyncio.sleep(options["delay"])
            if options["error"] is not None:
                raise options["error"]
            result = options["result"]
            if result == "auto":
                if action == "get_msg":
                    return {"message_id": params["message_id"], "message": [
                        {"type": "forward", "data": {"id": "native-forward-resource"}}]}
                sent = next(call for call in reversed(self.calls)
                            if call[1].get("self_id") == params.get("self_id"))
                return {"messages": forward_nodes(sent[1]["message"])}
            return result
        # The installed native forward schema requires singular message.
        if action not in ("send_group_forward_msg", "send_private_forward_msg"):
            raise AssertionError("Unexpected business send action")
        if not isinstance(params.get("message"), list) or len(params["message"]) != 2:
            raise AssertionError("Native forward schema requires message nodes")
        self.calls.append((action, params, self._timeout_sec))
        self.started.set()
        await asyncio.sleep(self.options["delay"])
        if self.on_call is not None:
            self.on_call()
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        if self.options["error"] is not None:
            raise self.options["error"]
        return self.options["receipt"]


class BaseEvent:
    async def send(self, chain):
        self.bookkeeping += 1
        if self.bookkeeping_error:
            raise self.bookkeeping_error


class QQEvent:
    def __init__(self, api):
        self.bot = SimpleNamespace(_api=api)
        self.group = "123"
        self.message_obj = SimpleNamespace(raw_message={"self_id": "456"})
        self.bookkeeping = 0
        self.bookkeeping_error = None
        self.fallback = AsyncMock()
        self.send = self.fallback
        self.parsed_chains = []
        self.drop_video = False

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return "789"

    async def _parse_onebot_json(self, chain):
        self.parsed_chains.append(chain)
        messages = [component.to_dict() for component in chain.chain]
        return messages[:-1] if self.drop_video else messages


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT, prefix=".test-video-")
        self.addCleanup(self.temp.cleanup)
        self.paths = [Path(self.temp.name) / name for name in ("a.mp4", "b.mp4")]
        for path in self.paths:
            path.write_bytes(MP4)
        self.result = SimpleNamespace(chain=[Plain("prompt-model-secret " + SIGNED_URL),
            Video.fromFileSystem(str(self.paths[0])), Plain("B"), Video.fromFileSystem(str(self.paths[1]))])
        self.host = SimpleNamespace(config={"video_send_timeout": 1, "qq_video_send_timeout": 0.1, "send_max_attempts": 3, "send_retry_delay": 0})
        self.api = LeafApi()
        self.event = QQEvent(self.api)
        components = ModuleType("fake_components")
        components.Plain = Plain
        components.Video = Video
        components.Reply = type("Reply", (), {})
        adapter = ModuleType("fake_adapter")
        adapter.AiocqhttpMessageEvent = QQEvent
        event_api = ModuleType("fake_api")
        event_api.AstrMessageEvent = BaseEvent
        chain_api = ModuleType("fake_chain")
        chain_api.MessageChain = MessageChain
        patcher = patch.dict(sys.modules, {
            "astrbot.api.message_components": components,
            "astrbot.api.event": event_api,
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": adapter,
            "astrbot.core.message.message_event_result": chain_api,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    async def send(self):
        return await delivery.deliver_video_battle_message(self.host, self.event, self.result)

    def assert_no_fallback(self):
        self.event.fallback.assert_not_awaited()

    async def test_two_native_videos_labels_same_single_raw_call_numeric_receipt(self):
        receipt = await self.send()
        self.assertEqual(receipt.message_id, "42")
        self.assertEqual(type(receipt).__name__, "BattleMessageReceipt")
        self.assertEqual(len(self.api.calls), 1)
        action, params, deadline = self.api.calls[0]
        self.assertEqual(action, "send_group_forward_msg")
        self.assertEqual(params["group_id"], 123)
        self.assertEqual(params["self_id"], "456")
        self.assertEqual(params["timeout"], 100)
        self.assertEqual(deadline, 15.1)
        self.assertEqual(self.api._timeout_sec, 0.01)
        messages = params["message"]
        self.assertEqual(messages, expected_forward())
        self.assertNotIn("messages", params)
        self.assertEqual([item["type"] for item in messages], ["node", "node"])
        for node, label in zip(messages, ("A", "B")):
            self.assertEqual(node["data"]["user_id"], 456)
            self.assertEqual(node["data"]["nickname"], "视频 Battle " + label)
            self.assertEqual([item["type"] for item in node["data"]["content"]], ["text", "video"])
            video = node["data"]["content"][1]
            self.assertEqual(set(video["data"]), {"file"})
            self.assertEqual(base64.b64decode(video["data"]["file"][9:]), MP4)
        self.assertNotIn("file:", str(messages))
        self.assertNotIn("https:", str(messages))
        self.assertNotIn("secret", str(messages))
        self.assertEqual([type(item) for item in self.event.parsed_chains[0].chain], [Plain, Plain, Video, Plain, Video, Plain])
        self.assertEqual(self.event.bookkeeping, 1)
        self.assertEqual(self.api.readbacks, [
            ("get_msg", {"message_id": 42, "self_id": "456"}, 15.1),
            ("get_forward_msg", {"message_id": "native-forward-resource", "self_id": "456"}, 15.1)])
        self.assertTrue(all(path.exists() for path in self.paths))
        self.assert_no_fallback()

    async def test_parent_video_send_timeout_is_honored(self):
        self.host.config = {"video_send_timeout": 0.3}
        self.api.options["delay"] = 10
        # This test targets a timeout AFTER the actual send boundary, not slow
        # thread-pool startup during preparation. Inline the already-read fixture.
        inline = delivery._inline_videos(self.paths)
        with patch.object(delivery.asyncio, "to_thread", AsyncMock(side_effect=
                lambda function, *args: inline if function is delivery._inline_videos else function(*args))) as prepare:
            failure = await self.send()
        self.assertEqual(prepare.await_count, 2)
        prepare.assert_any_await(delivery._inline_videos, self.paths)
        prepare.assert_any_await(delivery._forward_request_fits, "send_group_forward_msg", self.api.calls[0][1])
        self.assertTrue(self.api.started.is_set())
        self.assertEqual(failure.code, "video_send_uncertain")
        self.assertTrue(failure.uncertain)
        self.assertEqual(failure.attempts, 1)
        self.assertEqual(self.api.calls[0][1]["timeout"], 300)
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.api.readbacks, [])
        self.assert_no_fallback()

    async def test_preparation_timeout_is_certain_without_actual_send(self):
        self.host.config = {"video_send_timeout": 0.03}

        async def slow_prepare(function, paths):
            await asyncio.sleep(10)

        with patch.object(delivery.asyncio, "to_thread", AsyncMock(side_effect=slow_prepare)) as prepare:
            failure = await self.send()
        prepare.assert_awaited_once_with(delivery._inline_videos, self.paths)
        self.assertEqual(failure.code, "video_transport_invalid")
        self.assertFalse(failure.uncertain)
        self.assertFalse(failure.confirmed)
        self.assertEqual(failure.attempts, 0)
        self.assertFalse(self.api.started.is_set())
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.api.readbacks, [])
        self.assert_no_fallback()

    async def test_reveal_footer_exact_and_public_aliases(self):
        self.assertIs(delivery.materialize_video_output, delivery.materialize_video)
        self.assertIs(delivery.deliver_video_battle_message, delivery.deliver_complete_video_battle)
        self.host.config["battle_allow_reveal_without_vote"] = True
        self.result.chain.append(Plain("unsafe upstream footer " + SIGNED_URL))
        receipt = await self.send()
        self.assertEqual(receipt.message_id, "42")
        messages = self.api.calls[0][1]["message"]
        self.assertEqual(messages[-1]["data"]["content"][0]["data"]["text"],
                         "\nB\n" + delivery.VIDEO_REVEAL_FOOTER)
        self.assertEqual(sum(segment["type"] == "video" for node in messages
                             for segment in node["data"]["content"]), 2)
        self.assertNotIn(SIGNED_URL, str(messages))
        self.assertEqual(len(self.api.calls), 1)

    async def test_private_routing_and_bookkeeping_failure_preserve_confirmation(self):
        self.event.group = ""
        self.event.bookkeeping_error = RuntimeError(SIGNED_URL)
        self.api.options["receipt"] = {"message_id": "-123"}
        receipt = await self.send()
        self.assertEqual(receipt.message_id, "-123")
        action, params, _ = self.api.calls[0]
        self.assertEqual(action, "send_private_forward_msg")
        self.assertEqual(params["user_id"], 789)
        self.assertNotIn("group_id", params)
        self.assertEqual(params["message"], expected_forward())
        self.assertNotIn("messages", params)
        self.assertEqual(len(self.api.calls), 1)
        self.assert_no_fallback()

    async def test_unknown_and_non_numeric_receipts_fail_uncertain_no_resend(self):
        for raw in (None, {}, "42", {"data": {"message_id": 42}}, {"message_id": True},
                    {"message_id": 0}, {"message_id": 42.0}, {"message_id": "https://secret"},
                    {"message_id": "1" * 21}, {"status": "async", "message_id": 42},
                    {"status": "failed", "retcode": 1200, "message_id": 42},
                    {"retcode": True, "message_id": 42}):
            self.api.calls.clear()
            self.api.options["receipt"] = raw
            failure = await self.send()
            self.assertIsInstance(failure, delivery.VideoDeliveryFailure)
            self.assertTrue(failure.uncertain)
            self.assertEqual(failure.attempts, 1)
            self.assertEqual(len(self.api.calls), 1)
            self.assert_no_fallback()
        self.assertEqual(self.api.readbacks, [])
        self.assertEqual(self.event.bookkeeping, 0)

    async def test_only_explicit_rejection_retries_unknown_errors_never_do(self):
        for error, uncertain in ((REJECTION, False), (TimeoutError(str(REJECTION)), True),
                                 (RuntimeError("retcode 1200 " + SIGNED_URL), True),
                                 (ConnectionError(str(REJECTION)), True),
                                 (RuntimeError(str(REJECTION) + " timeout"), True),
                                 (RuntimeError("EventChecker failed sendMsg result:120"), True),
                                 (RuntimeError("NodeIKernelMsgService/sendMsg result:120"), True),
                                 (RuntimeError("EventChecker failed NodeIKernelMsgService/sendMsg result:1200"), True)):
            self.api.calls.clear()
            self.api.options["error"] = error
            failure = await self.send()
            self.assertEqual(failure.uncertain, uncertain)
            expected = 1 if uncertain else 3
            self.assertEqual(failure.attempts, expected)
            self.assertEqual(len(self.api.calls), expected)
            self.assertNotIn("PRIVATE", failure.message)
            self.assert_no_fallback()

    async def test_known_rejection_then_success_exact_complete_payload_original_metadata(self):
        # Different videos establish lane ordering as well as completeness.
        second = MP4[:-4] + b"BBBB-LONGER"
        self.paths[1].write_bytes(second)
        self.result.metadata = {"session": "opaque", "model": "private-model", "url": SIGNED_URL}
        original_chain = self.result.chain
        original_components = list(original_chain)
        original_state = copy.deepcopy([vars(component) for component in original_chain])
        original_metadata = copy.deepcopy(self.result.metadata)
        original_files = [(path.read_bytes(), path.stat().st_mtime_ns) for path in self.paths]
        self.api.outcomes = [RuntimeError(str(REJECTION) + " " + SIGNED_URL), {"status": "ok", "retcode": 0, "message_id": 43}]
        with patch.object(delivery, "_inline_videos", wraps=delivery._inline_videos) as encode:
            receipt = await self.send()
        self.assertEqual(receipt.message_id, "43")
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(self.api.calls[0], self.api.calls[1])
        messages = self.api.calls[0][1]["message"]
        self.assertEqual(messages, expected_forward(second=second))
        self.assertIsNot(messages, self.api.calls[1][1]["message"])
        encode.assert_called_once()
        self.assertEqual(len(self.event.parsed_chains), 1)
        self.assertEqual(self.event.bookkeeping, 1)
        self.assertIs(self.result.chain, original_chain)
        self.assertEqual(self.result.chain, original_components)
        self.assertEqual([vars(component) for component in self.result.chain], original_state)
        self.assertEqual(self.result.metadata, original_metadata)
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in self.paths], original_files)
        self.assertNotIn("private-model", str(messages))
        self.assertNotIn(SIGNED_URL, str(messages))
        self.assertEqual(self.api._timeout_sec, 0.01)
        self.assert_no_fallback()

    async def test_rejection_then_timeout_or_unknown_receipt_stops_before_third(self):
        for unknown in (TimeoutError("receipt timeout " + SIGNED_URL), None, {},
                        {"status": "async", "message_id": 42},
                        {"status": "failed", "retcode": 1200, "message_id": 42},
                        RuntimeError("unknown receipt " + SIGNED_URL)):
            with self.subTest(outcome_type=type(unknown).__name__):
                self.api.calls.clear()
                self.api.outcomes = [RuntimeError(str(REJECTION)), unknown, {"message_id": 99}]
                failure = await self.send()
                self.assertEqual(failure.code, "video_send_uncertain")
                self.assertTrue(failure.uncertain)
                self.assertEqual(failure.attempts, 2)
                self.assertEqual(len(self.api.calls), 2)
                self.assertEqual(self.api.calls[0], self.api.calls[1])
                self.assertEqual(self.api.outcomes, [{"message_id": 99}])
                self.assertNotIn("PRIVATE", failure.message)
        self.assertEqual(self.event.bookkeeping, 0)
        self.assert_no_fallback()

    async def test_rejections_exhaust_configured_attempts_and_linear_waits(self):
        self.host.config.update(video_send_timeout=1, send_max_attempts=4, send_retry_delay=0.01)
        self.api.options["error"] = RuntimeError(str(REJECTION))
        original_sleep = asyncio.sleep
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            await original_sleep(0)

        with patch.object(delivery.asyncio, "sleep", sleep):
            failure = await self.send()
        self.assertEqual(failure.code, "video_send_rejected")
        self.assertEqual(failure.attempts, 4)
        self.assertFalse(failure.uncertain)
        self.assertEqual([wait for wait in waits if wait], [0.01, 0.02, 0.03])
        self.assertEqual(len(self.api.calls), 4)
        self.assertTrue(all(call == self.api.calls[0] for call in self.api.calls))
        self.assertEqual(self.event.bookkeeping, 0)
        self.assert_no_fallback()

    async def test_host_shared_policy_methods_override_config(self):
        self.host.config.update(send_max_attempts=6, send_retry_delay=30)
        self.host._send_max_attempts = Mock(return_value=2)
        self.host._send_retry_delay = Mock(return_value=0)
        self.api.options["error"] = REJECTION
        failure = await self.send()
        self.assertEqual(failure.attempts, 2)
        self.assertFalse(failure.uncertain)
        self.assertEqual(len(self.api.calls), 2)
        self.host._send_max_attempts.assert_called_once_with()
        self.host._send_retry_delay.assert_called_once_with()
        self.assert_no_fallback()

    async def test_shared_policy_fallback_defaults_bounds_and_types(self):
        host = SimpleNamespace(config={})
        self.assertEqual(delivery._send_policy(host, {}), (3, 2.0))
        for attempts, delay, expected in (
            ("2", "0.5", (2, 0.5)), (0, -1, (1, 0.0)), (99, 99, (6, 30.0)),
            (True, False, (3, 2.0)), (float("inf"), float("nan"), (3, 2.0)),
            (float("nan"), float("inf"), (3, 2.0)), (object(), [], (3, 2.0)),
            ("bad", "bad", (3, 2.0)),
        ):
            self.assertEqual(delivery._send_policy(host, {"send_max_attempts": attempts, "send_retry_delay": delay}), expected)

    async def test_single_attempt_config_disables_rejection_retry(self):
        self.host.config["send_max_attempts"] = 1
        self.api.outcomes = [REJECTION, {"message_id": 99}]
        failure = await self.send()
        self.assertEqual(failure.code, "video_send_rejected")
        self.assertEqual(failure.attempts, 1)
        self.assertFalse(failure.uncertain)
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.api.outcomes, [{"message_id": 99}])
        self.assert_no_fallback()

    async def test_total_deadline_bounds_retry_wait_after_known_rejection(self):
        self.host.config.update(video_send_timeout=0.3, qq_video_send_timeout=1, send_retry_delay=10)
        self.api.outcomes = [REJECTION, {"message_id": 99}]
        failure = await asyncio.wait_for(self.send(), 1)
        self.assertEqual(failure.code, "video_send_rejected")
        self.assertEqual(failure.attempts, 1)
        self.assertFalse(failure.uncertain)
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.api.calls[0][1]["timeout"], 300)
        self.assertEqual(self.api.calls[0][2], 15.3)
        self.assertEqual(self.api._timeout_sec, 0.01)
        self.assert_no_fallback()

    async def test_total_deadline_bounds_second_send_not_reset_per_attempt(self):
        self.host.config.update(video_send_timeout=0.3, qq_video_send_timeout=1, send_retry_delay=0)
        self.api.outcomes = [REJECTION, {"message_id": 99}, {"message_id": 100}]
        self.api.options["delay"] = 0.2
        failure = await asyncio.wait_for(self.send(), 1)
        self.assertEqual(failure.code, "video_send_uncertain")
        self.assertEqual(failure.attempts, 2)
        self.assertTrue(failure.uncertain)
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(self.api.outcomes, [{"message_id": 99}, {"message_id": 100}])
        self.assert_no_fallback()

    async def test_cancellation_after_rejection_during_second_send_never_third(self):
        self.api.outcomes = [REJECTION, asyncio.CancelledError(), {"message_id": 99}]
        with self.assertRaises(asyncio.CancelledError):
            await self.send()
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(self.api.outcomes, [{"message_id": 99}])
        self.assert_no_fallback()

    async def test_cancellation_during_retry_wait_never_second(self):
        self.host.config.update(video_send_timeout=1, send_retry_delay=10)
        self.api.outcomes = [REJECTION, {"message_id": 99}]
        waiting = asyncio.Event()
        original_sleep = asyncio.sleep

        async def sleep(seconds):
            if seconds:
                waiting.set()
            await original_sleep(seconds)

        with patch.object(delivery.asyncio, "sleep", sleep):
            task = asyncio.create_task(self.send())
            await asyncio.wait_for(waiting.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.api.outcomes, [{"message_id": 99}])
        self.assert_no_fallback()

    async def test_serializer_timeout_has_zero_actual_send_attempts(self):
        async def parse(chain):
            await asyncio.sleep(10)

        self.event._parse_onebot_json = parse
        failure = await self.send()
        self.assertEqual(failure.attempts, 0)
        self.assertEqual(self.api.calls, [])
        self.assert_no_fallback()

    async def test_retry_freezes_footer_even_if_config_or_serializer_changes(self):
        self.host.config["battle_allow_reveal_without_vote"] = True

        def mutate_local_settings():
            self.host.config["battle_allow_reveal_without_vote"] = False
            self.event.drop_video = True

        self.api.on_call = mutate_local_settings
        self.api.outcomes = [REJECTION, {"message_id": 43}]
        receipt = await self.send()
        self.assertEqual(receipt.message_id, "43")
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(self.api.calls[0], self.api.calls[1])
        self.assertEqual(self.api.calls[1][1]["message"][-1]["data"]["content"][0]["data"]["text"], "\nB\n" + delivery.VIDEO_REVEAL_FOOTER)
        self.assertEqual(len(self.event.parsed_chains), 1)
        self.assert_no_fallback()

    async def test_complete_readback_coalesced_text_verifies_before_bookkeeping(self):
        nodes = forward_nodes(expected_forward())
        nodes[0]["message"] = [
            {"type": "text", "data": {"text": delivery.VIDEO_HEADER}},
            {"type": "video", "data": {"file": "native-file-a", "file_size": str(len(MP4))}},
            {"type": "text", "data": {"text": "\nA\n"}},
        ]
        nodes[1]["message"][1]["data"]["file"] = "native-file-b"
        self.api.forward_options["result"] = {"messages": nodes}
        receipt = await self.send()
        self.assertEqual(receipt.message_id, "42")
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.api.readbacks[0][0:2], ("get_msg", {"message_id": 42, "self_id": "456"}))
        self.assertEqual(self.event.bookkeeping, 1)
        self.assert_no_fallback()

    async def test_get_msg_preserves_two_bot_routing_without_shared_scope_mutation(self):
        # Both bot accounts share the original API and even the same numeric
        # receipt ID: readback must still use each event's captured self_id.
        other = QQEvent(self.api)
        other.message_obj.raw_message["self_id"] = "987"
        other.group = ""
        scoped_apis = []
        original_scope = delivery._scoped_api

        def capture_scope(api, timeout):
            scoped = original_scope(api, timeout)
            scoped_apis.append(scoped)
            return scoped

        with patch.object(delivery, "_scoped_api", side_effect=capture_scope):
            receipts = await asyncio.gather(
                self.send(),
                delivery.deliver_video_battle_message(self.host, other, self.result),
            )
        self.assertEqual([receipt.message_id for receipt in receipts], ["42", "42"])
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(
            {action: params["self_id"] for action, params, _ in self.api.calls},
            {"send_group_forward_msg": "456", "send_private_forward_msg": "987"},
        )
        self.assertCountEqual(self.api.readbacks, [
            ("get_msg", {"message_id": 42, "self_id": "456"}, 15.1),
            ("get_msg", {"message_id": 42, "self_id": "987"}, 15.1),
            ("get_forward_msg", {"message_id": "native-forward-resource", "self_id": "456"}, 15.1),
            ("get_forward_msg", {"message_id": "native-forward-resource", "self_id": "987"}, 15.1),
        ])
        self.assertEqual(len(scoped_apis), 2)
        self.assertIsNot(scoped_apis[0], scoped_apis[1])
        self.assertTrue(all(scoped is not self.api for scoped in scoped_apis))
        self.assertEqual(self.api._timeout_sec, 0.01)
        self.assertEqual(self.event.message_obj.raw_message["self_id"], "456")
        self.assertEqual(other.message_obj.raw_message["self_id"], "987")
        self.assertEqual(self.event.bookkeeping, 1)
        self.assertEqual(other.bookkeeping, 1)
        other.fallback.assert_not_awaited()
        self.assert_no_fallback()

        # Do not invent bot identity when the event does not provide it.
        self.event.message_obj.raw_message = {}
        before = (len(self.api.calls), len(self.api.readbacks))
        failure = await self.send()
        self.assertEqual(failure.code, "video_transport_unavailable")
        self.assertEqual(failure.attempts, 0)
        self.assertEqual(before, (len(self.api.calls), len(self.api.readbacks)))
        self.assertEqual(self.api._timeout_sec, 0.01)
        self.assert_no_fallback()

    async def test_confirmed_receipt_incomplete_or_unknown_readback_never_votes_or_retries(self):
        complete = forward_nodes(expected_forward())
        for node in complete:
            node["message"][1]["data"]["file"] = SIGNED_URL
        bad_label = copy.deepcopy(complete)
        bad_label[1]["message"][0]["data"]["text"] = "\nA\n" + delivery.VIDEO_VOTE_FOOTER
        extra = copy.deepcopy(complete)
        extra[1]["message"].append({"type": "video", "data": {"file": SIGNED_URL}})
        missing_file = copy.deepcopy(complete)
        missing_file[0]["message"][1]["data"] = {}
        no_footer = copy.deepcopy(complete)
        no_footer[1]["message"][0]["data"]["text"] = "\nB\n"
        missing_video = copy.deepcopy(complete)
        missing_video[1]["message"].pop()
        results = (
            None, {}, {"messages": "[CQ:video,file=private]"},
            {"raw_message": complete}, {"content": complete}, {"data": {"messages": complete}},
            {"status": "async", "messages": complete}, {"retcode": True, "messages": complete},
            *({"messages": body} for body in (
                complete[:1], missing_video, no_footer, bad_label, extra, missing_file,
                list(reversed(complete)), [])),
        )
        for result in results:
            self.api.calls.clear()
            self.api.readbacks.clear()
            self.api.forward_options["result"] = result
            failure = await self.send()
            self.assertEqual(failure.code, "video_verification_uncertain")
            self.assertTrue(failure.confirmed)
            self.assertTrue(failure.uncertain)
            self.assertEqual(failure.message_id, "42")
            self.assertEqual(failure.attempts, 1)
            self.assertEqual(len(self.api.calls), 1)
            self.assertEqual(len(self.api.readbacks), 2)
            self.assertNotIn("PRIVATE", failure.message)
            self.assertNotIn(SIGNED_URL, failure.message)
        self.assertEqual(self.event.bookkeeping, 0)
        self.assert_no_fallback()

    async def test_readback_timeout_unknown_error_or_rejection_never_retries_confirmed_send(self):
        for error in (TimeoutError(SIGNED_URL), ConnectionError(SIGNED_URL),
                      RuntimeError(SIGNED_URL), REJECTION):
            self.api.calls.clear()
            self.api.readbacks.clear()
            self.api.readback_options["error"] = error
            failure = await self.send()
            self.assertEqual(failure.code, "video_verification_uncertain")
            self.assertTrue(failure.confirmed)
            self.assertTrue(failure.uncertain)
            self.assertEqual(failure.attempts, 1)
            self.assertEqual(len(self.api.calls), 1)
            self.assertEqual(len(self.api.readbacks), 1)
        self.api.readback_options.update(error=None, delay=10)
        failure = await self.send()
        self.assertTrue(failure.confirmed)
        self.assertTrue(failure.uncertain)
        self.assertEqual(failure.attempts, 1)
        self.assertEqual(self.event.bookkeeping, 0)
        self.assert_no_fallback()

    async def test_rejection_success_incomplete_readback_no_third_send(self):
        self.api.outcomes = [REJECTION, {"message_id": 43}, {"message_id": 99}]
        self.api.readback_options["result"] = {"message_id": 43, "message": []}
        failure = await self.send()
        self.assertEqual(failure.code, "video_verification_uncertain")
        self.assertTrue(failure.confirmed)
        self.assertEqual(failure.attempts, 2)
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(len(self.api.readbacks), 1)
        self.assertEqual(self.api.outcomes, [{"message_id": 99}])
        self.assert_no_fallback()

    async def test_cancellation_during_readback_never_resends(self):
        self.api.readback_options["error"] = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.send()
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(len(self.api.readbacks), 1)
        self.assertEqual(self.event.bookkeeping, 0)
        self.assert_no_fallback()

    async def test_invalid_timeout_types_fail_before_send(self):
        for key in ("video_send_timeout", "qq_video_send_timeout"):
            for value in (True, 0, -1, 601, float("nan"), float("inf"), object()):
                original = self.host.config[key]
                self.host.config[key] = value
                failure = await self.send()
                self.assertEqual(failure.attempts, 0)
                self.assertEqual(self.api.calls, [])
                self.host.config[key] = original
        self.assert_no_fallback()

    async def test_exception_chain_timeout_overrides_rejection(self):
        error = RuntimeError(str(REJECTION))
        error.__cause__ = TimeoutError("receipt unknown")
        self.api.options["error"] = error
        failure = await self.send()
        self.assertTrue(failure.uncertain)
        self.assertEqual(len(self.api.calls), 1)

    async def test_bounded_send_timeout_and_cancellation_never_resend(self):
        self.host.config["qq_video_send_timeout"] = 0.02
        self.api.options["delay"] = 10

        async def prepare(function, *args):
            # Exercise timeout/cancellation AFTER the real platform boundary;
            # thread-pool scheduling is covered by preparation timeout tests.
            return function(*args)

        with patch.object(delivery.asyncio, "to_thread", AsyncMock(side_effect=prepare)):
            failure = await self.send()
            self.assertTrue(self.api.started.is_set())
            self.assertTrue(failure.uncertain)
            self.assertEqual(len(self.api.calls), 1)
            self.api.calls.clear()
            self.api.started.clear()
            task = asyncio.create_task(self.send())
            await self.api.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(len(self.api.calls), 1)
        self.assert_no_fallback()

    async def test_serializer_dropping_one_video_fails_without_platform_call(self):
        self.event.drop_video = True
        failure = await self.send()
        self.assertEqual(failure.code, "video_transport_unavailable")
        self.assertEqual(failure.attempts, 0)
        self.assertFalse(failure.uncertain)
        self.assertEqual(self.api.calls, [])
        self.assert_no_fallback()

    async def test_unsupported_transport_invalid_paths_and_missing_video_fail_closed(self):
        with patch.object(delivery, "prepare_qq_video_sender", return_value=None):
            failure = await self.send()
        self.assertEqual(failure.code, "video_transport_unavailable")
        for file in (SIGNED_URL, "file://other-container/video.mp4", "relative.mp4", "base64://DATA"):
            self.result.chain[1] = Video(file)
            failure = await self.send()
            self.assertEqual(failure.attempts, 0)
        self.result.chain = [Video.fromFileSystem(str(self.paths[0]))]
        failure = await self.send()
        self.assertEqual(failure.attempts, 0)
        self.assertEqual(self.api.calls, [])
        self.assert_no_fallback()

    async def test_total_inline_limit_and_mutated_invalid_file_prevent_send(self):
        total = 2 * len(MP4)
        with patch.object(delivery, "MAX_INLINE_VIDEO_BYTES", total - 1):
            failure = await self.send()
        self.assertEqual(failure.attempts, 0)
        self.assertEqual(self.api.calls, [])
        with patch.object(delivery, "MAX_INLINE_VIDEO_BYTES", total):
            receipt = await self.send()
        self.assertEqual(receipt.message_id, "42")
        self.api.calls.clear()
        self.paths[1].write_bytes(b"not an mp4")
        failure = await self.send()
        self.assertEqual(failure.attempts, 0)
        self.assertEqual(self.api.calls, [])


if __name__ == "__main__":
    unittest.main()
