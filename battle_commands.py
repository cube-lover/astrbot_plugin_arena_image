"""Blind A/B commands, independent of main.py and the transport implementation.

Host contract: _battle_client(), config, data_dir, _generation_lock,
_active_generations, _collect_input_images(), _materialize_output(),
_prune_outputs(). Call _init_battles() once from the host constructor.

Only opaque JSON context and small session metadata are persisted. ``vote`` is
None for an incomplete/failed comparison, "" for a votable one, and a/b after a
successful vote. ``random_choice`` is optional in legacy sessions and records a
sticky, explicitly authorized random A/B preference before any network request.
This preserves eligibility across restarts without saving outputs, side errors,
prompts, or the raw response. transport_mode belongs to our envelope, not to the
opaque protocol context.
"""

# Broad catches at command boundaries are intentional: transports are duck-typed,
# and every user-facing failure must be reported without logging opaque context.
# ruff: noqa: BLE001

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from astrbot.core.star.filter.command import GreedyStr

from .battle_reply import BattleMessageReceipt, message_id, quoted_message_id, reply_intent

MAX_BATTLE_SESSIONS = 100
MAX_CONTEXT_BYTES = 64 * 1024
MAX_SESSION_FILE_BYTES = 8 * 1024 * 1024
_SESSION_FIELDS = (
    "context",
    "evaluation_id",
    "owner",
    "chat",
    "timestamp",
    "vote",
    "names",
    "transport_mode",
    "random_choice",
    "platform_message_id",
)


def _display_error(exc: Exception, *, limit: int = 300) -> str:
    """Bound diagnostics, suppress payload dumps and redact common secrets/URLs.

    Do not import main's helper: main imports this mixin. Side error payloads
    never pass through this formatter, since they can contain model identities.
    """
    text = str(exc).strip() or type(exc).__name__
    if any(char in text for char in "{}[]"):
        return "错误详情含内部数据，已隐藏"
    text = re.sub(
        r"(?:https?://|data:|base64://|file://)\S+",
        "[地址已隐藏]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bBearer\s+\S+", "Bearer [已隐藏]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:[\w-]*(?:token|secret|password|cookie|authorization)|api[_-]?key)"
        r"\s*[:=]\s*[^\s,;]+",
        "[凭据已隐藏]",
        text,
        flags=re.IGNORECASE,
    )
    return text if len(text) <= limit else text[:limit] + "…"


