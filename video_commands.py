"""Blind video Battle controller; no command registration or transport fallback.

Host hooks are supplied by ArenaImagePlugin's mvbattle integration:
* _transport_mode() -> str: bridge/direct use the original server browser;
  userscript is unsupported. _video_battle_client() returns a server-direct
  ArenaVideoClient with video-specific deadlines.
  open_page() serializes complete page contexts by configured CDP endpoint.
  This prevents another image operation from reloading a tab during a video
  create, but also queues read-only browser work until that context is released.
  The video command itself uses its own queue, not the image generation counter.
  read_video_battle(context) reads the exact saved /c/session without a new POST.
  Client sides require finished=True and error="" (or False); nonempty/malformed
  errors never qualify. Prepared side flags are strict bools.
* async _materialize_video_output(candidate, *, output_dir) -> Path: authorize
  the URL, bound download bytes/time, validate a playable MP4 (not just MIME or
  an HTTP 200), and atomically save it under output_dir. Reject on any failure;
  clean partial files on cancellation. This controller also checks scope/ftyp.
* async _deliver_video_battle_message(event, result, receipt_callback=None): send
  ONE native message containing the complete ordered A/B chain, without loss.
  If supported, await receipt_callback(native_id) immediately after a valid
  numeric outer receipt, BEFORE get_msg/get_forward verification. Callback
  failure/cancellation must abort verification, never retry or resend. Return
  BattleMessageReceipt with that SAME outer ID ONLY after full verification;
  failure/unknown outcomes retain the saved binding but never enable voting.
  Legacy hosts without the keyword are called once without a callback; their
  binding can only be saved after the final receipt. No transport fallback.
* _prune_video_outputs(*, output_dir, pending): synchronous video-only pruning,
  preserving pending paths. Never call image _prune_outputs or delete image data.

All session state/locks live here, with metadata in host.data_dir/video_battles.
Only allowlisted video session context is retained; prompts, outputs, URLs and
files are not persisted. Shared Battle config (including disclosed random A/B
reveal) remains unchanged. video_queue_depth defaults to 1; generation plus
materialization uses video_request_timeout (900s default), native sending uses
video_send_timeout (180s default). Queue wait is outside these deadlines.

The client must await its optional _video_context_callback(context) BEFORE the
one-shot create POST. This durably reserves a non-votable session even if create
or subsequent readback times out, fails or is cancelled. Fake/legacy clients may
instead reserve from the returned response (that fallback cannot protect a real
pre-response create). video_resume requires an explicit code or native quote;
it reads exactly that saved context once and never calls begin_battle. A durable
video-only delivery_attempted flag blocks resume after ANY native send attempt,
including unknown receipt/cancellation; there is no automatic readback or resend.

Parent integration must route quotes by unique (chat, native message_id), never
by latest/code/text or by falling back to image handlers after a video error.
New codes exclude image codes; legacy cross-store code/receipt collisions fail
closed here. This controller cannot prevent a separately invoked image handler
from processing a collision: routing arbitration belongs to the parent.
"""

# Transport hooks are duck-typed; command-boundary errors must remain anonymous.
# ruff: noqa: BLE001
from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, TypedDict

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain, Video
from astrbot.core.star.filter.command import GreedyStr

from .arena_video import video_context
from .battle_commands import (
    _SESSION_FIELDS,
    MAX_BATTLE_SESSIONS,
    MAX_SESSION_FILE_BYTES,
    BattleCommandsMixin,
    _argument,
    _display_error,
    _integer,
)
from .battle_reply import BattleMessageReceipt, message_id


class VideoCandidate(TypedDict):
    url: str
    contentType: str


class VideoBattleClient(Protocol):
    async def begin_battle(self, *, prompt: str, images: list) -> dict: ...
    async def read_video_battle(self, context: dict) -> dict: ...
    async def vote_battle(self, context: dict, *, winner: str) -> Any: ...
    async def reveal_battle(self, context: dict) -> dict[str, str]: ...


class VideoBattleHost(Protocol):
    data_dir: Path
    config: Mapping[str, Any]

    def _transport_mode(self) -> str: ...
    def _video_battle_client(self) -> VideoBattleClient: ...
    async def _collect_input_images(self, event: AstrMessageEvent) -> list: ...
    async def _materialize_video_output(
        self, candidate: VideoCandidate, *, output_dir: Path
    ) -> Path: ...
    async def _deliver_video_battle_message(
        self,
        event: AstrMessageEvent,
        result: Any,
        receipt_callback: Callable[[Any], Awaitable[None]] | None = None,
    ) -> BattleMessageReceipt | Any: ...
    def _prune_video_outputs(self, *, output_dir: Path, pending: set[Path]) -> None: ...


