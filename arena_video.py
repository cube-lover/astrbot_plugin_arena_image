"""Validated anonymous video Battle data, shared by the server video client.

The official create envelope and a successful native A/B video session were
observed in an existing server browser. Exact-session readback and bounded MP4
materialization were tested on that result. A later candidate create with one
reference image reached the upstream endpoint but returned HTTP 429; it did not
prove programmatic generation acceptance or QQ dual-video delivery.

Readback uses verified /c/{id} Flight: the JSON evaluation endpoint returned 404
in the observed deployment. Persisted Flight can omit modality and carry
metadata.videoRun/videoDuration, whereas native terminal state can omit metadata.
Missing modality requires explicit video context plus per-side video evidence;
explicit non-video modality is rejected. A terminal resume-workflow 404 alone
cannot override exact-session successes or establish failure.

This module validates supplied data only, without requests, downloads, model
selection or votes. URLs and stream finish markers are independent. Only exact
session status=success plus a valid MP4 attachment completes a side. Errors are
fixed anonymous messages. HTTPS checking here is syntactic; authorized DNS and
actual media checks are enforced separately by video_delivery.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import math
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from .arena_battle import battle_context
from .bridge_client import BridgeError

MAX_INPUT_BYTES = 1024 * 1024
MAX_LINES = 4096
MAX_LINE_BYTES = 64 * 1024
MAX_DEPTH = 24
MAX_NODES = 20000
MAX_URL_CHARS = 16384
MAX_TEXT_CHARS = 65536
MAX_VIDEOS_PER_SIDE = 16
MAX_MESSAGES = 256

_STATUSES = frozenset({"pending", "resampling", "success", "failed", "flagged", "stopped"})
_FINISH_REASONS = frozenset({"stop", "length", "content-filter", "error", "other", "unknown"})
_INVALID = "匿名视频协议数据无效，未确认生成完成"
_UPSTREAM = "匿名视频上游返回错误，未确认生成完成"
_NO_VIDEO = "匿名视频消息没有可用视频，未确认生成完成"
_STATUS_ERRORS = {
    "failed": "匿名视频生成失败，未揭晓模型",
    "flagged": "匿名视频结果已被标记，未确认生成完成",
    "stopped": "匿名视频生成已停止，未确认生成完成",
}
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _invalid(*, limit: bool = False) -> BridgeError:
    return BridgeError(
        "匿名视频协议输入超过限制" if limit else _INVALID,
        payload={
            "error": {
                "code": "video_protocol_limit" if limit else "video_protocol_invalid",
            }
        },
    )


def _byte_size(value: str) -> int:
    # Check character length first to avoid encoding an unbounded allocation.
    if len(value) > MAX_INPUT_BYTES:
        raise _invalid(limit=True)
    size = None
    with contextlib.suppress(UnicodeError):
        size = len(value.encode("utf-8"))
    if size is None:
        raise _invalid()
    return size


def _bounded_json(value: Any) -> None:
    """Validate an already-decoded JSON tree without recursive traversal."""
    stack = [(value, 0)]
    seen = set()
    nodes = size = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise _invalid(limit=True)
        if isinstance(item, (dict, list)):
            if id(item) in seen:
                raise _invalid()  # Cycles/aliases are not a decoded JSON tree.
            seen.add(id(item))
            if len(item) + nodes + len(stack) > MAX_NODES:
                raise _invalid(limit=True)
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise _invalid()
                    size += _byte_size(key)
                    stack.append((child, depth + 1))
            else:
                stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            size += _byte_size(item)
        elif item is None or isinstance(item, bool):
            size += 4
        elif isinstance(item, int):
            if item.bit_length() > 256:
                raise _invalid()
            size += 80
        elif isinstance(item, float) and math.isfinite(item):
            size += 32
        else:
            raise _invalid()
        if size > MAX_INPUT_BYTES:
            raise _invalid(limit=True)


def _uuid(value: Any) -> str:
    result = None
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            result = str(uuid.UUID(value))
    if result is None:
        raise _invalid()
    return result


def video_context(context: dict) -> dict[str, str]:
    """Extend battle_context's allowlist, never pass arbitrary context through.

    Preserve its normalized IDs/browser_scope, add mode=battle/modality=video
    (reject conflicting explicit values), and preserve optional userMessageId
    only after UUID validation. Workflow run IDs belong to readback sides, not
    model selections or transport context. Caller input is never mutated.
    """
    _bounded_json(context)
    if not isinstance(context, dict):
        raise _invalid()
    scope = context.get("browser_scope")
    if (
        not isinstance(scope, str)
        or not scope.strip()
        or len(scope) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in scope)
        or context.get("mode", "battle") != "battle"
        or context.get("modality", "video") != "video"
    ):
        raise _invalid()
    # Validate before delegating so base-helper errors cannot contain raw IDs.
    ids = {
        key: _uuid(context.get(key)) for key in ("evaluationSessionId", "messageAId", "messageBId")
    }
    if len(set(ids.values())) != 3:
        raise _invalid()
    result = battle_context({**ids, "browser_scope": scope})
    result.update(mode="battle", modality="video")
    if "userMessageId" in context:
        user_id = _uuid(context["userMessageId"])
        if user_id in ids.values():
            raise _invalid()
        result["userMessageId"] = user_id
    return result


def _https_url(value: Any) -> str:
    """Return an unchanged usable URL or empty string; never echo invalid input."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_URL_CHARS
        or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
        or "\\" in value
        or re.search(r"%(?![0-9a-fA-F]{2})|%(?:00|0a|0d)", value, re.IGNORECASE)
    ):
        return ""
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (
            parts.scheme != "https"
            or not host
            or "@" in parts.netloc
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or (parts.port is not None and not 1 <= parts.port <= 65535)
            or parts.netloc.endswith(":")
        ):
            return ""
        if ":" in host:
            ipaddress.IPv6Address(host)
            if "%" in host:  # No IPv6 zone identifiers.
                return ""
        elif len(host) > 253 or not all(
            _HOST_LABEL.fullmatch(label) for label in host.rstrip(".").split(".")
        ):
            return ""
    except ValueError:
        return ""
    return value


