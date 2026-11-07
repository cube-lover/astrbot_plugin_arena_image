"""Bounded MP4 materialization and complete anonymous OneBot delivery.

``materialize_video_output`` returns a Path in a new, private directory below
output_dir. The caller owns that directory on success; all failures/cancellation
remove it. HTTPS only, no redirects, proxy environment or cookies. DNS results
are checked in the connector's resolver (not a separate, rebindable preflight);
all answers must be public unicast addresses. IP literals are checked too.
No private-storage exception/allowlist is enabled.

ffprobe=None/False performs only MIME, length and ISO BMFF header validation,
NOT full codec/playability validation. Pass True (PATH lookup) or an executable
path to require bounded video-stream/dimension/duration verification AND full
video-sample decoding to a null sink using ffmpeg (sibling executable or PATH).
Missing ffmpeg fails closed. Metadata alone cannot establish playability. Decode
is capped at 30 seconds, single-threaded, and creates no transcoded output.
Only the MOV demuxer/file protocol is allowed; external track references are
explicitly disabled. Subprocess diagnostics are bounded and never disclosed.

Delivery accepts result.chain with exactly two Video.fromFileSystem components
created from verified outputs. It rebuilds fixed A/B labels, ignoring supplied
text (which may contain prompts/models). AstrBot Video.to_dict may retain a
plugin-local file URI, even though Image serialization inlines images. Therefore
we use the adapter's component serializer, then replace BOTH video records with
explicit base64 OneBot payloads. Never send a URI/signed URL or rely on a shared
NapCat filesystem. The combined raw-file cap is 32 MiB, NOT 64 MiB per video.
A numeric direct message_id is mandatory. No fallback: only a known, confirmed
QQ rejection may retry the identical complete message within the configured
attempt/deadline bounds. Timeout, unknown receipt and cancellation never retry.
A read-only get_msg must confirm the exact complete dual-video message before a
votable receipt is returned; failed/unknown readback never permits another send.
Native QQ dual-video acceptance is unverified.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import tempfile
from typing import Any
from urllib.parse import unquote, urlsplit
import uuid

import aiohttp

from .bridge_client import BridgeError
from .qq_delivery import _scoped_api

DEFAULT_MAX_VIDEO_BYTES = 64 * 1024 * 1024
MAX_INLINE_VIDEO_BYTES = 32 * 1024 * 1024  # Two videos combined, before base64.
DEFAULT_VIDEO_TIMEOUT = 90.0
DEFAULT_VIDEO_SEND_TIMEOUT = 180.0
DEFAULT_SEND_MAX_ATTEMPTS = 3
DEFAULT_SEND_RETRY_DELAY = 2.0
VIDEO_HEADER = "视频 Battle\n"
VIDEO_VOTE_FOOTER = "引用本条回复 A / B 投票并揭晓。"
VIDEO_REVEAL_FOOTER = "引用本条回复 A / B 投票；回复“不投票”随机投票并揭晓。"
FFPROBE_TIMEOUT = 15.0
FFMPEG_DECODE_TIMEOUT = 30.0
MAX_DECODE_STDERR_BYTES = 16 * 1024
MAX_VIDEO_DURATION = 180.0
MAX_PROBE_BYTES = 64 * 1024
_CHUNK_BYTES = 64 * 1024
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_ERRORS = {
    "video_invalid_input": "匿名视频下载参数无效，未发送视频",
    "video_unsafe_url": "匿名视频地址不安全，未下载或发送视频",
    "video_download_failed": "匿名视频下载失败，未发送视频",
    "video_download_timeout": "匿名视频下载或校验超时，未发送视频",
    "video_size_limit": "匿名视频超过大小限制，未发送视频",
    "video_invalid_mime": "匿名视频类型不是 MP4，未发送视频",
    "video_invalid_mp4": "匿名视频 MP4 校验失败，未发送视频",
    "video_probe_failed": "匿名视频流校验失败，未发送视频",
    "video_probe_timeout": "匿名视频流校验超时，未发送视频",
    "video_decode_failed": "匿名视频样本解码校验失败，未发送视频",
    "video_decode_timeout": "匿名视频样本解码校验超时，未发送视频",
}


class VideoMaterializationError(BridgeError):
    """Fixed messages/codes only; never attach an upstream exception or URL."""

    def __init__(self, code: str):
        super().__init__(_ERRORS[code], payload={"error": {"code": code}})


@dataclass(frozen=True)
class VideoDeliveryFailure:
    message: str
    attempts: int
    uncertain: bool
    code: str = "video_delivery_failed"
    confirmed: bool = False  # Send confirmed, but readback may not prove completeness.


def _error(code: str) -> VideoMaterializationError:
    return VideoMaterializationError(code)


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return bool(
            address.is_global and not address.is_multicast
            and not address.is_reserved and not address.is_unspecified
            and not address.is_loopback and not address.is_link_local
            and not (isinstance(address, ipaddress.IPv6Address) and (
                address.ipv4_mapped or address.sixtofour or address.teredo
            ))
        )
    except ValueError:
        return False


def _video_url(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("mimeType", value.get("contentType", "video/mp4")) != "video/mp4":
            raise _error("video_invalid_mime")
        value = value.get("url")
    if (
        not isinstance(value, str) or not value or len(value) > 16384
        or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
        or "\\" in value
        or re.search(r"%(?![0-9a-fA-F]{2})|%(?:00|0a|0d)", value, re.I)
    ):
        raise _error("video_unsafe_url")
    valid = False
    try:
        parts = urlsplit(value)
        host = parts.hostname
        valid = bool(
            parts.scheme == "https" and host and not parts.fragment
            and "@" not in parts.netloc and parts.username is None
            and parts.password is None and not parts.netloc.endswith(":")
            and (parts.port is None or 1 <= parts.port <= 65535)
        )
        if valid:
            assert host is not None
            try:
                ipaddress.ip_address(host)
            except ValueError:
                valid = bool(
                    len(host) <= 253 and all(_HOST_LABEL.fullmatch(label)
                    for label in host.rstrip(".").split("."))
                    and host.rstrip(".").casefold() != "localhost"
                    and not host.rstrip(".").casefold().endswith(
                        (".localhost", ".local", ".internal")
                    )
                )
            else:
                valid = "%" not in host and _public_address(host)
    except (ValueError, AssertionError):
        valid = False
    if not valid:
        raise _error("video_unsafe_url")
    return value


class _PublicResolver(aiohttp.abc.AbstractResolver):
    """Only checked addresses reach TCPConnector, including on cache misses."""

    def __init__(self):
        self._resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await self._resolver.resolve(host, port, family)
        if not records or any(not _public_address(item["host"]) for item in records):
            raise _error("video_unsafe_url")
        return records

    async def close(self):
        await self._resolver.close()


def _verify_ftyp(header: bytes, size: int) -> None:
    # Require a complete, reasonably sized first ftyp box and some media data.
    header = bytes(header)
    if len(header) < 16 or header[4:8] != b"ftyp":
        raise _error("video_invalid_mp4")
    box_size = int.from_bytes(header[:4], "big")
    if not 16 <= box_size <= 4096 or box_size % 4 or box_size > len(header) or size < box_size + 8:
        raise _error("video_invalid_mp4")
    brands = [header[8:12]] + [header[i:i + 4] for i in range(16, box_size, 4)]
    if not any(brand in {b"isom", b"mp41", b"mp42", b"avc1", b"dash", b"msdh", b"msix"}
               or re.fullmatch(rb"iso[0-9]", brand) for brand in brands):
        raise _error("video_invalid_mp4")


def _positive_number(value: Any, maximum: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    try:
        number = float(value)
        return math.isfinite(number) and 0 < number <= maximum
    except (ValueError, OverflowError):
        return False


def _verify_probe(raw: bytes) -> None:
    valid = False
    try:
        data = json.loads(raw)
        streams = data.get("streams")
        if isinstance(streams, list) and streams:
            stream = streams[0]
            duration = stream.get("duration", data.get("format", {}).get("duration"))
            valid = bool(
                stream.get("codec_type") == "video"
                and all(isinstance(stream.get(key), int) and not isinstance(stream[key], bool)
                        and 0 < stream[key] <= 16384 for key in ("width", "height"))
                and _positive_number(duration, MAX_VIDEO_DURATION)
            )
            # Invalid explicit durations must not be hidden by a valid fallback.
            for item in (stream, data.get("format", {})):
                if "duration" in item and not _positive_number(item["duration"], MAX_VIDEO_DURATION):
                    valid = False
    except (ValueError, TypeError, AttributeError, KeyError):
        pass
    if not valid:
        raise _error("video_probe_failed")


async def _probe_video(path: Path, executable: str) -> None:
    process = None
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        executable, "-v", "error", "-protocol_whitelist", "file", "-f", "mov",
        "-enable_drefs", "0", "-use_absolute_path", "0", "-i", str(path), "-select_streams", "v:0", "-show_entries",
        "stream=codec_type,width,height,duration:format=duration", "-of", "json",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=MAX_PROBE_BYTES,
    ))
    try:
        async with asyncio.timeout(FFPROBE_TIMEOUT):
            # Shield creation so cancellation cannot orphan an already spawned child.
            process = await asyncio.shield(spawn)
            raw = bytearray()
            while True:
                chunk = await process.stdout.read(min(_CHUNK_BYTES, MAX_PROBE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_PROBE_BYTES:
                    raise _error("video_probe_failed")
            if await process.wait() != 0:
                raise _error("video_probe_failed")
            _verify_probe(raw)
    except TimeoutError:
        raise _error("video_probe_timeout") from None
    except asyncio.CancelledError:
        raise
    except VideoMaterializationError:
        raise
    except Exception:
        raise _error("video_probe_failed") from None
    finally:
        if process is None:
            # Recover the handle on cancellation during process creation.
            try:
                process = await asyncio.shield(spawn)
            except Exception:
                pass
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.shield(process.wait())


def _ffmpeg_executable(probe_executable: str) -> str:
    # A caller-supplied ffprobe path may come from a bundled FFmpeg install.
    # Never download/install a binary or silently skip sample validation.
    probe = Path(probe_executable)
    if probe.is_absolute():
        sibling = probe.with_name("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)
    executable = shutil.which("ffmpeg")
    if not executable:
        raise _error("video_decode_failed")
    return executable


async def _decode_video(path: Path, executable: str) -> None:
    """Decode the entire first video stream, with no output file or raw stdout.

    No -t/-frames limit: that could hide a corrupt sample at the tail. Wall time
    and decoder/filter thread counts bound work instead. Abort on empty output
    prevents metadata-only/zero-frame files from passing. With loglevel=error,
    ANY diagnostic (even with exit 0) fails closed; diagnostics stay private.
    """
    process = None
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        executable, "-nostdin", "-hide_banner", "-nostats", "-v", "error",
        "-xerror", "-max_error_rate", "0", "-abort_on", "empty_output+empty_output_stream",
        "-max_alloc", str(DEFAULT_MAX_VIDEO_BYTES),
        "-filter_threads", "1", "-filter_complex_threads", "1",
        "-protocol_whitelist", "file", "-format_whitelist", "mov",
        "-f", "mov", "-enable_drefs", "0", "-use_absolute_path", "0",
        "-err_detect", "explode", "-threads", "1", "-i", str(path),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-threads", "1", "-f", "null", "-",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE, limit=MAX_DECODE_STDERR_BYTES,
    ))
    try:
        async with asyncio.timeout(FFMPEG_DECODE_TIMEOUT):
            process = await asyncio.shield(spawn)
            diagnostic_bytes = 0
            while True:
                chunk = await process.stderr.read(min(
                    _CHUNK_BYTES, MAX_DECODE_STDERR_BYTES + 1 - diagnostic_bytes,
                ))
                if not chunk:
                    break
                diagnostic_bytes += len(chunk)
                if diagnostic_bytes > MAX_DECODE_STDERR_BYTES:
                    raise _error("video_decode_failed")
            if await process.wait() != 0 or diagnostic_bytes:
                raise _error("video_decode_failed")
    except TimeoutError:
        raise _error("video_decode_timeout") from None
    except asyncio.CancelledError:
        raise
    except VideoMaterializationError:
        raise
    except Exception:
        raise _error("video_decode_failed") from None
    finally:
        if process is None:
            # As with ffprobe, cancellation during spawn must recover the handle.
            try:
                process = await asyncio.shield(spawn)
            except Exception:
                pass
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.shield(process.wait())


async def materialize_video(
    value: Any, output_dir: str | Path,
    max_bytes: int = DEFAULT_MAX_VIDEO_BYTES,
    timeout: float = DEFAULT_VIDEO_TIMEOUT,
    ffprobe: str | Path | bool | None = None,
) -> Path:
    """Download one URL/video record; return a verified randomly named MP4 Path.

    ffprobe is opt-in and requires ffmpeg sample decoding too; neither check is
    silently skipped when requested. timeout covers downloading AND both checks
    (ffprobe <=15s, ffmpeg <=30s). max_bytes may lower but not raise the 64 MiB
    hard cap. The success directory can be removed by the caller after delivery.
    """
    scope = None
    success = False
    code = "video_download_failed"
    try:
        if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                or not 0 < max_bytes <= DEFAULT_MAX_VIDEO_BYTES
                or not _positive_number(timeout, 600)):
            raise _error("video_invalid_input")
        url = _video_url(value)
        executable = None
        if ffprobe is True:
            executable = shutil.which("ffprobe")
            if not executable:
                raise _error("video_probe_failed")
        elif isinstance(ffprobe, (str, Path)) and str(ffprobe):
            executable = str(ffprobe)
        elif ffprobe not in (None, False):
            raise _error("video_invalid_input")
        decoder = _ffmpeg_executable(executable) if executable else None
        async with asyncio.timeout(float(timeout)):
            parent = Path(output_dir)
            parent.mkdir(parents=True, exist_ok=True)
            scope = Path(tempfile.mkdtemp(prefix="video-", dir=parent))
            final = scope / (uuid.uuid4().hex + ".mp4")
            temporary = final.with_suffix(".part")
            resolver = _PublicResolver()
            try:
                connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, limit=1)
                async with aiohttp.ClientSession(
                    connector=connector, trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                    auto_decompress=False, timeout=aiohttp.ClientTimeout(total=float(timeout)),
                ) as client:
                    async with client.get(url, allow_redirects=False, headers={
                        "Accept": "video/mp4", "Accept-Encoding": "identity",
                    }) as response:
                        if response.status != 200:  # All redirects fail closed.
                            raise _error("video_download_failed")
                        mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        if mime != "video/mp4":
                            raise _error("video_invalid_mime")
                        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                            raise _error("video_invalid_mp4")
                        declared = response.headers.get("Content-Length")
                        length = None
                        if declared is not None:
                            if not re.fullmatch(r"[0-9]{1,20}", declared):
                                raise _error("video_invalid_mp4")
                            length = int(declared)
                            if length > max_bytes:
                                raise _error("video_size_limit")
                            if length == 0:
                                raise _error("video_invalid_mp4")
                        size = 0
                        header = bytearray()
                        with temporary.open("xb") as output:
                            async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
                                size += len(chunk)
                                if size > max_bytes:
                                    raise _error("video_size_limit")
                                if len(header) < 4096:
                                    header.extend(chunk[:4096 - len(header)])
                                output.write(chunk)
                        if length is not None and length != size:
                            raise _error("video_invalid_mp4")
                        _verify_ftyp(header, size)
                if executable:
                    await _probe_video(temporary, executable)
                    await _decode_video(temporary, decoder)
                temporary.replace(final)
            finally:
                await resolver.close()
            success = True
            return final
    except asyncio.CancelledError:
        raise
    except VideoMaterializationError as exc:
        code = exc.code
    except TimeoutError:
        code = "video_download_timeout"
    except Exception:
        code = "video_download_failed"
    finally:
        if scope is not None and not success:
            shutil.rmtree(scope)
    # Outside the exception handler: no sensitive upstream __context__/cause.
    raise _error(code)


materialize_video_output = materialize_video


def _local_video_path(component: Any) -> Path:
    value = getattr(component, "file", None)
    if not isinstance(value, str) or not value or len(value) > 32768:
        raise ValueError("Invalid local video")
    if value.lower().startswith("file:"):
        parts = urlsplit(value)
        if parts.scheme != "file" or parts.netloc or parts.query or parts.fragment:
            raise ValueError("Invalid local video")
        value = unquote(parts.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
    elif "://" in value or value.lower().startswith(("base64:", "data:")):
        raise ValueError("Invalid local video")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("Invalid local video")
    return path


def _inline_videos(paths: list[Path]) -> list[str]:
    # Open both before allocating base64, check aggregate size, then recheck
    # actual reads and headers. Files changed since materialization fail closed.
    handles = []
    try:
        total = 0
        sizes = []
        for path in paths:
            if path.is_symlink():
                raise ValueError("Invalid video file")
            handle = path.open("rb")
            handles.append(handle)
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise ValueError("Invalid video file")
            total += info.st_size
            sizes.append(info.st_size)
        if total > MAX_INLINE_VIDEO_BYTES:
            raise ValueError("Video transport size limit")
        encoded = []
        for handle, size in zip(handles, sizes):
            raw = handle.read(size + 1)
            if len(raw) != size:
                raise ValueError("Video file changed")
            _verify_ftyp(raw[:4096], size)
            encoded.append("base64://" + base64.b64encode(raw).decode("ascii"))
            del raw
        return encoded
    finally:
        for handle in handles:
            handle.close()


def _confirmed_rejection(exc: Exception) -> bool:
    # Match main.py's narrow NapCat rejection, with ambiguity taking precedence
    # across the exception chain. Retcode 1200 alone is NEVER proof of rejection.
    texts = []
    seen = set()
    current = exc
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, ConnectionError, asyncio.CancelledError)):
            return False
        text = str(current).casefold().replace('\\"', '"').replace("\\'", "'")
        if any(word in text for word in (
            "timeout", "timed out", "超时", "connection", "disconnect", "socket",
            "closed", "断开", "断连",
        )):
            return False
        texts.append(text)
        current = current.__cause__ or current.__context__
    return any("eventchecker failed" in text and "nodeikernelmsgservice/sendmsg" in text
               and re.search(r"[\"']?result[\"']?\s*[:=]\s*120\b", text) is not None
               for text in texts)


class _PreSendError(ValueError):
    """Local serializer failure, before any platform call."""


def prepare_qq_video_sender(event: Any, timeout_seconds: float):
    """Prepare only a known aiocqhttp raw transport, without sending anything."""
    try:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        from astrbot.api.event import AstrMessageEvent
        if not isinstance(event, AiocqhttpMessageEvent):
            return None
        scoped = _scoped_api(vars(event.bot)["_api"], timeout_seconds + 15)
        is_group = bool(event.get_group_id())
        target = event.get_group_id() if is_group else event.get_sender_id()
        if not re.fullmatch(r"[0-9]{1,20}", str(target)) or int(target) <= 0:
            return None
        raw = getattr(event.message_obj, "raw_message", None)
        self_id = raw.get("self_id") if isinstance(raw, dict) else None
        self_id = self_id or getattr(event.message_obj, "self_id", None)
        routing = {"self_id": self_id} if self_id else {}
        if not callable(getattr(event, "_parse_onebot_json", None)):
            return None
    except (ImportError, AttributeError, KeyError, TypeError, ValueError):
        return None

    payload = None

    async def send(chain, inline):
        nonlocal payload
        if payload is None:
            try:
                messages = await event._parse_onebot_json(chain)
                valid = (
                    isinstance(messages, list) and len(messages) == 6 and len(inline) == 2
                    and [item.get("type") for item in messages] == ["text", "text", "video", "text", "video", "text"]
                    and messages[0].get("data", {}).get("text") == VIDEO_HEADER
                    and messages[1].get("data", {}).get("text") == "\nA\n"
                    and messages[3].get("data", {}).get("text") == "\nB\n"
                    and messages[5].get("data", {}).get("text") in (VIDEO_VOTE_FOOTER, VIDEO_REVEAL_FOOTER)
                )
            except Exception:
                valid = False
            if not valid:
                raise _PreSendError("Unsupported native dual-video serialization")
            # Freeze both base64 videos and labels/footer once. Drop callback
            # URLs, thumbnails and serializer metadata; never reread/reencode
            # files or serialize a different message after a known rejection.
            payload = [
                {"type": "text", "data": {"text": VIDEO_HEADER}},
                {"type": "text", "data": {"text": "\nA\n"}},
                {"type": "video", "data": {"file": inline[0]}},
                {"type": "text", "data": {"text": "\nB\n"}},
                {"type": "video", "data": {"file": inline[1]}},
                {"type": "text", "data": {"text": messages[5]["data"]["text"]}},
            ]
        send.attempts += 1
        result = await scoped.call_action(
            "send_group_msg" if is_group else "send_private_msg",
            **{"group_id" if is_group else "user_id": int(target)},
            # Fresh containers protect the frozen payload from transport edits.
            message=[{"type": item["type"], "data": dict(item["data"])} for item in payload],
            timeout=int(timeout_seconds * 1000), **routing,
        )
        return result, AstrMessageEvent

    async def verify(native_id):
        readback = await scoped.call_action("get_msg", message_id=int(native_id), **routing)
        return _verified_dual_video_message(readback, native_id, payload)

    send.attempts = 0
    send.verify = verify
    return send


def _verified_dual_video_message(readback: Any, native_id: str, payload: Any) -> bool:
    """Fail closed on the known OneBot get_msg {message_id, message: list} shape.

    Native readback may replace inline video files with platform file identifiers;
    never compare or expose those values. No raw-string/content/schema guessing.
    """
    from .battle_reply import message_id
    try:
        if not isinstance(readback, dict) or not isinstance(payload, list):
            return False
        retcode = readback.get("retcode", 0)
        if (readback.get("status", "ok") != "ok" or isinstance(retcode, bool)
                or not isinstance(retcode, int) or retcode != 0
                or message_id(readback.get("message_id")) != native_id):
            return False
        messages = readback.get("message")
        if not isinstance(messages, list) or len(messages) > 16:
            return False
        # OneBot may coalesce adjacent text segments. Preserve positional text
        # around exactly two videos rather than insisting on six readback nodes.
        expected_text = [
            payload[0]["data"]["text"] + payload[1]["data"]["text"],
            payload[3]["data"]["text"], payload[5]["data"]["text"],
        ]
        text_blocks = ["", "", ""]
        videos = 0
        for actual in messages:
            if not isinstance(actual, dict) or not isinstance(actual.get("data"), dict):
                return False
            data = actual["data"]
            if actual.get("type") == "text":
                text = data.get("text")
                if not isinstance(text, str) or len(text_blocks[videos]) + len(text) > len(expected_text[videos]):
                    return False
                text_blocks[videos] += text
            elif actual.get("type") == "video":
                if videos >= 2 or not isinstance(data.get("file"), str) or not data["file"]:
                    return False
                videos += 1
            else:
                return False
        return videos == 2 and text_blocks == expected_text
    except Exception:
        return False


def _delivery_failure(code: str, attempts: int, uncertain: bool, *, confirmed: bool = False) -> VideoDeliveryFailure:
    messages = {
        "video_transport_unavailable": "无法安全传输双视频，未发送；没有启用回退",
        "video_transport_invalid": "双视频内容或传输大小校验失败，未发送",
        "video_send_rejected": "平台明确拒绝双视频消息，已停止发送；没有启用回退",
        "video_send_uncertain": "双视频消息回执不确定，已停止发送；请勿自动重发",
        "video_verification_uncertain": "平台已确认发送，但未能确认完整双视频呈现；已停止发送，不能投票或自动重发",
    }
    return VideoDeliveryFailure(messages[code], attempts, uncertain, code, confirmed)


def _send_policy(host: Any, config: Any) -> tuple[int, float]:
    """Use image Battle's shared policy, with finite/type-safe config fallback."""
    values = []
    for method_name, key, default, maximum, integer in (
        ("_send_max_attempts", "send_max_attempts", DEFAULT_SEND_MAX_ATTEMPTS, 6, True),
        ("_send_retry_delay", "send_retry_delay", DEFAULT_SEND_RETRY_DELAY, 30.0, False),
    ):
        method = getattr(host, method_name, None)
        value = method() if callable(method) else config.get(key, default)
        try:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError("Invalid retry policy")
            number = int(value) if integer else float(value)
            if not math.isfinite(number):
                raise ValueError("Invalid retry policy")
            value = max(1 if integer else 0.0, min(maximum, number))
        except (TypeError, ValueError, OverflowError):
            value = default
        values.append(value)
    return values[0], values[1]