def _integer(value, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _argument(value) -> str:
    # AstrBot uses the GreedyStr type itself as the default argument sentinel.
    return "" if value is GreedyStr else str(value or "").strip()


class BattleCommandsMixin:
    def _init_battles(self) -> None:
        self._battle_sessions_file = Path(self.data_dir) / "battle_sessions.json"
        self._battle_sessions: dict[str, dict] = {}
        self._battle_latest: dict[tuple[str, str], str] = {}
        self._battle_locks: dict[str, asyncio.Lock] = {}
        self._battle_busy: dict[str, int] = {}
        self._battle_config_lock = asyncio.Lock()
        self._battle_pending_outputs: set[Path] = set()
        self._battle_load_error = ""
        try:
            if self._battle_sessions_file.stat().st_size > MAX_SESSION_FILE_BYTES:
                raise ValueError("会话文件过大")
            raw = json.loads(self._battle_sessions_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("会话文件格式错误")
            for code, entry in raw.items():
                if not re.fullmatch(r"[0-9A-F]{6}", code) or not isinstance(
                    entry, dict
                ):
                    continue
                try:
                    cleaned = {
                        key: entry[key] for key in _SESSION_FIELDS
                        if key not in {"random_choice", "platform_message_id"}
                    }
                    cleaned["random_choice"] = entry.get("random_choice", "")
                    cleaned["platform_message_id"] = entry.get("platform_message_id", "")
                    self._battle_validate_session(cleaned)
                except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
                    continue
                self._battle_sessions[code] = cleaned
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            # Never log a context or dump a malformed JSON document.
            self._battle_load_error = "读取对战会话失败：" + _display_error(exc)
        self._battle_trim()

    def _battle_ttl(self) -> int:
        return _integer(self.config.get("battle_session_ttl"), 86400, 1, 31536000)

    def _battle_mode(self) -> str:
        getter = getattr(self, "_transport_mode", None)
        if callable(getter):
            return str(getter())
        mode = str(self.config.get("transport_mode") or "bridge").strip().casefold()
        return mode if mode in {"direct", "userscript"} else "bridge"

    @staticmethod
    def _battle_identity(event: AstrMessageEvent) -> tuple[str, str]:
        # Fail closed rather than collapsing missing IDs into a shared owner.
        chat = str(getattr(event, "unified_msg_origin", "") or "").strip()
        sender = getattr(event, "get_sender_id", None)
        owner = str(sender() or "").strip() if callable(sender) else ""
        if not chat or not owner:
            raise ValueError("无法识别聊天或发起人，不能操作对战")
        return chat, owner

    @staticmethod
    def _battle_copy_context(context) -> dict:
        if not isinstance(context, dict):
            raise TypeError("上游没有返回有效的对战会话")
        encoded = json.dumps(context, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise ValueError("上游对战会话过大")
        # Deep copy: a client may mutate the context passed to vote/reveal.
        return json.loads(encoded)

    @staticmethod
    def _battle_validate_names(names) -> dict[str, str]:
        if not isinstance(names, dict):
            raise TypeError("上游没有返回完整的揭晓名称")
        cleaned = {}
        for side in ("a", "b"):
            name = names.get(side)
            if not isinstance(name, str) or not name.strip() or len(name) > 300:
                raise ValueError("上游没有返回完整的揭晓名称")
            cleaned[side] = name.strip()
        return cleaned

    def _battle_validate_session(self, entry: dict) -> None:
        self._battle_copy_context(entry["context"])
        for key in ("evaluation_id", "owner", "chat", "transport_mode"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise ValueError("会话字段无效")
        stamp = entry["timestamp"]
        if (
            isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)
        ):
            raise ValueError("会话时间无效")
        if stamp > time.time() + 60 or stamp <= 0:
            raise ValueError("会话时间无效")
        if entry["vote"] not in (None, "", "a", "b"):
            raise ValueError("投票状态无效")
        choice = entry["random_choice"]
        if choice not in ("", "a", "b") or (
            choice and entry["vote"] not in ("", choice)
        ):
            raise ValueError("随机投票状态无效")
        native_id = entry.get("platform_message_id", "")
        if not isinstance(native_id, str) or (native_id and message_id(native_id) != native_id):
            raise ValueError("平台消息标识无效")
        entry["platform_message_id"] = native_id
        if entry["names"] is not None:
            entry["names"] = self._battle_validate_names(entry["names"])

    def _battle_trim(self) -> bool:
        now = time.time()
        removed = False
        for code, entry in list(self._battle_sessions.items()):
            if (
                not self._battle_busy.get(code)
                and now - entry["timestamp"] >= self._battle_ttl()
            ):
                del self._battle_sessions[code]
                removed = True
        ordered = sorted(
            self._battle_sessions,
            key=lambda key: self._battle_sessions[key]["timestamp"],
        )
        for code in ordered:
            if len(self._battle_sessions) <= MAX_BATTLE_SESSIONS:
                break
            if not self._battle_busy.get(code):
                del self._battle_sessions[code]
                removed = True
        self._battle_latest.clear()
        for code in ordered:
            if code in self._battle_sessions:
                entry = self._battle_sessions[code]
                self._battle_latest[(entry["chat"], entry["owner"])] = code
        for code in list(self._battle_locks):
            if code not in self._battle_sessions and not self._battle_busy.get(code):
                del self._battle_locks[code]
        return removed

    def _battle_save(self) -> None:
        # No awaits in snapshot/replace: all handlers run on the same event loop.
        # A unique temporary file plus os.replace avoids partial JSON on restart.
        self._battle_trim()
        payload = {
            code: {key: entry[key] for key in _SESSION_FIELDS}
            for code, entry in self._battle_sessions.items()
        }
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
        if len(encoded.encode("utf-8")) > MAX_SESSION_FILE_BYTES:
            raise ValueError("对战会话文件超过容量上限")
        self._battle_sessions_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self._battle_sessions_file.with_name(
            f".battle_sessions.{secrets.token_hex(6)}.tmp"
        )
        try:
            with temp.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self._battle_sessions_file)
        finally:
            temp.unlink(missing_ok=True)

    def _battle_lookup(self, event, requested: str) -> tuple[str, dict]:
        chat, owner = self._battle_identity(event)
        if self._battle_trim():
            self._battle_save()
        code = (
            requested.upper()
            if requested
            else self._battle_latest.get((chat, owner), "")
        )
        entry = self._battle_sessions.get(code)
        if entry is None or entry["chat"] != chat or entry["owner"] != owner:
            raise ValueError(
                "找不到你的有效对战，编号不存在、已过期或不属于当前聊天及发起人"
            )
        if time.time() - entry["timestamp"] >= self._battle_ttl():
            raise ValueError("这场对战已过期，请重新发起")
        if entry["transport_mode"] != self._battle_mode():
            raise ValueError("这场对战的传输通道已切换，请切回原通道或重新发起对战")
        return code, entry

    def _battle_reply_code(self, event, native_id: str) -> str:
        chat, _ = self._battle_identity(event)
        if self._battle_trim():
            self._battle_save()
        # Never infer the pair from latest, quoted text or an image URL.
        matches = [code for code, entry in self._battle_sessions.items()
                   if entry["chat"] == chat and entry.get("platform_message_id") == native_id]
        if len(matches) != 1:
            raise ValueError("引用的消息不是有效对战结果，或已过期；未提交投票")
        code = matches[0]
        self._battle_lookup(event, code)
        return code

    async def battle_reply(self, event: AstrMessageEvent):
        intent = reply_intent(event)
        if not intent:
            return
        try:
            native_id = quoted_message_id(event)
            if not native_id:
                return
            code = self._battle_reply_code(event, native_id)
        except Exception as exc:
            yield event.plain_result("引用投票失败：" + _display_error(exc))
            return
        generator = (self.battle_reveal(event, code) if intent == "不投票"
                     else self.battle_vote(event, f"{intent.upper()} {code}"))
        try:
            async for result in generator:
                yield result
        finally:
            await generator.aclose()

    def _battle_quoted_request(self, event, requested: str) -> str:
        native_id = quoted_message_id(event)
        if not native_id:
            return requested
        quoted = self._battle_reply_code(event, native_id)
        if requested and requested.upper() != quoted:
            raise ValueError("命令编号与引用的对战不一致，未提交投票")
        return quoted

    @asynccontextmanager
    async def _battle_guard(self, code: str):
        lock = self._battle_locks.setdefault(code, asyncio.Lock())
        self._battle_busy[code] = self._battle_busy.get(code, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._battle_busy[code] - 1
            if remaining:
                self._battle_busy[code] = remaining
            else:
                del self._battle_busy[code]
            self._battle_trim()

    def _battle_new_id(self) -> str:
        for _ in range(100):
            code = secrets.token_hex(3).upper()
            if code not in self._battle_sessions and code not in self._battle_locks:
                return code
        raise ValueError("暂时无法分配对战编号，请稍后重试")

    def _battle_disabled(self) -> bool:
        return not self.config.get("battle_enabled", True)

    async def _battle_prepare_side(self, side, *, paths=None) -> dict:
        """Download under the generation lock, but never send under that lock."""
        valid = isinstance(side, dict)
        side = side if valid else {}
        text = side.get("text") if isinstance(side.get("text"), str) else ""
        candidates = side.get("images")
        candidates = candidates if isinstance(candidates, list) else []
        cap = _integer(self.config.get("max_output_images"), 1, 1, 4)
        paths = [] if paths is None else paths
        for candidate in candidates:
            if len(paths) >= cap:
                break
            try:
                path = await self._materialize_output(candidate)
            except Exception:  # Signed URLs/model identities must not be logged.
                continue
            paths.append(path)
            self._battle_pending_outputs.add(path)
        return {"paths": paths, "text": text.strip()[:4000],
                "error": bool(side.get("error")) or not valid,
                "had_images": bool(candidates)}

    def _battle_prefix(self, event) -> str:
        getter = getattr(self, "_battle_command_prefix", None)
        return getter(event) if callable(getter) else "/"

    def _battle_comparison_result(self, event, code: str, sides) -> tuple[object, bool]:
        """One ordered message: header, A images, B images, compact commands.

        A single receipt covers every component. Eligibility is persisted only
        after the combined send succeeds (or its one framework yield resumes).
        """
        result = event.plain_result("Battle\n")
        valid = []
        for label, side in zip(("A", "B"), sides):
            result.chain.append(Plain(f"\n{label}\n"))
            for path in side["paths"]:
                result.chain.append(Image.fromFileSystem(str(path)))
            if side["text"]:
                result.chain.append(Plain(side["text"] + "\n"))
            presented = bool(side["paths"] or side["text"]) and not (
                side["had_images"] and not side["paths"]
            )
            ok = presented and not side["error"]
            valid.append(ok)
            if not ok:
                reason = "上游返回错误" if side["error"] else "结果无法完整呈现"
                result.chain.append(Plain(f"{label} 路失败：{reason}\n"))
        eligible = all(valid)
        if eligible:
            footer = (
                "\n引用本条回复 A / B 投票；回复“不投票”随机投票并揭晓。"
                if self.config.get("battle_allow_reveal_without_vote", False)
                else "\n引用本条回复 A / B 投票并揭晓。"
            )
        else:
            footer = "\n本场禁止投票，请重新发起。"
        result.chain.append(Plain(footer))
        return result, eligible

    async def battle(self, event: AstrMessageEvent, prompt: GreedyStr = GreedyStr):
        """Generate anonymous lanes, release queue lock, then confirm delivery."""
        if self._battle_disabled():
            yield event.plain_result("对战功能已关闭。")
            return
        try:
            if self._battle_load_error:
                raise ValueError(self._battle_load_error)
            chat, owner = self._battle_identity(event)
            text = _argument(prompt)
            # Text commands can be acknowledged before reference images are
            # downloaded. Image-only commands still need an input check first.
            images = await self._collect_input_images(event) if not text else None
        except Exception as exc:
            yield event.plain_result(f"开始对战失败：{_display_error(exc)}")
            return
        if not text and not images:
            yield event.plain_result("用法：battle 画面描述（可附图或引用图片）")
            return
        waiting = self._active_generations
        cap = _integer(self.config.get("max_queue_depth"), 5, 1, 50)
        if waiting >= cap:
            yield event.plain_result(f"排队已满（{waiting}/{cap}），请稍后再发起对战。")
            return
        self._active_generations += 1
        prepared = []
        reserved = False
        try:
            if waiting:
                yield event.plain_result(f"前面还有 {waiting} 个出图任务，对战已排队。")
            else:
                # Yield before taking the generation lock or making any Arena
                # request. The scheduler sends this receipt before resuming us.
                yield event.plain_result("已收到，正在生成 A/B 对战图片。")
            if images is None:
                try:
                    images = await self._collect_input_images(event)
                except Exception as exc:
                    yield event.plain_result(f"开始对战失败：{_display_error(exc)}")
                    return
            async with self._generation_lock:
                started = time.monotonic()
                mode = self._battle_mode()
                response = await self._battle_client().begin_battle(
                    prompt=text or "根据参考图生成一张更精致的图片", images=images,
                )
                if not isinstance(response, dict):
                    raise TypeError("上游没有返回有效的对战结果")
                evaluation_id = response.get("evaluation_id")
                if not isinstance(evaluation_id, str) or not evaluation_id.strip():
                    raise ValueError("上游没有返回对战标识")
                context = self._battle_copy_context(response.get("context"))
                sides = response.get("sides")
                sides = sides if isinstance(sides, dict) else {}
                self._battle_trim()
                code = self._battle_new_id()
                if len(self._battle_sessions) >= MAX_BATTLE_SESSIONS:
                    available = [key for key in self._battle_sessions if not self._battle_busy.get(key)]
                    if not available:
                        raise ValueError("对战会话已满且正在处理，请稍后再试")
                    oldest = min(available, key=lambda key: self._battle_sessions[key]["timestamp"])
                    del self._battle_sessions[oldest]
                # Reserve this session BEFORE releasing the generation lock. The
                # busy count keeps another generation from evicting its delivery.
                self._battle_sessions[code] = {
                    "context": context, "evaluation_id": evaluation_id,
                    "owner": owner, "chat": chat, "timestamp": time.time(),
                    "vote": None, "names": None, "transport_mode": mode,
                    "random_choice": "", "platform_message_id": "",
                }
                self._battle_busy[code] = self._battle_busy.get(code, 0) + 1
                reserved = True
                self._battle_save()
                for key in ("a", "b"):
                    paths = []
                    prepared.append({"paths": paths})
                    prepared[-1] = await self._battle_prepare_side(sides.get(key), paths=paths)
                self._last_generation_seconds = time.monotonic() - started
            # _battle_guard serializes vote/reveal until all yielded results have
            # been consumed. Closing/cancelling at any yield leaves vote=None.
            async with self._battle_guard(code):
                result, eligible = self._battle_comparison_result(event, code, prepared)
                deliver = getattr(self, "_deliver_battle_message", None)
                try:
                    outcome = await deliver(event, result) if callable(deliver) else ""
                except Exception:
                    outcome = object()  # Uncertain send: never split or resend.
                if isinstance(outcome, str) and outcome == "":
                    yield result
                elif isinstance(outcome, BattleMessageReceipt):
                    native_id = message_id(outcome.message_id)
                    if not native_id:
                        raise ValueError("发送成功但平台消息标识无效，未开放投票")
                    self._battle_sessions[code]["platform_message_id"] = native_id
                elif outcome is not None:
                    uncertain = bool(getattr(outcome, "uncertain", True))
                    reason = (
                        "发送回执不确定，图片可能已送达，不再重发"
                        if uncertain else "平台明确拒收图片"
                    )
                    yield event.plain_result(f"Battle · {code}\n{reason}；本场禁止投票。")
                    return
                self._battle_sessions[code]["vote"] = "" if eligible else None
                try:
                    self._battle_save()
                except BaseException:
                    # A confirmed send must not become votable only in memory
                    # when binding/eligibility persistence fails. Do not resend.
                    self._battle_sessions[code]["vote"] = None
                    self._battle_sessions[code]["platform_message_id"] = ""
                    raise
        except Exception as exc:
            yield event.plain_result(f"对战失败：{_display_error(exc)}")
        finally:
            if reserved and self._battle_busy.get(code):
                self._battle_busy[code] -= 1
                if not self._battle_busy[code]:
                    del self._battle_busy[code]
                self._battle_trim()
            for side in prepared:
                self._battle_pending_outputs.difference_update(side["paths"])
            self._prune_outputs()
            self._active_generations = max(0, self._active_generations - 1)


    async def _battle_vote_locked(self, client, entry: dict, winner: str) -> None:
        result = await client.vote_battle(
            self._battle_copy_context(entry["context"]), winner=winner
        )
        # Preserve duck-typed clients' historical None-success contract, while
        # accepting protocol success/reveal_pending even without model names.
        if result is False or (
            isinstance(result, dict)
            and (result.get("error") or result.get("success") is False)
        ):
            raise ValueError("上游未确认投票成功")
        entry["vote"] = winner
        # Never undo upstream confirmation on IO/name/readback failure. The
        # next reveal retries this save before reading names, without a vote.
        self._battle_save()
        if isinstance(result, dict) and all(result.get(side) for side in ("a", "b")):
            entry["names"] = self._battle_validate_names(result)
            self._battle_save()

    async def _battle_reveal_locked(self, client, entry: dict) -> dict[str, str]:
        if entry["names"] is None:
            # A previous confirmed vote may still need persistence after an IO
            # failure. Do not read/validate names until that mark is on disk.
            self._battle_save()
            names = await client.reveal_battle(
                self._battle_copy_context(entry["context"])
            )
            entry["names"] = self._battle_validate_names(names)
            # Keep names in memory on an IO failure: revealing must never make
            # this session votable again. Retry persistence even for cached names.
        self._battle_save()
        return entry["names"]

    @staticmethod
    def _battle_names_message(code: str, names: dict, random_choice: str = "") -> str:
        source = f"已随机投票 {random_choice.upper()}。\n" if random_choice else ""
        return source + f"Battle 已揭晓\nA：{names['a']}\nB：{names['b']}"

    async def battle_vote(self, event: AstrMessageEvent, text: GreedyStr = GreedyStr):
        if self._battle_disabled():
            yield event.plain_result("对战功能已关闭。")
            return
        prefix = self._battle_prefix(event)
        args = _argument(text).split()
        if len(args) not in (1, 2) or args[0].casefold() not in {"a", "b"}:
            yield event.plain_result(f"用法：{prefix}battle投票 A/B [编号]")
            return
        winner = args[0].casefold()
        requested = args[1] if len(args) == 2 else ""
        attempted = False
        entry = None
        try:
            requested = self._battle_quoted_request(event, requested)
            code, _ = self._battle_lookup(event, requested)
            async with self._battle_guard(code):
                _, entry = self._battle_lookup(event, code)
                if entry["names"] is not None:
                    raise ValueError("这场对战已揭晓，为保留盲测结果，不能再投票")
                if entry["vote"] in {"a", "b"}:
                    raise ValueError(
                        f"这场对战已经投票，请勿重复投票；可用 {prefix}battle揭晓 查看名称"
                    )
                if entry["random_choice"]:
                    raise ValueError(
                        "这场对战的随机投票尚未确认，不能改为手动 A/B；"
                        f"请继续 {prefix}battle不投票 {code}，重试将保留原选择"
                    )
                if entry["vote"] is None:
                    raise ValueError("这场对战有单侧失败或结果未完整呈现，禁止投票")
                client = self._battle_client()
                attempted = True
                await self._battle_vote_locked(client, entry, winner)
                if entry["transport_mode"] != self._battle_mode():
                    raise ValueError("传输通道已切换，请切回原通道后揭晓")
                names = await self._battle_reveal_locked(client, entry)
            yield event.plain_result(
                f"已投票 {winner.upper()}。\n" + self._battle_names_message(code, names)
            )
        except Exception as exc:
            prefix = (
                f"投票已成功，但揭晓或保存失败；不要重复投票，可重试 {prefix}battle揭晓："
                if attempted and entry["vote"] == winner
                else "投票失败："
            )
            yield event.plain_result(prefix + _display_error(exc))

    async def battle_reveal(self, event: AstrMessageEvent, text: GreedyStr = GreedyStr):
        if self._battle_disabled():
            yield event.plain_result("对战功能已关闭。")
            return
        prefix = self._battle_prefix(event)
        args = _argument(text).split()
        if len(args) > 1:
            yield event.plain_result(f"用法：{prefix}battle揭晓 [编号] 或 {prefix}battle不投票 [编号]")
            return
        entry = None
        try:
            requested = self._battle_quoted_request(event, args[0] if args else "")
            code, _ = self._battle_lookup(event, requested)
            async with self._battle_guard(code):
                _, entry = self._battle_lookup(event, code)
                if entry["names"] is None and entry["vote"] not in {"a", "b"}:
                    if entry["vote"] is None:
                        raise ValueError(
                            "这场对战有单侧失败或结果未完整呈现，禁止投票，"
                            "也不能随机投票揭晓，请重新发起对战"
                        )
                    if not self.config.get("battle_allow_reveal_without_vote", False):
                        raise ValueError(
                            f"请先投票再揭晓；管理员可用 {prefix}battle揭晓开关 开 "
                            "启用随机投票揭晓（会提交 A/B 偏好票）"
                        )
                    if not entry["random_choice"]:
                        entry["random_choice"] = secrets.choice(("a", "b"))
                    # Persist the sticky choice BEFORE network. Failure/cancel
                    # never clears it; another attempt must save/reuse this side.
                    self._battle_save()
                    client = self._battle_client()
                    await self._battle_vote_locked(client, entry, entry["random_choice"])
                    if entry["transport_mode"] != self._battle_mode():
                        raise ValueError("传输通道已切换，请切回原通道后揭晓")
                else:
                    client = self._battle_client() if entry["names"] is None else None
                names = await self._battle_reveal_locked(client, entry)
            yield event.plain_result(
                self._battle_names_message(code, names, entry["random_choice"])
            )
        except Exception as exc:
            error_prefix = "揭晓失败："
            if entry is not None and entry["random_choice"]:
                side = entry["random_choice"].upper()
                if entry["vote"] == entry["random_choice"]:
                    error_prefix = (
                        f"已随机投票 {side}，投票已成功，但揭晓或保存失败；"
                        f"不要重复投票，可重试 {prefix}battle不投票 {code}："
                    )
                else:
                    error_prefix = (
                        f"揭晓失败：随机选择 {side} 保持不变，投票或保存尚未确认成功；"
                        f"请继续 {prefix}battle不投票 {code}："
                    )
            yield event.plain_result(error_prefix + _display_error(exc))

    async def battle_reveal_switch(
        self, event: AstrMessageEvent, text: GreedyStr = GreedyStr
    ):
        prefix = self._battle_prefix(event)
        value = _argument(text)
        if value not in {"", "查询", "开", "关"}:
            yield event.plain_result(f"用法：{prefix}battle揭晓开关 开/关/查询（管理员）")
            return
        try:
            async with self._battle_config_lock:
                if value in {"开", "关"}:
                    key = "battle_allow_reveal_without_vote"
                    existed = key in self.config
                    previous = self.config.get(key)
                    self.config[key] = value == "开"
                    try:
                        saver = getattr(self.config, "save_config", None)
                        if not callable(saver):
                            saver = getattr(self, "save_config", None)
                        if callable(saver):
                            result = saver()
                            if inspect.isawaitable(result):
                                await result
                    except BaseException:
                        if existed:
                            self.config[key] = previous
                        else:
                            self.config.pop(key, None)
                        raise
                enabled = bool(
                    self.config.get("battle_allow_reveal_without_vote", False)
                )
            yield event.plain_result(
                f"随机投票揭晓开关：{'开' if enabled else '关'}。"
                + (
                    f"{prefix}battle不投票 或 {prefix}battle揭晓 会提交随机 A/B 偏好票；"
                    "揭晓后禁止投票。"
                    if enabled else "必须先投票成功再揭晓，不会提交随机票。"
                )
            )
        except Exception as exc:
            yield event.plain_result(f"修改揭晓开关失败：{_display_error(exc)}")
