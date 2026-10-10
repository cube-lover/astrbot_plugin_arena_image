"""Per-image OneBot deadlines without mutating the shared AstrBot client."""
from __future__ import annotations

from typing import Any


def _scoped_api(api: Any, timeout_seconds: float) -> Any:
    """Copy aiocqhttp API wrappers, sharing connections but not timeout state.

    aiocqhttp has separate HTTP/reverse-WS deadlines. Changing the original
    object's timeout and restoring it in finally races with concurrent plugins.
    Inspect instance storage rather than getattr: Api.__getattr__ synthesizes
    callable OneBot actions for every unknown attribute.
    """
    state = vars(api)
    # copy.copy probes __setstate__; Api.__getattr__ turns that into a remote
    # API action too. These Python wrappers have ordinary instance dictionaries.
    scoped = object.__new__(type(api))
    vars(scoped).update(state)
    supported = False
    if "_timeout_sec" in state:
        scoped._timeout_sec = max(float(state["_timeout_sec"]), timeout_seconds)
        supported = True
    for name in ("_http_api", "_wsr_api"):
        if state.get(name) is not None:
            setattr(scoped, name, _scoped_api(state[name], timeout_seconds))
            supported = True
    if not supported:
        raise TypeError("Unsupported aiocqhttp API wrapper")
    return scoped


def prepare_qq_image_sender(event: Any, timeout_seconds: float):
    """Return a scoped sender for the standard adapter, else keep event.send.

    Preparation is entirely local. Once a send starts, errors propagate to the
    caller: there is never a second transport fallback after an uncertain send.
    """
    try:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )
        from astrbot.api.event import AstrMessageEvent
    except ImportError:
        return None
    if not isinstance(event, AiocqhttpMessageEvent):
        return None
    try:
        scoped = _scoped_api(vars(event.bot)["_api"], timeout_seconds + 15)
        is_group = bool(event.get_group_id())
        target = event.get_group_id() if is_group else event.get_sender_id()
        if not str(target).isdigit():
            return None
        raw = getattr(event.message_obj, "raw_message", None)
        self_id = raw.get("self_id") if isinstance(raw, dict) else None
        self_id = self_id or getattr(event.message_obj, "self_id", None)
        routing = {"self_id": self_id} if self_id else {}
    except (AttributeError, KeyError, TypeError, ValueError):
        return None

    async def send(chain):
        # Same component conversion as AstrBot, including base64 across Docker
        # containers. A local plugin path is not a NapCat filesystem path.
        messages = await event._parse_onebot_json(chain)
        if not messages:
            raise ValueError("Empty image message")
        action = "send_group_msg" if is_group else "send_private_msg"
        target_key = "group_id" if is_group else "user_id"
        result = await scoped.call_action(
            action,
            **{target_key: int(target)},
            message=messages,
            timeout=int(timeout_seconds * 1000),  # NapCat uses milliseconds.
            **routing,
        )
        # NapCat returns message_id only after its matching SUCCESS update.
        # Missing data/async acknowledgment is not proof of delivery.
        if not isinstance(result, dict) or not result.get("message_id"):
            raise RuntimeError("QQ image receipt has no confirmed message_id")
        # Match the standard adapter's post-send metrics/has_send_oper update.
        # Bookkeeping failure must never turn a positive receipt into a resend.
        try:
            await AstrMessageEvent.send(event, chain)
        except Exception:
            from astrbot import logger
            logger.warning("[arena_image] QQ 已确认图片发送，消息统计更新失败")
        return result

    return send