def _video_record(value: Any, *, session: bool = False) -> dict[str, str] | None:
    if not isinstance(value, dict):
        raise _invalid()
    mime_key = "contentType" if session else "mimeType"
    if session:
        mime = value.get(mime_key)
        if not isinstance(mime, str):
            raise _invalid()
        if mime != "video/mp4":
            if mime.startswith("video/"):
                raise _invalid()  # Unsupported video, not evidence of success.
            return None  # Non-video attachments are not video outputs.
    elif value.get("type") != "video":
        return None  # Do not recursively collect URLs from unrelated records.
    if value.get(mime_key) != "video/mp4":
        raise _invalid()
    url = _https_url(value.get("url"))
    thumbnail = value.get("thumbnailUrl")
    if not url or (thumbnail not in (None, "") and not _https_url(thumbnail)):
        raise _invalid()
    return {"url": url, "mimeType": "video/mp4", "thumbnailUrl": thumbnail or ""}


def _add_video(lane: dict, video: dict[str, str]) -> None:
    for prior in lane["videos"]:
        if prior["url"] == video["url"]:
            if prior != video:
                raise _invalid()
            return
    if len(lane["videos"]) >= MAX_VIDEOS_PER_SIDE:
        raise _invalid(limit=True)
    lane["videos"].append(video)


def _lane() -> dict[str, Any]:
    return {"text": "", "videos": [], "error": "", "finish_reason": ""}


def _fail_lane(lane: dict, message: str) -> None:
    # Sticky fail-closed state: later URLs/finish cannot erase an earlier error.
    lane.update(text="", videos=[], error=message)


def parse_video_stream(body: str | None) -> dict[str, dict[str, Any]]:
    """Parse a/b lanes with 0/2/3/d prefixes, optionally wrapped in SSE data:.

    Returns text/videos/error/finish_reason per side. There is deliberately no
    completion flag: a URL or finish marker is not explicit session success.
    Malformed recognized rows invalidate only their lane; global input limits
    raise a sanitized BridgeError. Unrelated prefixes/events are ignored.
    """
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise _invalid()
    if _byte_size(body) > MAX_INPUT_BYTES:
        raise _invalid(limit=True)
    lines = body.splitlines()
    if len(lines) > MAX_LINES:
        raise _invalid(limit=True)
    lanes = {side: _lane() for side in ("a", "b")}
    for raw in lines:
        if _byte_size(raw) > MAX_LINE_BYTES:
            raise _invalid(limit=True)
        line = raw.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if len(line) < 3 or line[0] not in lanes or line[1:3] not in {"0:", "2:", "3:", "d:"}:
            continue
        lane = lanes[line[0]]
        prefix = line[1]
        if lane["error"]:
            continue
        if prefix == "3":
            # Even malformed error payloads must never escape anonymously.
            _fail_lane(lane, _UPSTREAM)
            continue
        value = None
        decoded = False
        try:
            value = json.loads(line[3:])
            decoded = True
        except (ValueError, RecursionError):
            pass
        if not decoded:
            _fail_lane(lane, _INVALID)
            continue
        try:
            _bounded_json(value)
            if prefix == "0":
                if not isinstance(value, str):
                    raise _invalid()
                if len(lane["text"]) + len(value) > MAX_TEXT_CHARS:
                    raise _invalid(limit=True)
                lane["text"] += value
            elif prefix == "2":
                records = value if isinstance(value, list) else [value]
                for record in records:
                    video = _video_record(record)
                    if video is not None:
                        _add_video(lane, video)
            elif prefix == "d":
                if (
                    not isinstance(value, dict)
                    or not isinstance(value.get("finishReason"), str)
                    or value["finishReason"] not in _FINISH_REASONS
                ):
                    raise _invalid()
                reason = value["finishReason"]
                if lane["finish_reason"] and lane["finish_reason"] != reason:
                    raise _invalid()
                lane["finish_reason"] = reason
                if reason in {"error", "content-filter"}:
                    _fail_lane(lane, _UPSTREAM)
        except BridgeError:
            _fail_lane(lane, _INVALID)
    for lane in lanes.values():
        lane["text"] = lane["text"].strip()
    return lanes