def _seconds(value: Any, default: float, maximum: float) -> float:
    try:
        number = float(value)
        return max(1.0, min(maximum, number)) if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


class VideoBattleController(BattleCommandsMixin):
    def __init__(self, host: VideoBattleHost):
        self.host = host
        self.data_dir = Path(host.data_dir) / "video_battles"
        self.output_dir = self.data_dir / "outputs"
        self.config = host.config
        self._generation_lock = asyncio.Lock()
        self._active_generations = 0
        self._last_generation_seconds = 0.0
        self._init_battles()

    def _init_battles(self) -> None:
        super()._init_battles()
        # The inherited loader deliberately keeps only the image envelope. Read
        # our one additional flag without relaxing its validation/size limits.
        # Legacy or damaged flags are uncertain: never resume into another send.
        for entry in self._battle_sessions.values():
            entry["delivery_attempted"] = True
        try:
            if self._battle_sessions_file.stat().st_size > MAX_SESSION_FILE_BYTES:
                return
            raw = json.loads(self._battle_sessions_file.read_text(encoding="utf-8"))
            for code, entry in self._battle_sessions.items():
                flag = raw.get(code, {}).get("delivery_attempted", True)
                entry["delivery_attempted"] = flag if type(flag) is bool else True
                if entry["platform_message_id"] or entry["vote"] is not None:
                    entry["delivery_attempted"] = True
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            pass

    def _battle_trim(self) -> bool:
        removed = super()._battle_trim()
        # Video commands require an explicit code or exact native quote, including
        # known-but-unverified forward cards. Never publish a latest fallback.
        self._battle_latest.clear()
        return removed

    def _battle_save(self) -> None:
        # Same bounded, atomic snapshot as the mixin, plus a video-only durable
        # send-attempt bit. It must be on disk BEFORE native delivery can start.
        self._battle_trim()
        payload = {
            code: {
                **{key: entry[key] for key in _SESSION_FIELDS},
                "delivery_attempted": entry.get("delivery_attempted", True),
            }
            for code, entry in self._battle_sessions.items()
        }
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
        if len(encoded.encode("utf-8")) > MAX_SESSION_FILE_BYTES:
            raise ValueError("视频对战会话文件超过容量上限")
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

    def __getattr__(self, name: str) -> Any:
        return getattr(self.host, name)

    def _battle_mode(self) -> str:
        mode = str(self.host._transport_mode()).strip().casefold()
        if mode not in {"bridge", "direct"}:
            raise ValueError("视频对战仅支持服务器直连，不支持 userscript 通道")
        return mode

    def _battle_client(self) -> VideoBattleClient:
        self._battle_mode()
        return self.host._video_battle_client()

    @staticmethod
    def _battle_copy_context(context: Any) -> dict:
        # Keep the existing bounded deep-copy guard, then the protocol's ID-only
        # allowlist. Do not persist arbitrary upstream prompt/attachment fields.
        try:
            return video_context(BattleCommandsMixin._battle_copy_context(context))
        except Exception:
            # The inherited loader catches ValueError, not protocol exceptions.
            raise ValueError("无效视频会话上下文") from None

    def _battle_validate_session(self, entry: dict) -> None:
        super()._battle_validate_session(entry)
        entry["context"] = self._battle_copy_context(entry["context"])
        if entry["evaluation_id"] != entry["context"]["evaluationSessionId"]:
            raise ValueError("视频会话标识不一致")
        if entry["transport_mode"] not in {"bridge", "direct"}:
            raise ValueError("视频会话通道无效")
        if entry["vote"] is not None and not entry["platform_message_id"]:
            raise ValueError("视频会话缺少原生回执")
        if entry["names"] is not None and entry["vote"] not in {"a", "b"}:
            raise ValueError("视频揭晓缺少已确认投票")

    def _image_codes(self) -> set[str]:
        return set(getattr(self.host, "_battle_sessions", {})) | set(
            getattr(self.host, "_battle_locks", {})
        )

    def _battle_new_id(self) -> str:
        for _ in range(100):
            code = super()._battle_new_id()
            if code not in self._image_codes():
                return code
        raise ValueError("暂时无法分配独立的视频对战编号，请稍后重试")

    def _battle_lookup(self, event, requested: str) -> tuple[str, dict]:
        code, entry = super()._battle_lookup(event, requested)
        if code in self._image_codes():
            raise ValueError("视频与图片对战编号冲突，未提交投票，请重新发起")
        native_id = entry.get("platform_message_id", "")
        if native_id and not self._receipt_is_unique(code, entry["chat"], native_id):
            raise ValueError("视频对战回执冲突，未提交投票，请重新发起")
        return code, entry

    def _battle_reply_code(self, event, native_id: str) -> str:
        chat, _ = self._battle_identity(event)
        if any(
            entry.get("chat") == chat and entry.get("platform_message_id") == native_id
            for entry in getattr(self.host, "_battle_sessions", {}).values()
        ):
            raise ValueError("引用消息属于图片对战或回执冲突，未提交视频投票")
        return super()._battle_reply_code(event, native_id)

    def _receipt_is_unique(self, code: str, chat: str, native_id: str) -> bool:
        stores = (self._battle_sessions, getattr(self.host, "_battle_sessions", {}))
        return not any(
            entry.get("chat") == chat and entry.get("platform_message_id") == native_id
            for store in stores
            for key, entry in store.items()
            if store is not self._battle_sessions or key != code
        )

    def _checked_video_path(self, value: Any) -> Path:
        path = Path(value).resolve(strict=True)
        path.relative_to(self.output_dir.resolve())
        if path.suffix.casefold() != ".mp4" or not path.is_file():
            raise ValueError("下载结果不是本场 MP4 文件")
        with path.open("rb") as handle:
            header = handle.read(12)
        if len(header) != 12 or header[4:8] != b"ftyp":
            raise ValueError("下载结果没有 MP4 文件头")
        # A header alone is not proof of playability; the materializer contract
        # additionally requires a complete playable video before returning.
        return path

    async def _battle_prepare_side(self, side, *, paths=None) -> dict:
        paths = [] if paths is None else paths
        valid = (
            isinstance(side, dict)
            and side.get("finished") is True
            and (
                side.get("error") is False
                or (isinstance(side.get("error"), str) and side["error"] == "")
            )
            and isinstance(side.get("videos"), list)
        )
        if valid:
            # Exactly one playable video per lane. Bound malformed upstream lists
            # and try alternatives only before any send has been attempted.
            for candidate in side["videos"][:16]:
                if not isinstance(candidate, dict):
                    continue
                url, mime = candidate.get("url"), candidate.get("contentType")
                if not isinstance(url, str) or not url.strip() or mime != "video/mp4":
                    continue
                try:
                    path = self._checked_video_path(
                        await self.host._materialize_video_output(
                            {"url": url, "contentType": mime},
                            output_dir=self.output_dir,
                        )
                    )
                except Exception:  # noqa: S112 - No URL/provider/file diagnostics.
                    continue
                paths.append(path)
                self._battle_pending_outputs.add(path)
                break
        return {"paths": paths, "error": not valid, "finished": bool(valid)}

    def _battle_comparison_result(self, event, code: str, sides) -> tuple[Any, bool]:
        result = event.plain_result("视频 Battle\n")
        valid = []
        for label, side in zip(("A", "B"), sides):
            result.chain.append(Plain(f"\n{label}\n"))
            ok = (
                side.get("finished") is True
                and side.get("error") is False
                and len(side["paths"]) == 1
            )
            if ok:
                result.chain.append(Video.fromFileSystem(str(side["paths"][0])))
            else:
                result.chain.append(
                    Plain(f"{label} 路失败：未确认完整可播放 MP4 视频\n")
                )
            valid.append(ok)
        eligible = len(valid) == 2 and all(valid)
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

    def _video_reserve(self, context: dict, *, chat: str, owner: str, mode: str) -> str:
        """Allocate/pin only; caller saves after recording ownership for cleanup."""
        self._battle_trim()
        code = self._battle_new_id()
        if len(self._battle_sessions) >= MAX_BATTLE_SESSIONS:
            available = [
                key for key in self._battle_sessions if not self._battle_busy.get(key)
            ]
            if not available:
                raise ValueError("视频对战会话已满")
            oldest = min(
                available, key=lambda key: self._battle_sessions[key]["timestamp"]
            )
            del self._battle_sessions[oldest]
        self._battle_sessions[code] = {
            "context": context,
            "evaluation_id": context["evaluationSessionId"],
            "owner": owner,
            "chat": chat,
            "timestamp": time.time(),
            "vote": None,
            "names": None,
            "transport_mode": mode,
            "random_choice": "",
            "platform_message_id": "",
            "delivery_attempted": False,
        }
        self._battle_busy[code] = self._battle_busy.get(code, 0) + 1
        return code

    def _video_response(
        self, response: Any, context: dict | None = None
    ) -> tuple[dict, dict]:
        if not isinstance(response, dict):
            raise TypeError("无效视频结果")
        checked = self._battle_copy_context(response.get("context"))
        if response.get("evaluation_id") != checked["evaluationSessionId"] or (
            context is not None and checked != context
        ):
            raise ValueError("视频结果与已保存的原会话不一致")
        sides = response.get("sides")
        return checked, sides if isinstance(sides, dict) else {}

    async def _video_prepare(self, sides: dict, prepared: list) -> None:
        for key in ("a", "b"):
            paths = []
            prepared.append({"paths": paths})
            prepared[-1] = await self._battle_prepare_side(sides.get(key), paths=paths)

    async def _video_deliver(
        self, event, code: str, prepared: list, *, resume=False
    ) -> str:
        """Caller holds the session guard, never the generation lock."""
        entry = self._battle_sessions[code]
        chat, owner = self._battle_identity(event)
        if (entry["chat"], entry["owner"]) != (chat, owner):
            raise ValueError("视频发送与会话聊天或发起人不一致")
        result, eligible = self._battle_comparison_result(event, code, prepared)
        if resume and not eligible:
            return f"视频对战 · {code} 尚未确认两路完整可播放 MP4；本场未开放投票，未发送视频。"
        if entry["transport_mode"] != self._battle_mode():
            raise ValueError("发送前通道已切换")
        if entry.get("delivery_attempted", True):
            raise ValueError("本场已尝试原生发送，不会重复发送视频")
        # Persist before crossing the uncertain send boundary. Crash/cancel or an
        # absent receipt must not enable another manual send after restart.
        entry["delivery_attempted"] = True
        self._battle_save()
        deliver = self.host._deliver_video_battle_message
        # Decide BEFORE sending. A TypeError may happen after a partial native
        # send; it must never trigger a retry using the legacy signature.
        try:
            parameters = inspect.signature(deliver).parameters
            parameter = parameters.get("receipt_callback")
            supports_callback = (
                parameter is not None
                and parameter.kind in {
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                }
            ) or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        except (TypeError, ValueError):
            supports_callback = False
        saved_id = ""
        callback_failed = False
        accepting_receipt = True

        def checked_receipt(value):
            native_id = message_id(value)
            if (
                not native_id
                or self._battle_sessions.get(code) is not entry
                or (entry["chat"], entry["owner"]) != (chat, owner)
                or self._battle_identity(event) != (chat, owner)
                or entry["vote"] is not None
                or entry["names"] is not None
                or code in self._image_codes()
                or not self._receipt_is_unique(code, chat, native_id)
            ):
                raise ValueError("视频回执无效、不唯一或与原会话不一致")
            return native_id

        async def capture_receipt(value):
            nonlocal saved_id, callback_failed
            # A retained callback must not mutate a completed/failed delivery,
            # especially after final eligibility has already been persisted.
            if not accepting_receipt:
                raise ValueError("视频发送已结束，不能保存新的原生回执")
            previous_id = entry["platform_message_id"]
            try:
                if callback_failed:
                    raise ValueError("视频回执保存失败")
                native_id = checked_receipt(value)
                if previous_id and previous_id != native_id:
                    raise ValueError("不能替换已保存的视频原生回执")
                entry["platform_message_id"] = native_id
                entry["vote"] = None
                self._battle_save()  # Host must await this BEFORE verification.
                saved_id = native_id
            except BaseException:
                callback_failed = True
                entry["platform_message_id"] = previous_id
                entry["vote"] = None
                raise

        try:
            timeout = _seconds(self.config.get("video_send_timeout"), 180.0, 600.0)
            async with asyncio.timeout(timeout):
                if supports_callback:
                    outcome = await deliver(event, result, receipt_callback=capture_receipt)
                else:
                    outcome = await deliver(event, result)
        except Exception:
            outcome = None
        finally:
            accepting_receipt = False
        try:
            native_id = (
                checked_receipt(outcome.message_id)
                if isinstance(outcome, BattleMessageReceipt)
                else ""
            )
        except Exception:
            native_id = ""
        if (
            not native_id
            or callback_failed
            or (supports_callback and not saved_id)
            or (saved_id and native_id != saved_id)
        ):
            return (
                f"视频对战 · {code} 未获唯一有效且一致的原生回执，视频可能已送达，"
                "不再重发；本场禁止投票。"
            )
        if entry["transport_mode"] != self._battle_mode():
            raise ValueError("发送期间通道已切换")
        entry["platform_message_id"] = native_id
        entry["vote"] = "" if eligible else None
        try:
            self._battle_save()
        except BaseException:
            entry["vote"] = None
            # Keep the pre-verification durable binding. Legacy hosts have none.
            entry["platform_message_id"] = saved_id
            raise
        return ""

    def _video_release(self, code: str, reserved: bool, prepared: list) -> None:
        if reserved and self._battle_busy.get(code):
            self._battle_busy[code] -= 1
            if not self._battle_busy[code]:
                del self._battle_busy[code]
            self._battle_trim()
        for side in prepared:
            self._battle_pending_outputs.difference_update(side["paths"])
        self._active_generations = max(0, self._active_generations - 1)
        try:
            self.host._prune_video_outputs(
                output_dir=self.output_dir, pending=set(self._battle_pending_outputs)
            )
        except Exception:  # noqa: S110 - Cleanup cannot mask cancellation.
            pass

    def _video_interrupted(self, code: str) -> str:
        prefix = f"视频对战 · {code} " if code else "视频对战"
        suffix = "不会自动重发视频。"
        entry = self._battle_sessions.get(code)
        if entry is not None and not entry.get("delivery_attempted", True):
            suffix += f"可用 mvbattle读回 {code}，只读取原会话，不会重新生成。"
        return prefix + "失败或超时；本场未开放投票，" + suffix

    async def video_battle(
        self, event: AstrMessageEvent, prompt: GreedyStr = GreedyStr
    ) -> AsyncIterator[Any]:
        if self._battle_disabled():
            yield event.plain_result("对战功能已关闭。")
            return
        try:
            if self._battle_load_error:
                raise ValueError("读取视频对战会话失败")
            mode = self._battle_mode()
            chat, owner = self._battle_identity(event)
            text = _argument(prompt)
            images = await self._collect_input_images(event) if not text else None
        except Exception:
            yield event.plain_result("开始视频对战失败：请检查通道、会话及聊天身份。")
            return
        if not text and not images:
            yield event.plain_result("用法：mvbattle 画面描述（可附图或引用图片）")
            return
        waiting = self._active_generations
        cap = _integer(self.config.get("video_queue_depth"), 1, 1, 50)
        if waiting >= cap:
            yield event.plain_result(f"视频排队已满（{waiting}/{cap}），请稍后再发起。")
            return
        self._active_generations += 1
        prepared = []
        reserved = False
        code = ""
        accepting_context = True

        async def capture_context(context):
            nonlocal code, reserved
            if not accepting_context:
                raise ValueError("视频请求已结束，不能提交新的生成")
            checked = self._battle_copy_context(context)
            if mode != self._battle_mode():
                raise ValueError("提交前通道已切换")
            if reserved:
                if self._battle_sessions[code]["context"] != checked:
                    raise ValueError("不能替换已保存的视频会话")
            else:
                code = self._video_reserve(checked, chat=chat, owner=owner, mode=mode)
                reserved = True
            self._battle_save()  # Awaited callback must succeed BEFORE POST.

        try:
            # Count the reservation before ACK; close/cancel at this yield must
            # release it without beginning any network request/session.
            yield event.plain_result("已收到，正在生成 A/B 对战视频。")
            async with self._generation_lock:
                started = time.monotonic()
                timeout = _seconds(
                    self.config.get("video_request_timeout"), 900.0, 3600.0
                )
                async with asyncio.timeout(timeout):
                    if mode != self._battle_mode():
                        raise ValueError("排队期间通道已切换")
                    if images is None:
                        images = await self._collect_input_images(event)
                    client = self._battle_client()
                    previous = getattr(client, "_video_context_callback", None)
                    client._video_context_callback = capture_context
                    try:
                        response = await client.begin_battle(
                            prompt=text or "根据参考图生成视频",
                            images=images,
                        )
                        saved = (
                            self._battle_sessions[code]["context"] if reserved else None
                        )
                        context, sides = self._video_response(response, saved)
                        if not reserved:
                            # Offline clients can reserve from their returned result.
                            # Never issue another begin/POST to recover context.
                            await capture_context(context)
                    finally:
                        accepting_context = False
                        if (
                            getattr(client, "_video_context_callback", None)
                            is capture_context
                        ):
                            client._video_context_callback = previous
                    await self._video_prepare(sides, prepared)
                    self._last_generation_seconds = time.monotonic() - started
            async with self._battle_guard(code):
                failure = await self._video_deliver(event, code, prepared)
                if failure:
                    yield event.plain_result(failure)
        except Exception as exc:
            prefix = "官网限制了本次视频请求（HTTP 429），未自动重发。\n" if getattr(exc, "status_code", None) == 429 else ""
            yield event.plain_result(prefix + self._video_interrupted(code))
        finally:
            accepting_context = False
            self._video_release(code, reserved, prepared)

    async def video_resume(
        self, event: AstrMessageEvent, code: GreedyStr = GreedyStr
    ) -> AsyncIterator[Any]:
        """One explicit exact-session read, no begin, automatic polling or vote."""
        if self._battle_disabled():
            yield event.plain_result("对战功能已关闭。")
            return
        try:
            if self._battle_load_error:
                raise ValueError("读取视频对战会话失败")
            args = _argument(code).split()
            if len(args) > 1:
                raise ValueError("恢复需要单个视频对战编号或原生引用")
            requested = self._battle_quoted_request(event, args[0] if args else "")
            if not requested:
                raise ValueError("恢复需要明确编号或原生引用，不能恢复最近一场")
            code, entry = self._battle_lookup(event, requested)
            self._video_require_pending(entry)
            if self._battle_busy.get(code):
                raise ValueError("本场仍在处理，不能同时恢复")
        except Exception as exc:
            yield event.plain_result("恢复视频对战失败：" + _display_error(exc))
            return
        cap = _integer(self.config.get("video_queue_depth"), 1, 1, 50)
        if self._active_generations >= cap:
            yield event.plain_result("视频排队已满，请稍后再恢复。")
            return
        self._active_generations += 1
        self._battle_busy[code] = self._battle_busy.get(code, 0) + 1
        prepared = []
        try:
            yield event.plain_result(
                f"已收到，正在读取视频对战 · {code}，不会重新生成。"
            )
            async with self._battle_guard(code):
                async with self._generation_lock:
                    _, entry = self._battle_lookup(event, code)
                    self._video_require_pending(entry)
                    timeout = _seconds(
                        self.config.get("video_request_timeout"), 900.0, 3600.0
                    )
                    async with asyncio.timeout(timeout):
                        saved = self._battle_copy_context(entry["context"])
                        response = await self._battle_client().read_video_battle(
                            self._battle_copy_context(saved)
                        )
                        _, sides = self._video_response(response, saved)
                        await self._video_prepare(sides, prepared)
                failure = await self._video_deliver(event, code, prepared, resume=True)
                if failure:
                    yield event.plain_result(failure)
        except Exception:
            yield event.plain_result(self._video_interrupted(code))
        finally:
            self._video_release(code, True, prepared)

    @staticmethod
    def _video_require_pending(entry: dict) -> None:
        if (
            entry["vote"] is not None
            or entry["names"] is not None
            or entry.get("platform_message_id")
            or entry.get("delivery_attempted", True)
        ):
            raise ValueError("本场已尝试发送或已完成，不能恢复或重复发送视频")

    @staticmethod
    def _video_recovery_result(result):
        # Shared vote state machine, video-specific recovery commands. Keep the
        # shared administrator reveal switch name unchanged.
        replacements = (("battle揭晓开关", "__SHARED_REVEAL_SWITCH__"),
                        ("battle揭晓", "mvbattle揭晓"),
                        ("battle不投票", "mvbattle揭晓"),
                        ("__SHARED_REVEAL_SWITCH__", "battle揭晓开关"))
        # Real native results keep Plain components; offline/framework adapters
        # may store plain_result text separately. Never rewrite model names or
        # opaque fields, only these fixed recovery command labels.
        if isinstance(getattr(result, "text", None), str):
            for old, new in replacements:
                result.text = result.text.replace(old, new)
        for component in getattr(result, "chain", []):
            if isinstance(component, Plain):
                for old, new in replacements:
                    component.text = component.text.replace(old, new)
        return result

    async def battle_vote(self, event, text=GreedyStr):
        async for result in super().battle_vote(event, text):
            yield self._video_recovery_result(result)

    async def battle_reveal(self, event, text=GreedyStr):
        async for result in super().battle_reveal(event, text):
            yield self._video_recovery_result(result)

    # Even an accidental legacy entry-point call must use the video lifecycle,
    # not the inherited image method and its framework-yield delivery fallback.
    battle = video_battle