async def deliver_complete_video_battle(host: Any, event: Any, result: Any):
    """Complete A/B sends; retry only known QQ rejection, never uncertainty.

    video_send_timeout (<=600s, default 180s) is the TOTAL budget, including local
    preparation, linear retry waits and read-only get_msg verification. Only the
    known get_msg message-list shape with both videos and positioned A/B text
    authorizes voting (adjacent text may be coalesced). qq_video_send_timeout
    optionally lowers each attempt's deadline; alone it also sets the total.
    Shared image _send_max_attempts/_send_retry_delay methods take precedence over
    config fallback (3 attempts, 2s delay; hard caps 6 and 30s respectively).
    Unsupported adapters/serialization fail closed. Caller must not yield/resend
    a failure. Files and result metadata remain owned and unchanged by the caller.
    """
    from .battle_reply import BattleMessageReceipt, message_id
    try:
        from astrbot.api.message_components import Plain, Video
        from astrbot.core.message.message_event_result import MessageChain
        config = getattr(host, "config", {}) or {}
        total_timeout = config.get("video_send_timeout", config.get("qq_video_send_timeout", DEFAULT_VIDEO_SEND_TIMEOUT))
        timeout = config.get("qq_video_send_timeout", total_timeout)
        if not _positive_number(total_timeout, 600) or not _positive_number(timeout, 600):
            raise ValueError("Invalid send timeout")
        timeout = min(float(timeout), float(total_timeout))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(total_timeout)
        max_attempts, delay = _send_policy(host, config)
        sender = prepare_qq_video_sender(event, timeout)
        if sender is None:
            return _delivery_failure("video_transport_unavailable", 0, False)
        components = result.chain
        if not isinstance(components, (list, tuple)) or len(components) > 16:
            raise ValueError("Invalid video chain")
        videos = [item for item in components if isinstance(item, Video)]
        if len(videos) != 2 or any(not isinstance(item, (Plain, Video)) for item in components):
            raise ValueError("Invalid video chain")
        paths = [_local_video_path(item) for item in videos]
        async with asyncio.timeout_at(deadline):
            inline = await asyncio.to_thread(_inline_videos, paths)
        footer = VIDEO_REVEAL_FOOTER if config.get("battle_allow_reveal_without_vote", False) else VIDEO_VOTE_FOOTER
        chain = MessageChain([
            Plain(VIDEO_HEADER), Plain("\nA\n"), Video.fromFileSystem(str(paths[0])),
            Plain("\nB\n"), Video.fromFileSystem(str(paths[1])), Plain(footer),
        ])
    except asyncio.CancelledError:
        raise
    except ImportError:
        return _delivery_failure("video_transport_unavailable", 0, False)
    except Exception:
        return _delivery_failure("video_transport_invalid", 0, False)

    attempts = 0
    while attempts < max_attempts:
        if loop.time() >= deadline:
            # No platform call yet, or the last call was a confirmed rejection.
            return _delivery_failure("video_send_rejected" if attempts else "video_transport_invalid", attempts, False)
        attempts += 1
        try:
            async with asyncio.timeout_at(min(deadline, loop.time() + timeout)):
                receipt, base_event = await sender(chain, inline)
        except asyncio.CancelledError:
            raise  # Cancellation never authorizes another send.
        except _PreSendError:
            return _delivery_failure("video_transport_unavailable", sender.attempts, False)
        except Exception as exc:
            # Serialization may consume the deadline before any platform call.
            # The prepared transport counts only actual call_action invocations.
            attempts = sender.attempts
            try:
                rejected = _confirmed_rejection(exc)
            except Exception:
                rejected = False
            if not rejected:
                return _delivery_failure("video_send_uncertain", attempts, True)
            if attempts >= max_attempts:
                return _delivery_failure("video_send_rejected", attempts, False)
            try:
                async with asyncio.timeout_at(deadline):
                    await asyncio.sleep(delay * attempts)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A deadline/wait failure after rejection is still certain:
                # no additional send has crossed the platform boundary.
                return _delivery_failure("video_send_rejected", attempts, False)
            continue

        native_id = ""
        if isinstance(receipt, dict):
            status_ok = receipt.get("status", "ok") == "ok"
            retcode = receipt.get("retcode", 0)
            retcode_ok = isinstance(retcode, int) and not isinstance(retcode, bool) and retcode == 0
            if status_ok and retcode_ok:
                native_id = message_id(receipt.get("message_id"))
        if not native_id:
            return _delivery_failure("video_send_uncertain", attempts, True)
        # A positive send receipt alone cannot prove QQ presented both videos.
        # After confirmation NO readback failure may retry or authorize voting.
        verified = False
        try:
            if loop.time() < deadline:
                async with asyncio.timeout_at(min(deadline, loop.time() + timeout)):
                    verified = await sender.verify(native_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if not verified:
            return _delivery_failure("video_verification_uncertain", attempts, True, confirmed=True)
        # Only local framework bookkeeping after confirmation, never event.send.
        # Metrics errors cannot invalidate a positive receipt or trigger a retry.
        try:
            remaining = deadline - loop.time()
            if remaining > 0:
                async with asyncio.timeout(min(remaining, 2.0)):
                    await base_event.send(event, chain)
        except Exception:
            pass
        return BattleMessageReceipt(native_id)


deliver_video_battle_message = deliver_complete_video_battle
