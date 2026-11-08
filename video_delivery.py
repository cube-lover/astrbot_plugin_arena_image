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
we use the adapter's component serializer, then build ONE native forward card
with two anonymous bot nodes, each containing exactly one explicit base64 video.
Never send a URI/signed URL or rely on a shared NapCat filesystem. The combined
raw-file cap is 32 MiB, NOT 64 MiB per video. A numeric outer message_id is
mandatory. No fallback: only a known, confirmed QQ rejection may retry the
identical frozen card within the configured attempt/deadline bounds. Timeout,
unknown receipt and cancellation never retry. An optional awaited receipt
callback persists the outer ID before read-only get_msg/get_forward_msg confirm
both ordered nodes. Tencent multiMsg JSON must bind meta.detail.resid to the
successful receipt's res_id/forward_id; get_forward_msg uses that resource,
never the numeric outer ID. Native video file_size must match frozen byte counts
and handles must differ. Size is not a content-hash proof: equal-sized distinct
source bytes fail closed because the installed readback lacks a verified hash.
Failed/unknown readback never permits another send or a votable receipt. Native
QQ presentation has not been live-tested by this implementation task.
"""
from __future__ import annotations

import asyncio
import base64
import copy
from dataclasses import dataclass
import hashlib
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
    message_id: str = ""  # Validated native OUTER ID only, even when verification fails.


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
    """Prepare a scoped aiocqhttp native-forward transport, without sending."""
    try:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        from astrbot.api.event import AstrMessageEvent
        if not isinstance(event, AiocqhttpMessageEvent):
            return None
        scoped = _scoped_api(vars(event.bot)["_api"], timeout_seconds + 15)
        is_group = bool(event.get_group_id())
        target = event.get_group_id() if is_group else event.get_sender_id()
        if isinstance(target, bool) or not re.fullmatch(r"[0-9]{1,20}", str(target)) or int(target) <= 0:
            return None
        raw = getattr(event.message_obj, "raw_message", None)
        self_id = raw.get("self_id") if isinstance(raw, dict) else None
        if self_id is None:
            self_id = getattr(event.message_obj, "self_id", None)
        # Custom nodes must use this event's bot, not the caller/model or an
        # invented account. Missing bot identity cannot be sent anonymously.
        if isinstance(self_id, bool) or not re.fullmatch(r"[0-9]{1,20}", str(self_id)) or int(self_id) <= 0:
            return None
        routing = {"self_id": self_id}
        if not callable(getattr(event, "_parse_onebot_json", None)):
            return None
    except (ImportError, AttributeError, KeyError, TypeError, ValueError):
        return None

    payload = None
    resource_id = None
    video_facts = None  # Private (byte count, SHA-256); never included in nodes.

    async def send(chain, inline):
        nonlocal payload, resource_id, video_facts
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
                    and all(isinstance(value, str) and re.fullmatch(r"base64://[A-Za-z0-9+/]+={0,2}", value)
                            for value in inline)
                )
            except Exception:
                valid = False
            if not valid:
                raise _PreSendError("Unsupported native video-forward serialization")
            # Retain facts from the frozen inline bytes, not mutable local
            # files or native filenames. Bound work inside the send deadline.
            try:
                video_facts = _inline_video_facts(inline)
            except Exception:
                raise _PreSendError("Invalid frozen video identity") from None
            # Freeze once. Strip URLs/thumbnails/metadata from serialization;
            # every known-rejection retry receives a fresh DEEP payload copy.
            payload = [
                {"type": "node", "data": {
                    "user_id": int(self_id), "nickname": "视频 Battle A",
                    "content": [
                        {"type": "text", "data": {"text": VIDEO_HEADER + "\nA\n"}},
                        {"type": "video", "data": {"file": inline[0]}},
                    ],
                }},
                {"type": "node", "data": {
                    "user_id": int(self_id), "nickname": "视频 Battle B",
                    "content": [
                        {"type": "text", "data": {"text": "\nB\n" + messages[5]["data"]["text"]}},
                        {"type": "video", "data": {"file": inline[1]}},
                    ],
                }},
            ]
        send.attempts += 1
        result = await scoped.call_action(
            "send_group_forward_msg" if is_group else "send_private_forward_msg",
            **{"group_id" if is_group else "user_id": int(target)},
            # Installed Ty.schema requires message BEFORE R2.check can
            # normalize messages -> message. Supply both native aliases with
            # independent copies; neither adapter edits nor retries can mutate
            # the frozen two-node card.
            message=copy.deepcopy(payload), messages=copy.deepcopy(payload),
            timeout=int(timeout_seconds * 1000), **routing,
        )
        if _native_api_ok(result):
            resource_id = _receipt_forward_resource(result)
        return result, AstrMessageEvent

    async def verify(native_id):
        # Direct aiocqhttp already unwraps the WebUI envelope. The numeric ID
        # identifies the outer message, NOT the Tencent multiMsg resource.
        outer = await scoped.call_action("get_msg", message_id=int(native_id), **routing)
        resid = _forward_outer_resource(outer, native_id, resource_id)
        if not resid:
            return False
        readback = await scoped.call_action("get_forward_msg", message_id=resid, **routing)
        return _verified_video_forward_message(readback, payload, video_facts)

    send.attempts = 0
    send.verify = verify
    return send


def _native_api_ok(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    retcode = value.get("retcode", 0)
    return (value.get("status", "ok") == "ok" and isinstance(retcode, int)
            and not isinstance(retcode, bool) and retcode == 0)


def _forward_resource_id(value: Any) -> str:
    return value if (isinstance(value, str) and 0 < len(value) <= 4096
                     and not any(char.isspace() or ord(char) < 32 for char in value)
                     and "://" not in value) else ""


def _receipt_forward_resource(receipt: dict) -> str | None:
    """None means absent; empty means invalid/conflicting explicit resources."""
    values = [receipt[key] for key in ("res_id", "forward_id") if key in receipt]
    if not values:
        return None
    resources = [_forward_resource_id(value) for value in values]
    return resources[0] if all(resources) and len(set(resources)) == 1 else ""


def _forward_outer_resource(readback: Any, native_id: str, receipt_resource: str | None) -> str:
    """Exact classic forward or Tencent JSON path, never recursive discovery.

    Tencent JSON requires a receipt resource. Legacy classic forward may supply
    its own explicit id when the receipt lacks resource fields, but any supplied
    receipt resource must match. Conflicting/invalid receipt fields fail closed.
    """
    from .battle_reply import message_id
    try:
        if (receipt_resource == "" or not _native_api_ok(readback)
                or message_id(readback.get("message_id")) != native_id):
            return ""
        segments = readback.get("message")
        if not isinstance(segments, list) or len(segments) != 1:
            return ""
        segment = segments[0]
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            return ""
        data = segment["data"]
        if segment.get("type") == "forward":
            resid = _forward_resource_id(data.get("id"))
        elif segment.get("type") == "json" and receipt_resource:
            raw = data.get("data")
            if not isinstance(raw, str) or not 0 < len(raw) <= 64 * 1024:
                return ""
            card = json.loads(raw)
            if not isinstance(card, dict) or card.get("app") != "com.tencent.multimsg":
                return ""
            meta = card.get("meta")
            detail = meta.get("detail") if isinstance(meta, dict) else None
            if not isinstance(detail, dict):
                return ""
            resid = _forward_resource_id(detail.get("resid"))
        else:
            return ""
        return resid if resid and (receipt_resource is None or resid == receipt_resource) else ""
    except Exception:
        return ""


def _inline_video_facts(inline: Any) -> tuple[tuple[int, str], ...]:
    """Private byte counts/digests of submitted bytes; never forward metadata."""
    if not isinstance(inline, (list, tuple)) or len(inline) != 2:
        raise ValueError("Invalid frozen videos")
    facts = []
    total = 0
    for value in inline:
        if (not isinstance(value, str) or not value.startswith("base64://")
                or not 0 < len(value) - 9 <= 4 * ((MAX_INLINE_VIDEO_BYTES + 2) // 3)):
            raise ValueError("Invalid frozen video")
        raw = base64.b64decode(value[9:], validate=True)
        total += len(raw)
        if not raw or total > MAX_INLINE_VIDEO_BYTES:
            raise ValueError("Invalid frozen video size")
        facts.append((len(raw), hashlib.sha256(raw).hexdigest()))
        del raw
    return tuple(facts)


def _native_video_size(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return 0
    if isinstance(value, str) and not re.fullmatch(r"[0-9]{1,10}", value):
        return 0
    number = int(value)
    return number if 0 < number <= MAX_INLINE_VIDEO_BYTES else 0


def _verified_video_forward_message(readback: Any, payload: Any, video_facts: Any = None) -> bool:
    """Only installed direct {messages: [{sender, message}, ...]} readback.

    Require two ordered anonymous bot nodes, one video each, exact aggregate
    text per node. Native file handles must be distinct and declared file_size
    must exactly match submitted bytes per lane. Size is NOT content identity:
    equal-sized distinct source digests cannot establish lane identity through
    this installed contract (no verified content hash), so fail closed. Identical
    source bytes may pass with distinct handles. Native may move text around its
    single video. Extra/nested nodes or non-text/video segments fail closed.
    """
    try:
        if not _native_api_ok(readback) or not isinstance(payload, list) or len(payload) != 2:
            return False
        if video_facts is None:
            video_facts = _inline_video_facts([node["data"]["content"][1]["data"]["file"] for node in payload])
        if (len(video_facts) != 2
                or (video_facts[0][0] == video_facts[1][0] and video_facts[0][1] != video_facts[1][1])):
            return False
        nodes = readback.get("messages")
        if not isinstance(nodes, list) or len(nodes) != 2:
            return False
        handles = set()
        for node, expected, facts in zip(nodes, payload, video_facts):
            if not isinstance(node, dict):
                return False
            sender = node.get("sender")
            identity = expected["data"]
            user_id = sender.get("user_id") if isinstance(sender, dict) else None
            if (isinstance(user_id, bool) or not isinstance(user_id, (int, str))
                    or not re.fullmatch(r"[0-9]{1,20}", str(user_id))
                    or int(user_id) != identity["user_id"]
                    or sender.get("nickname") != identity["nickname"]):
                return False
            segments = node.get("message")
            if not isinstance(segments, list) or not 2 <= len(segments) <= 16:
                return False
            expected_text = identity["content"][0]["data"]["text"]
            actual_text = ""
            videos = 0
            for segment in segments:
                if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
                    return False
                data = segment["data"]
                if segment.get("type") == "text":
                    text = data.get("text")
                    if not isinstance(text, str) or len(actual_text) + len(text) > len(expected_text):
                        return False
                    actual_text += text
                elif segment.get("type") == "video":
                    handle = data.get("file")
                    if (videos or not isinstance(handle, str) or not 0 < len(handle) <= 32768
                            or not handle.strip() or handle.strip() in handles
                            or _native_video_size(data.get("file_size")) != facts[0]):
                        return False
                    handles.add(handle.strip())
                    videos += 1
                else:
                    return False
            if videos != 1 or actual_text != expected_text:
                return False
        return True
    except Exception:
        return False


def _delivery_failure(
    code: str, attempts: int, uncertain: bool, *, confirmed: bool = False, message_id: str = "",
) -> VideoDeliveryFailure:
    from .battle_reply import message_id as native_message_id
    messages = {
        "video_transport_unavailable": "无法安全传输双视频，未发送；没有启用回退",
        "video_transport_invalid": "双视频内容或传输大小校验失败，未发送",
        "video_send_rejected": "平台明确拒绝双视频消息，已停止发送；没有启用回退",
        "video_send_uncertain": "双视频消息回执不确定，已停止发送；请勿自动重发",
        "video_verification_uncertain": "平台已确认发送，但未能确认完整双视频呈现；已停止发送，不能投票或自动重发",
    }
    return VideoDeliveryFailure(messages[code], attempts, uncertain, code, confirmed,
                                native_message_id(message_id))


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


async def deliver_complete_video_battle(host: Any, event: Any, result: Any, receipt_callback=None):
    """Complete A/B sends; retry only known QQ rejection, never uncertainty.

    video_send_timeout (<=600s, default 180s) is the TOTAL budget, including local
    preparation, linear retry waits, optional awaited receipt callback, and
    read-only get_msg + get_forward_msg verification (one call each, no polling
    or business retry). Only the bound Tencent multiMsg JSON (or classic
    forward) outer plus two complete anonymous A/B bot nodes authorize voting.
    receipt_callback(native_id), or the optional
    host._video_native_receipt hook, runs after a positive receipt and BEFORE
    verification. Callback failure fails closed with the outer message_id;
    cancellation propagates, never resends. qq_video_send_timeout
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
        callback = receipt_callback if receipt_callback is not None else getattr(host, "_video_native_receipt", None)
        if callback is not None and not callable(callback):
            raise ValueError("Invalid receipt callback")
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
            if not attempts:
                return _delivery_failure("video_transport_invalid", 0, False)
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
        if _native_api_ok(receipt):
            native_id = message_id(receipt.get("message_id"))
        if not native_id:
            return _delivery_failure("video_send_uncertain", attempts, True)
        # Persist the native outer ID even if verification is interrupted.
        # Await exactly once, never fire/forget. After confirmation NO callback
        # or readback failure may authorize another send or a votable receipt.
        verified = False
        try:
            async with asyncio.timeout_at(min(deadline, loop.time() + timeout)):
                if callback is not None:
                    await callback(native_id)
                if loop.time() < deadline:
                    verified = await sender.verify(native_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if not verified:
            return _delivery_failure("video_verification_uncertain", attempts, True,
                                     confirmed=True, message_id=native_id)
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