def validate_video_session(data: Any, context: dict) -> dict[str, Any]:
    """Validate the exact decoded/extracted evaluation, not a wrapper search.

    Accept native state or a session already verified/extracted from /c/{id}
    Flight. Do not assume the direct evaluation GET succeeded. Missing modality
    is allowed ONLY with explicitly video caller context and video evidence on
    each side: a successful MP4 attachment or a known videoRun on a pending/
    resampling assistant. A present null/non-video modality fails closed.

    Both expected assistants must match ID, evaluationSessionId and a/b position
    exactly; a missing/mismatched/duplicate side invalidates the entire readback.
    Status is mandatory and never inferred from URLs, HTTP status or metadata.
    Only successful messages' experimental_attachments are parsed. Other
    statuses may carry stale artifacts, which are intentionally not returned.

    metadata.videoRun.runId is optional and treated as a bounded opaque workflow
    identifier (not assumed to be a UUID). No content/model/provider/feedback
    fields are returned. A side completes only with explicit success and video;
    the Battle completes only when both sides do. A raw success without video
    remains unconfirmed rather than being fabricated into success.
    """
    checked_context = video_context(context)
    _bounded_json(data)
    if not isinstance(data, dict) or any(
        (
            data.get("id") != checked_context["evaluationSessionId"],
            data.get("mode") != "battle",
        )
    ):
        raise _invalid()
    missing_modality = "modality" not in data
    if missing_modality:
        # Do not let video_context's default supply evidence absent in caller input.
        if context.get("modality") != "video":
            raise _invalid()
    elif data["modality"] != "video":
        raise _invalid()
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise _invalid()
    if len(messages) > MAX_MESSAGES:
        raise _invalid(limit=True)
    if any(not isinstance(message, dict) for message in messages):
        raise _invalid()
    sides = {}
    for side in ("a", "b"):
        expected_id = checked_context["message" + side.upper() + "Id"]
        matches = [message for message in messages if message.get("id") == expected_id]
        occupants = [
            message
            for message in messages
            if (message.get("role") == "assistant" and message.get("participantPosition") == side)
        ]
        if len(matches) != 1 or len(occupants) != 1 or occupants[0] is not matches[0]:
            raise _invalid()
        message = matches[0]
        status = message.get("status")
        if (
            message.get("evaluationSessionId") != checked_context["evaluationSessionId"]
            or message.get("role") != "assistant"
            or message.get("participantPosition") != side
            or not isinstance(status, str)
            or status not in _STATUSES
        ):
            raise _invalid()
        lane = _lane()
        lane.update(status=status, run_id="", completed=False)
        metadata = message.get("metadata")
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise _invalid()
            run = metadata.get("videoRun")
            if run is not None:
                if not isinstance(run, dict):
                    raise _invalid()
                run_id = run.get("runId")
                if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
                    raise _invalid()
                lane["run_id"] = run_id
        if status == "success":
            attachments = message.get("experimental_attachments")
            if not isinstance(attachments, list):
                raise _invalid()
            for attachment in attachments:
                video = _video_record(attachment, session=True)
                if video is not None:
                    _add_video(lane, video)
            lane["completed"] = bool(lane["videos"])
            if not lane["completed"]:
                lane["error"] = _NO_VIDEO
        else:
            lane["error"] = _STATUS_ERRORS.get(status, "")
        if missing_modality and not (
            lane["completed"] or (status in {"pending", "resampling"} and lane["run_id"])
        ):
            raise _invalid()  # Context alone cannot turn an image Battle into video.
        sides[side] = lane
    return {
        "evaluation_id": checked_context["evaluationSessionId"],
        "sides": sides,
        "context": checked_context,
        "completed": all(lane["completed"] for lane in sides.values()),
    }
