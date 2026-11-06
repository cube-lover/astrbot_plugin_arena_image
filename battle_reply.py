"""Scoped quoted-message intent and confirmed receipt types for Battle."""
from __future__ import annotations

import re
from dataclasses import dataclass

from astrbot.api.message_components import Plain, Reply


def message_id(value) -> str:
    """Only bounded native OneBot message identifiers, never URLs or payloads."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return ""
    value = str(value).strip()
    return value if re.fullmatch(r"-?[0-9]{1,20}", value) and int(value) != 0 else ""


def quoted_message_id(event) -> str:
    """Read the quote identifier only; quoted text/images are not instructions."""
    components = getattr(getattr(event, "message_obj", None), "message", []) or []
    replies = [component for component in components if isinstance(component, Reply)]
    if not replies:
        return ""
    ids = {message_id(getattr(component, "id", None)) for component in replies}
    if len(ids) != 1 or "" in ids:
        raise ValueError("引用消息标识不完整或不唯一，未提交投票")
    return ids.pop()


def reply_intent(event) -> str:
    # WakingCheckStage may strip a command prefix from event.message_str.
    # Use the independent incoming Plain components, not that mutable string
    # and never Reply.chain/message_str (which are the quoted message's text).
    components = getattr(getattr(event, "message_obj", None), "message", []) or []
    text = "".join(str(component.text or "") for component in components
                   if isinstance(component, Plain)).strip()
    return text.casefold() if text.casefold() in {"a", "b", "不投票"} else ""


class BattleReplyFilter:
    """Match only exact A/B/不投票 with a quote, never bare ordinary letters."""

    def __init__(self, raise_error=True):
        self.raise_error = raise_error

    def filter(self, event, cfg) -> bool:
        if not reply_intent(event):
            return False
        # Do not throw at filter time: an invalid quote can be ignored without
        # preventing unrelated handlers. The command boundary validates again.
        try:
            return bool(quoted_message_id(event))
        except ValueError:
            return False


@dataclass(frozen=True)
class BattleMessageReceipt:
    """A confirmed combined send with its native platform message identifier."""

    message_id: str
