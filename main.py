"""AstrBot image commands backed by the local LMArenaBridge service."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import GreedyStr

try:  # Pillow ships with AstrBot, but stay importable without it.
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover - exercised only on minimal installs
    PILImage = None

try:
    from astrbot.core.utils.quoted_message import extract_quoted_message_images
except ImportError:  # AstrBot versions before the shared quoted-message helper.
    extract_quoted_message_images = None

from .bridge_client import (
    ArenaBridgeClient,
    BridgeError,
    data_uri_from_base64,
    decode_image_value,
    image_urls,
    model_created_at,
    model_is_image_capable,
    response_text,
)

PLUGIN_NAME = "astrbot_plugin_arena_image"
GLOBAL_SELECTION_KEY = "__global__"

# Outputs are capped far above the input cap: Arena returns high-resolution
# PNGs that legitimately exceed the 10 MB reference-image budget.
DEFAULT_MAX_OUTPUT_IMAGE_BYTES = 64 * 1024 * 1024
# Messaging platforms reject very large attachments, so anything above this is
# re-encoded before it is sent instead of being dropped.
DEFAULT_SEND_IMAGE_MAX_BYTES = 8 * 1024 * 1024
# Arena's attachment pipeline only accepts still pictures, so GIF uploads are
# always flattened even when they hold a single frame.
ALWAYS_FLATTEN_INPUT_MIMES = frozenset({"image/gif", "image/apng"})


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default


def _display_error(exc: Exception, *, limit: int = 500) -> str:
    """Return a bounded user-facing error without dumping request payloads."""
    text = str(exc).strip() or type(exc).__name__
    return text if len(text) <= limit else f"{text[:limit]}…"


def _human_bytes(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.1f} MB"
    return f"{max(0, value) // 1024} KB"


def _shrink_image_bytes(raw: bytes, mime: str, limit: int) -> tuple[bytes, str]:
    """Re-encode an oversized image so a chat platform still accepts it.

    Transparency is dropped because the fallback container is JPEG; this only
    runs for images that would otherwise be rejected for their size.
    """
    if PILImage is None or limit <= 0 or len(raw) <= limit:
        return raw, mime
    try:
        with PILImage.open(io.BytesIO(raw)) as source:
            source.load()
            image = source.convert("RGB")
    except Exception:  # Unknown/animated formats stay untouched.
        return raw, mime

    resample = getattr(PILImage, "LANCZOS", 1)
    data = raw
    for scale, quality in ((1.0, 88), (1.0, 72), (0.75, 75), (0.5, 75), (0.35, 70)):
        candidate = image
        if scale < 1.0:
            candidate = image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                resample,
            )
        buffer = io.BytesIO()
        candidate.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= limit:
            return data, "image/jpeg"
    return data, "image/jpeg"


def _is_animated_upload(raw: bytes, mime: str) -> bool:
    """Cheap check for reference images Arena cannot take as-is.

    Chunk sniffing keeps the common still-image path free of Pillow work: APNG
    declares ``acTL`` and animated WebP declares ``ANIM``/``ANMF``, both near
    the start of the file.
    """
    clean = str(mime or "").split(";", 1)[0].strip().lower()
    if clean in ALWAYS_FLATTEN_INPUT_MIMES:
        return True
    head = raw[:4096]
    if clean == "image/png":
        return b"acTL" in head
    if clean == "image/webp":
        return b"ANIM" in head or b"ANMF" in head
    return False


def _first_frame_bytes(raw: bytes, mime: str) -> tuple[bytes, str]:
    """Flatten an animated reference image to its first frame as PNG.

    Arena rejects animated attachments, so an untouched GIF fails the whole
    generation.  Bytes Pillow cannot open are returned unchanged so the Bridge
    still receives the original upload.
    """
    if PILImage is None or not raw:
        return raw, mime
    try:
        with PILImage.open(io.BytesIO(raw)) as source:
            source.seek(0)
            transparent = "transparency" in source.info or source.mode in {"RGBA", "LA", "PA"}
            frame = source.convert("RGBA" if transparent else "RGB")
        buffer = io.BytesIO()
        frame.save(buffer, format="PNG", optimize=True)
    except Exception:  # Broken or unknown container: leave it to the Bridge.
        return raw, mime
    return buffer.getvalue(), "image/png"


@register(
    PLUGIN_NAME,
    "cube-lover",
    "通过 LMArenaBridge 提供模型列表、模型切换、文生图和图生图",
    "0.4.7",
)
class ArenaImagePlugin(Star):
    """Commands for the image-capable models exposed by LMArenaBridge."""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.context = context
        self.config = config or {}
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.output_dir = self.data_dir / "generated"
        self.selection_file = self.data_dir / "session_models.json"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._selected_models: dict[str, str] = self._load_selection()
        self._models_cache: list[dict[str, Any]] = []
        self._models_cached_at = 0.0
        self._models_lock = asyncio.Lock()
        self._model_health_cache: dict[str, dict[str, float]] = {}
        self._model_health_cached_at = 0.0
        self._model_health_lock = asyncio.Lock()
        self._selection_lock = asyncio.Lock()
        self._generation_lock = asyncio.Lock()
        self._active_generations = 0
        self._last_generation_seconds = 0.0
        self._interactive_auth_session_id = ""

    async def initialize(self):
        logger.info("[arena_image] 插件已加载，Bridge 地址：%s", self._bridge_url())

    async def terminate(self):
        return None

    def _bridge_url(self) -> str:
        return str(self.config.get("bridge_url") or "http://arena-bridge:8000").strip()

    def _client(self) -> ArenaBridgeClient:
        return ArenaBridgeClient(
            self._bridge_url(),
            str(self.config.get("bridge_api_key") or ""),
            timeout=_as_float(
                self.config.get("request_timeout"),
                300.0,
                10.0,
                1800.0,
            ),
            rate_limit_retries=_as_int(self.config.get("rate_limit_retries"), 2, 0, 5),
            rate_limit_max_wait=_as_float(
                self.config.get("rate_limit_max_wait"),
                30.0,
                1.0,
                300.0,
            ),
        )

    def _input_max_bytes(self) -> int:
        return _as_int(
            self.config.get("max_image_bytes"),
            10 * 1024 * 1024,
            1024,
            50 * 1024 * 1024,
        )

    def _output_max_bytes(self) -> int:
        return _as_int(
            self.config.get("max_output_image_bytes"),
            DEFAULT_MAX_OUTPUT_IMAGE_BYTES,
            64 * 1024,
            256 * 1024 * 1024,
        )

    def _send_max_bytes(self) -> int:
        return _as_int(
            self.config.get("send_image_max_bytes"),
            DEFAULT_SEND_IMAGE_MAX_BYTES,
            256 * 1024,
            64 * 1024 * 1024,
        )

    @staticmethod
    def _is_private_chat(event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_private_chat", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return not str(getattr(event, "get_group_id", lambda: "")() or "").strip()

    @staticmethod
    def _rate_limit_hint(exc: Exception) -> str | None:
        """Translate upstream throttling into an actionable Chinese message."""
        if not isinstance(exc, BridgeError) or not exc.is_rate_limited:
            return None
        wait = int(exc.retry_after or 0)
        cooldown = f"{wait} 秒" if wait > 0 else "半分钟到一分钟"
        return (
            "竞技场上游正在限流（已自动重试仍未通过）。\n"
            f"请等待约 {cooldown} 后再试，连续重试只会继续触发限流。\n"
            "如果一直限流，可用 /竞技场画图模型 或 /竞技场灰测模型 换一个模型。"
        )

    @staticmethod
    def _verification_hint(exc: Exception) -> str | None:
        if not isinstance(exc, BridgeError) or not exc.requires_interactive_auth:
            return None
        text = str(exc).casefold()
        if exc.code == "arena_session_rejected":
            return (
                "Arena 拒绝了这次请求，但服务器上保存的 Arena 会话还没有过期。\n"
                "这基本都是 reCAPTCHA / Cloudflare 风控，不是登录失效，重新绑定不会有帮助。\n"
                "请让管理员私聊机器人运行：/竞技场验证，在服务器浏览器里过一次验证后重试。\n"
                "想确认会话细节可运行：/竞技场验证状态"
            )
        auth_error = exc.code in {
            "arena_auth_expired",
            "arena_auth_required",
            "arena_login_required",
            "authentication_error",
            "http_401",
            "401",
        } or any(
            marker in text
            for marker in (
                "lmarena auth token",
                "arena-auth",
                "auth token has expired",
                "auth token is invalid",
                "login required",
                "login_gate",
            )
        )
        if (
            exc.code == "arena_login_required"
            or "login_gate" in text
            or "login required" in text
        ):
            return (
                "检测到 Arena 现在要求登录账号才能出图。\n"
                "请让管理员私聊机器人运行：/竞技场验证\n"
                "打开链接后，在服务器浏览器里用 Google 或邮箱登录 Arena，不要只过 Cloudflare。"
            )
        if auth_error:
            return (
                "检测到 Arena 会话 Cookie 已失效或无效。\n"
                "请让管理员私聊机器人运行：/竞技场重新绑定，获取新的服务器浏览器链接。\n"
                "打开链接后完成 Arena 登录和 CF/Turnstile 验证，Cookie 会自动保存；"
                "完成后运行：/竞技场验证状态"
            )
        return (
            "检测到服务器端 Arena/Cloudflare 验证。\n"
            "请让管理员私聊机器人运行：/竞技场验证（Cookie 失效时改用 /竞技场重新绑定）\n"
            "打开服务器浏览器链接并完成验证后，再运行：/竞技场验证状态"
        )

    @staticmethod
    def _format_verification_status(payload: dict[str, Any]) -> str:
        status = str(payload.get("status") or "unknown")
        logged_in = bool(payload.get("has_logged_in"))
        if status == "verified" or bool(payload.get("verified")):
            headline = "服务器浏览器验证已完成，可以重试画图命令。"
        elif payload.get("has_cf_clearance") and payload.get("has_arena_auth") and not logged_in:
            headline = "CF 已通过，但仍是匿名会话。请在服务器浏览器里登录 Arena 账号后再查状态。"
        elif status in {"starting", "waiting"}:
            headline = "服务器浏览器正在等待你完成 Arena 登录/CF 验证。"
        elif status == "expired":
            headline = "验证会话已过期，请重新运行 /竞技场验证。"
        else:
            headline = str(payload.get("message") or "验证状态未知。")
        details = [
            headline,
            f"CF Cookie：{'已获取' if payload.get('has_cf_clearance') else '未获取'}",
            f"Arena 会话：{'已获取' if payload.get('has_arena_auth') else '未获取'}",
            f"账号登录：{'已登录' if logged_in else '未登录（匿名无法出图）'}",
        ]
        source = str(payload.get("session_source") or "").strip()
        if source:
            expires_in = payload.get("session_expires_in")
            try:
                minutes = None if expires_in is None else max(0, int(expires_in) // 60)
            except (TypeError, ValueError):
                minutes = None
            if minutes is None:
                details.append(f"会话来源：{source}")
            else:
                details.append(f"会话来源：{source}（还有约 {minutes} 分钟）")
        action = str(payload.get("recaptcha_action") or "").strip()
        if action:
            note = ""
            if action == "sign_up" and payload.get("session_logged_in"):
                note = "（异常：已登录时应为 chat_submit，请更新 Bridge）"
            details.append(f"reCAPTCHA 动作：{action}{note}")
        if not logged_in and status in {"waiting", "expired", "starting"}:
            details.append("重新获取链接：/竞技场重新绑定（需私聊）")
        return "\n".join(details)

    def _session_key(self, event: AstrMessageEvent) -> str:
        value = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if value:
            return value
        for getter_name in ("get_group_id", "get_sender_id"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                try:
                    value = str(getter() or "").strip()
                except Exception:
                    value = ""
                if value:
                    return value
        return "default"

    def _load_selection(self) -> dict[str, str]:
        try:
            value = json.loads(self.selection_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(value, dict):
            return {}
        cleaned = {
            str(key): str(model)
            for key, model in value.items()
            if str(key).strip() and str(model).strip()
        }
        if GLOBAL_SELECTION_KEY in cleaned:
            return {GLOBAL_SELECTION_KEY: cleaned[GLOBAL_SELECTION_KEY]}
        # Migrate the old per-chat format.  The last saved value is the most
        # recently selected model and becomes the single global selection.
        if cleaned:
            return {GLOBAL_SELECTION_KEY: next(reversed(cleaned.values()))}
        return {}

    async def _save_selection(self) -> None:
        async with self._selection_lock:
            self.selection_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.selection_file.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(
                    dict(self._selected_models),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(self.selection_file)

    async def _fetch_models(self, *, force: bool = False) -> list[dict[str, Any]]:
        ttl = _as_int(self.config.get("model_cache_seconds"), 30, 0, 3600)
        if not force and self._models_cache and time.monotonic() - self._models_cached_at < ttl:
            return list(self._models_cache)
        async with self._models_lock:
            if not force and self._models_cache and time.monotonic() - self._models_cached_at < ttl:
                return list(self._models_cache)
            raw_models = await self._client().list_models()
            image_only = bool(self.config.get("image_models_only", True))
            models = []
            for model in raw_models:
                if not self._model_id(model):
                    continue
                if image_only and not model_is_image_capable(model)[0]:
                    continue
                models.append(model)
            # Newest first.  Arena appends gray-test checkpoints at arbitrary
            # positions in its own table, and the ones worth trying are always the
            # freshest; rows whose id carries no timestamp (Arena's pre-UUIDv7
            # models) keep Arena's ordering at the tail.
            models.sort(key=lambda item: model_created_at(item) or 0, reverse=True)
            # Arena keeps several rows per model name (`gpt-image-2 (medium)` has
            # five).  Every row collapses to the same name in a request, so the
            # extras only padded the list with duplicate lines under different
            # numbers.  Keep the newest row of each name -- the list is already
            # newest-first -- and let the Bridge pick the healthiest variant.
            deduped: dict[str, dict[str, Any]] = {}
            for item in models:
                deduped.setdefault(self._model_id(item).casefold(), item)
            models = list(deduped.values())
            self._models_cache = models
            self._models_cached_at = time.monotonic()
            return list(models)

    async def _fetch_model_health(self, *, force: bool = False) -> dict[str, dict[str, float]]:
        """Read recent Bridge health statuses; missing models have not been tested."""
        ttl = max(0, _as_int(self.config.get("model_health_cache_seconds"), 30, 0, 3600))
        if not force and self._model_health_cache and time.monotonic() - self._model_health_cached_at < ttl:
            return dict(self._model_health_cache)
        async with self._model_health_lock:
            if not force and self._model_health_cache and time.monotonic() - self._model_health_cached_at < ttl:
                return dict(self._model_health_cache)
            payload = await self._client().model_health()
            health: dict[str, dict[str, float]] = {}
            for item in payload.get("models", []) if isinstance(payload, dict) else []:
                if not isinstance(item, dict):
                    continue
                model_id = str(item.get("id") or "").strip()
                try:
                    status_code = int(item.get("status_code") or 0)
                except (TypeError, ValueError):
                    continue
                try:
                    checked_at = float(item.get("checked_at") or 0)
                except (TypeError, ValueError):
                    checked_at = 0.0
                if model_id and status_code > 0:
                    health[model_id] = {"status_code": status_code, "checked_at": checked_at}
            self._model_health_cache = health
            self._model_health_cached_at = time.monotonic()
            return dict(health)

    @staticmethod
    def _model_id(model: dict[str, Any]) -> str:
        return str(model.get("id") or model.get("publicName") or model.get("name") or "").strip()

    async def _resolve_model(
        self,
        event: AstrMessageEvent,
        requested: str = "",
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        models = await self._fetch_models()
        if not models:
            raise BridgeError("Bridge 尚未获取到模型列表")
        requested = str(requested or "").strip()
        if requested.isdigit():
            index = int(requested) - 1
            if 0 <= index < len(models):
                return models[index], models
            raise BridgeError(f"模型编号超出范围：1-{len(models)}")

        by_id = {self._model_id(model).casefold(): model for model in models}
        if requested:
            selected = by_id.get(requested.casefold())
            if selected:
                return selected, models
            raise BridgeError(f"模型不存在：{requested}")

        global_model = self._selected_models.get(GLOBAL_SELECTION_KEY, "")
        default_model = str(self.config.get("default_model") or "").strip()
        for candidate in (global_model, default_model):
            if candidate and candidate.casefold() in by_id:
                return by_id[candidate.casefold()], models
        return models[0], models

    @staticmethod
    def _model_kind(model: dict[str, Any]) -> str:
        output_image, input_image = model_is_image_capable(model)
        if output_image and input_image:
            return "文生图/图生图"
        if output_image:
            return "文生图"
        if input_image:
            return "图片输入"
        return "其他"

    @staticmethod
    def _model_created_text(model: dict[str, Any]) -> str:
        """Render Arena's row creation date; empty when the Bridge cannot tell."""
        created = model_created_at(model)
        if not created:
            return ""
        try:
            stamp = time.strftime("%Y-%m-%d", time.localtime(created))
        except (OSError, OverflowError, ValueError):
            return ""
        return f" 创建 {stamp}"

    @staticmethod
    def _health_age_text(checked_at: float) -> str:
        """How long ago the Bridge saw this result; empty when it did not say."""
        try:
            age = time.time() - float(checked_at or 0)
        except (TypeError, ValueError):
            return ""
        if checked_at <= 0 or age < 0:
            return ""
        if age < 60:
            return "刚刚"
        if age < 3600:
            return f"{int(age // 60)}分钟前"
        if age < 86400:
            return f"{int(age // 3600)}小时前"
        return f"{int(age // 86400)}天前"

    @classmethod
    def _model_health_text(cls, entry: Any) -> str:
        """Annotate the last upstream result: the failing code, or a bare ✅."""
        if isinstance(entry, dict):
            try:
                status = int(entry.get("status_code") or 0)
            except (TypeError, ValueError):
                return ""
            checked_at = entry.get("checked_at") or 0
        else:
            try:
                status = int(entry or 0)
            except (TypeError, ValueError):
                return ""
            checked_at = 0
        if status <= 0:
            return ""
        if 200 <= status < 300:
            return " ✅"
        age = cls._health_age_text(checked_at)
        return f" ⚠{status} {age}" if age else f" ⚠{status}"

    @staticmethod
    def _model_is_stealth(model: dict[str, Any]) -> bool:
        """Whether Arena is hiding this row's vendor.

        Gray-test checkpoints ship with no `organization`, which the Bridge
        renders as `owned_by: lmarena`; every branded model carries its real
        vendor there.
        """
        owner = str(model.get("owned_by") or "").strip().casefold()
        return owner in {"", "lmarena", "unknown"}

    async def _model_list_text(self, event: AstrMessageEvent, *, stealth: bool) -> str:
        """Render one half of the model list.

        Arena exposes well over a hundred image models once the gray-test rows
        are allowed, which is far too many for a single chat message, so the
        gray-test half and the branded half get a command each.  Numbers stay
        the row's position in the *full* list, so `/竞技场切换模型 编号`
        resolves the same model whichever half you read it from -- at the cost
        of each half skipping numbers.
        """
        models = await self._fetch_models(force=True)
        current, _ = await self._resolve_model(event)
        health_map = await self._fetch_model_health()
        current_id = self._model_id(current)
        chosen = [
            (position, model)
            for position, model in enumerate(models, start=1)
            if self._model_is_stealth(model) is stealth
        ]
        title = "竞技场灰测模型" if stealth else "竞技场画图模型"
        lines = [f"{title}（{len(chosen)} 个，画图模型共 {len(models)} 个）："]
        limit = _as_int(self.config.get("model_list_limit"), 50, 1, 200)
        for position, model in chosen[:limit]:
            model_id = self._model_id(model)
            marker = " ← 当前" if model_id == current_id else ""
            status_text = self._model_health_text(health_map.get(model_id))
            created_text = self._model_created_text(model)
            lines.append(
                f"{position}. {model_id} [{self._model_kind(model)}]"
                f"{created_text}{status_text}{marker}"
            )
        if len(chosen) > limit:
            lines.append(f"……其余 {len(chosen) - limit} 个已省略，可调整 model_list_limit。")
        lines.append("用法：/竞技场切换模型 编号或完整模型名（编号两个列表通用，所以不连号）")
        lines.append(
            "另一半：/竞技场画图模型（正式模型）"
            if stealth
            else "另一半：/竞技场灰测模型（含蒙娜丽莎）"
        )
        return "\n".join(lines)

    @filter.command("竞技场画图模型", alias={"arena画图模型", "竞技场模型列表", "arena模型列表"})
    async def list_models(self, event: AstrMessageEvent):
        """Branded image models: every row where Arena names the vendor."""
        try:
            text = await self._model_list_text(event, stealth=False)
        except Exception as exc:
            yield event.plain_result(f"读取模型列表失败：{_display_error(exc)}")
            return
        yield event.plain_result(text)

    @filter.command(
        "竞技场灰测模型",
        alias={"arena灰测模型", "竞技场隐身模型", "arena隐身模型", "竞技场灰测"},
    )
    async def list_stealth_models(self, event: AstrMessageEvent):
        """Gray-test checkpoints: the rows Arena ships without a vendor."""
        try:
            text = await self._model_list_text(event, stealth=True)
        except Exception as exc:
            yield event.plain_result(f"读取灰测模型列表失败：{_display_error(exc)}")
            return
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("竞技场切换模型", alias={"arena切换模型"})
    async def switch_model(
        self,
        event: AstrMessageEvent,
        model: GreedyStr = GreedyStr,
    ):
        """Switch the shared model for every chat.  Admin only: it is global."""
        requested = str(model or "").strip()
        if not requested:
            yield event.plain_result("用法：/竞技场切换模型 编号或完整模型名")
            return
        try:
            selected, _ = await self._resolve_model(event, requested)
            model_id = self._model_id(selected)
            self._selected_models.clear()
            self._selected_models[GLOBAL_SELECTION_KEY] = model_id
            await self._save_selection()
            output_image, input_image = model_is_image_capable(selected)
            capabilities = (
                f"文生图={'是' if output_image else '未知'}，"
                f"图生图={'是' if input_image else '未知'}"
            )
            yield event.plain_result(f"所有群聊已切换到：{model_id}\n{capabilities}")
        except Exception as exc:
            yield event.plain_result(f"切换失败：{_display_error(exc)}")

    @filter.command("jjc", alias={"竞技场"})
    async def unified_image(
        self,
        event: AstrMessageEvent,
        prompt: GreedyStr = GreedyStr,
    ):
        """Unified text-to-image/image-to-image command."""
        prompt_text = str(prompt or "").strip()
        try:
            images = await self._collect_input_images(event)
        except Exception as exc:
            yield event.plain_result(f"读取参考图失败：{_display_error(exc)}")
            return

        if not prompt_text and not images:
            yield event.plain_result("用法：/jjc 画面描述；也可以附图或引用图片后使用 /jjc 描述")
            return

        is_image_to_image = bool(images)
        actual_prompt = prompt_text or "根据参考图生成一张更精致的图片"
        mode_text = "图生图" if is_image_to_image else "文生图"
        try:
            selected, _ = await self._resolve_model(event)
            model_id = self._model_id(selected)
        except Exception as exc:
            yield event.plain_result(f"读取当前模型失败：{_display_error(exc)}")
            return
        yield event.plain_result(
            f"已收到，当前模型：{model_id}\n"
            f"开始{mode_text}：{actual_prompt}\n"
            "正在提交到竞技场画图模型，请稍候……"
        )
        async for result in self._generate(
            event,
            actual_prompt,
            include_input_images=is_image_to_image,
            input_images=images if is_image_to_image else None,
            model_id=model_id,
        ):
            yield result

    @filter.command("竞技场画图", alias={"arena画图"})
    async def text_to_image(
        self,
        event: AstrMessageEvent,
        prompt: GreedyStr = GreedyStr,
    ):
        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            yield event.plain_result("用法：/竞技场画图 你的画面描述")
            return
        try:
            selected, _ = await self._resolve_model(event)
            model_id = self._model_id(selected)
        except Exception as exc:
            yield event.plain_result(f"读取当前模型失败：{_display_error(exc)}")
            return
        yield event.plain_result(
            f"已收到，当前模型：{model_id}\n"
            f"开始文生图：{prompt_text}\n"
            "正在提交到竞技场画图模型，请稍候……"
        )
        async for result in self._generate(
            event,
            prompt_text,
            include_input_images=False,
            model_id=model_id,
        ):
            yield result

    @filter.command("竞技场图生图", alias={"arena图生图"})
    async def image_to_image(
        self,
        event: AstrMessageEvent,
        prompt: GreedyStr = GreedyStr,
    ):
        prompt_text = str(prompt or "").strip()
        try:
            images = await self._collect_input_images(event)
        except Exception as exc:
            yield event.plain_result(f"读取参考图失败：{_display_error(exc)}")
            return
        if not images:
            yield event.plain_result(
                "请在消息中附图，或引用一张图片后使用 /竞技场图生图 描述"
            )
            return
        try:
            selected, _ = await self._resolve_model(event)
            model_id = self._model_id(selected)
        except Exception as exc:
            yield event.plain_result(f"读取当前模型失败：{_display_error(exc)}")
            return
        actual_prompt = prompt_text or "根据参考图生成一张更精致的图片"
        yield event.plain_result(
            f"已收到，当前模型：{model_id}\n"
            f"开始图生图：{actual_prompt}\n"
            "正在提交到竞技场画图模型，请稍候……"
        )
        async for result in self._generate(
            event,
            actual_prompt,
            include_input_images=True,
            input_images=images,
            model_id=model_id,
        ):
            yield result

    async def _interactive_auth_payload(self) -> dict[str, Any]:
        """Reuse a live session and recover cleanly after a Bridge restart."""
        if self._interactive_auth_session_id:
            current = await self._client().interactive_auth_status(
                self._interactive_auth_session_id
            )
            current_status = str(current.get("status") or "")
            if current_status in {"starting", "waiting"}:
                return current
        payload = await self._client().start_interactive_auth()
        self._interactive_auth_session_id = str(payload.get("session_id") or "").strip()
        return payload

    @staticmethod
    def _interactive_auth_message(
        payload: dict[str, Any],
        *,
        reveal_url: bool = False,
    ) -> str:
        message = ArenaImagePlugin._format_verification_status(payload)
        status = str(payload.get("status") or "")
        browser_url = str(payload.get("browser_url") or "").strip()
        if status in {"starting", "waiting"} and browser_url:
            if reveal_url:
                message += f"\n服务器浏览器链接：\n{browser_url}"
            else:
                message += "\n验证链接只在私聊发放，请私聊机器人运行：/竞技场验证"
        elif status == "verified":
            message += "\n如需重新绑定 Cookie，请私聊运行：/竞技场重新绑定"
        return message

    # The browser link hands out control of a server-side browser that holds the
    # logged-in Arena/Google session, so it never goes to a group chat.
    _GROUP_LINK_REFUSAL = (
        "为安全起见，服务器浏览器链接只在私聊发放。\n"
        "请私聊机器人再运行一次这个命令。"
    )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("竞技场验证", alias={"arena验证"})
    async def interactive_verify(self, event: AstrMessageEvent):
        """Open the visible server browser for a manual Arena/CF challenge."""
        if not self._is_private_chat(event):
            yield event.plain_result(self._GROUP_LINK_REFUSAL)
            return
        try:
            self._interactive_auth_session_id = ""
            payload = await self._client().start_interactive_auth()
            self._interactive_auth_session_id = str(payload.get("session_id") or "").strip()
            browser_url = str(payload.get("browser_url") or "").strip()
            if not browser_url:
                raise BridgeError("服务器浏览器链接为空")
            yield event.plain_result(
                "已打开服务器浏览器验证会话。\n"
                "请打开链接，点击“进入验证浏览器”，然后：\n"
                "1. 如弹出 Cloudflare，先完成验证\n"
                "2. 必须在 Arena 页面登录账号（Google 或邮箱），匿名会话无法出图\n"
                f"{browser_url}\n"
                "完成后运行：/竞技场验证状态"
            )
        except Exception as exc:
            yield event.plain_result(f"启动服务器浏览器验证失败：{_display_error(exc)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("竞技场重新绑定", alias={"arena重新绑定"})
    async def interactive_rebind(self, event: AstrMessageEvent):
        """Show the link used to refresh the server-side Arena session cookie."""
        if not self._is_private_chat(event):
            yield event.plain_result(self._GROUP_LINK_REFUSAL)
            return
        try:
            self._interactive_auth_session_id = ""
            payload = await self._client().start_interactive_auth()
            self._interactive_auth_session_id = str(payload.get("session_id") or "").strip()
            browser_url = str(payload.get("browser_url") or "").strip()
            if not browser_url:
                raise BridgeError("服务器浏览器链接为空")
            yield event.plain_result(
                "已生成 Cookie 重新绑定链接。\n"
                "请打开链接，点击“进入验证浏览器”，登录 Arena 账号（Google/邮箱）：\n"
                f"{browser_url}\n"
                "完成后 Bridge 会自动读取并保存新的 Cookie；"
                "随后运行：/竞技场验证状态"
            )
        except Exception as exc:
            yield event.plain_result(f"生成 Cookie 重新绑定链接失败：{_display_error(exc)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("竞技场验证状态", alias={"arena验证状态"})
    async def interactive_verify_status(self, event: AstrMessageEvent):
        """Check and persist the current server-browser verification session."""
        try:
            if self._interactive_auth_session_id:
                payload = await self._client().interactive_auth_status(
                    self._interactive_auth_session_id
                )
            else:
                payload = await self._client().latest_interactive_auth_status()
                self._interactive_auth_session_id = str(
                    payload.get("session_id") or ""
                ).strip()
            yield event.plain_result(
                self._interactive_auth_message(
                    payload,
                    reveal_url=self._is_private_chat(event),
                )
            )
        except Exception as exc:
            yield event.plain_result(
                f"读取验证状态失败：{_display_error(exc)}\n"
                "需要验证时私聊运行：/竞技场验证；Cookie 失效时私聊运行：/竞技场重新绑定"
            )

    @filter.command("竞技场状态", alias={"arena状态"})
    async def status(self, event: AstrMessageEvent):
        try:
            health = await self._client().health()
            models = await self._fetch_models()
            current, _ = await self._resolve_model(event)
            state = health.get("status", "unknown")
            yield event.plain_result(
                f"Bridge：{state}\n"
                f"模型数：{len(models)}\n"
                f"当前模型：{self._model_id(current)}\n"
                f"数据目录：{self.data_dir}"
            )
        except Exception as exc:
            yield event.plain_result(f"Bridge 状态读取失败：{_display_error(exc)}")

    async def _collect_input_images(self, event: AstrMessageEvent) -> list[str]:
        """Convert current and quoted AstrBot Image components to data URIs."""
        max_images = _as_int(self.config.get("max_input_images"), 4, 1, 8)
        max_bytes = self._input_max_bytes()
        result: list[str] = []
        seen_sources: set[str] = set()
        # Content digests catch the same picture arriving through two different
        # paths, e.g. inline in the message and again via the quoted message.
        seen_digests: set[str] = set()
        components = getattr(getattr(event, "message_obj", None), "message", []) or []

        def remember(value: str) -> bool:
            digest = hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()
            if digest in seen_digests:
                return False
            seen_digests.add(digest)
            return True

        async def add_component(component: Image, source_key: str) -> None:
            if len(result) >= max_images or source_key in seen_sources:
                return
            seen_sources.add(source_key)
            source = str(
                getattr(component, "url", None)
                or getattr(component, "file", None)
                or source_key
            ).strip()
            try:
                raw_base64 = await component.convert_to_base64()
                raw, mime = decode_image_value(
                    raw_base64,
                    source=source,
                    max_bytes=max_bytes,
                )
                if _is_animated_upload(raw, mime):
                    original_mime = mime
                    original_size = len(raw)
                    frame, frame_mime = await asyncio.to_thread(_first_frame_bytes, raw, mime)
                    if frame is raw:
                        # The helper hands back the very same object when it cannot
                        # decode the animation, which also covers a missing Pillow.
                        logger.warning(
                            "[arena_image] 参考图是动图（%s）但无法取帧（缺少 Pillow？），原样上传",
                            original_mime,
                        )
                    else:
                        raw, mime = frame, frame_mime
                        if len(raw) > max_bytes:
                            raw, mime = await asyncio.to_thread(
                                _shrink_image_bytes, raw, mime, max_bytes
                            )
                        logger.info(
                            "[arena_image] 参考图是动图（%s），已取第一帧作为 %s：%s -> %s",
                            original_mime,
                            mime,
                            _human_bytes(original_size),
                            _human_bytes(len(raw)),
                        )
                data_uri = data_uri_from_base64(
                    raw,
                    source=source,
                    mime_type=mime,
                    max_bytes=max_bytes,
                )
                if remember(data_uri):
                    result.append(data_uri)
            except Exception as exc:
                # Keep a remote source URL as a fallback.  The Bridge will
                # download it and upload it to Arena, which covers adapters
                # whose Image.convert_to_base64() is unavailable.
                if source.lower().startswith(("http://", "https://")):
                    if remember(source):
                        result.append(source)
                    logger.warning(
                        "[arena_image] 参考图 Base64 读取失败，交给 Bridge 下载上传：%s",
                        _display_error(exc),
                    )
                else:
                    logger.debug(
                        "[arena_image] 跳过读取失败的参考图：%s",
                        _display_error(exc),
                    )

        for index, component in enumerate(components):
            if isinstance(component, Image):
                source = str(
                    getattr(component, "url", None) or getattr(component, "file", None) or ""
                )
                if source.lower().startswith(("data:", "base64://")):
                    source_key = f"inline:{hash(source)}"
                else:
                    source_key = source or f"component:{index}:{id(component)}"
                await add_component(component, source_key)
            elif isinstance(component, At):
                qq = str(getattr(component, "qq", "") or "").strip()
                if qq and qq.casefold() != "all" and len(result) < max_images:
                    avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
                    try:
                        await add_component(
                            Image.fromURL(avatar_url),
                            f"at-avatar:{qq}",
                        )
                    except Exception as exc:
                        logger.debug(
                            "[arena_image] 读取 @头像失败：%s",
                            _display_error(exc),
                        )

        if len(result) < max_images and extract_quoted_message_images is not None:
            try:
                references = await extract_quoted_message_images(event)
            except Exception as exc:
                logger.debug(
                    "[arena_image] 读取引用消息图片失败：%s",
                    _display_error(exc),
                )
                references = []
            for reference in references or []:
                if len(result) >= max_images:
                    break
                if isinstance(reference, Image):
                    await add_component(reference, f"quoted:{id(reference)}")
                    continue
                source_key = str(reference or "").strip()
                if not source_key or source_key in seen_sources:
                    continue
                try:
                    source_lower = source_key.lower()
                    if source_lower.startswith(("http://", "https://")):
                        component = Image.fromURL(source_key)
                    elif source_lower.startswith(("data:", "base64://", "file://")):
                        component = Image(file=source_key)
                    else:
                        component = Image.fromFileSystem(source_key)
                    await add_component(component, source_key)
                except Exception as exc:
                    logger.debug(
                        "[arena_image] 跳过无效的引用图片：%s",
                        _display_error(exc),
                    )
        logger.info("[arena_image] 图生图参考图数量：%d", len(result))
        return result

    async def _generate(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        include_input_images: bool,
        input_images: list[str] | None = None,
        model_id: str | None = None,
    ):
        # Generation is serialised, so tell the user where they are in the queue
        # instead of leaving them on "正在提交" for several minutes.
        max_queue = _as_int(self.config.get("max_queue_depth"), 5, 1, 50)
        waiting = self._active_generations
        if waiting >= max_queue:
            yield event.plain_result(
                f"排队已满（{waiting}/{max_queue}），前面的图还在画，请稍后再发。"
            )
            return
        if waiting > 0:
            estimate = int(self._last_generation_seconds * waiting)
            eta = f"，预计还要等 {estimate} 秒左右" if estimate > 0 else ""
            yield event.plain_result(f"前面还有 {waiting} 个出图任务，已排队{eta}。")

        self._active_generations += 1
        try:
            async with self._generation_lock:
                try:
                    if not model_id:
                        selected, _ = await self._resolve_model(event)
                        model_id = self._model_id(selected)
                    generation_started_at = time.monotonic()
                    response = await self._client().complete(
                        model=model_id,
                        prompt=prompt,
                        images=input_images if include_input_images else None,
                    )
                    urls = image_urls(response)
                    text = response_text(response).strip()
                    if not urls:
                        yield event.plain_result(
                            f"模型 {model_id} 返回了文本而不是图片：\n"
                            f"{text or '没有可显示的内容'}"
                        )
                        return

                    max_outputs = _as_int(self.config.get("max_output_images"), 1, 1, 4)
                    sent = 0
                    failures: list[str] = []
                    for url in urls:
                        if sent >= max_outputs:
                            break
                        try:
                            path = await self._materialize_output(url)
                        except BridgeError as exc:
                            failures.append(_display_error(exc))
                            logger.warning(
                                "[arena_image] 跳过无法下载的候选图片：%s",
                                _display_error(exc),
                            )
                            continue
                        elapsed_seconds = time.monotonic() - generation_started_at
                        result = event.plain_result(
                            f"模型：{model_id}\n"
                            f"画图耗时：{elapsed_seconds:.1f} 秒"
                        )
                        result.chain.append(Image.fromFileSystem(str(path)))
                        yield result
                        sent += 1

                    if sent:
                        self._last_generation_seconds = time.monotonic() - generation_started_at
                        self._prune_outputs()
                    elif text:
                        # Every candidate turned out not to be an image, so the
                        # model's own answer is the useful reply.
                        yield event.plain_result(
                            f"模型 {model_id} 返回了文本而不是图片：\n{text}"
                        )
                    else:
                        yield event.plain_result(
                            "生成失败："
                            + (failures[0] if failures else "Bridge 没有返回可用图片")
                        )
                except Exception as exc:
                    logger.exception("[arena_image] 生成失败")
                    hint = self._rate_limit_hint(exc) or self._verification_hint(exc)
                    if hint:
                        yield event.plain_result(hint)
                    else:
                        yield event.plain_result(f"生成失败：{_display_error(exc)}")
        finally:
            self._active_generations = max(0, self._active_generations - 1)

    async def _materialize_output(self, value: str) -> Path:
        max_bytes = self._output_max_bytes()
        oversize = (
            f"生成图片过大（上限 {_human_bytes(max_bytes)}），"
            "可在插件配置里调高 max_output_image_bytes。"
        )
        clean_value = str(value or "").strip()
        if clean_value.lower().startswith(("data:", "base64://")):
            raw, mime = decode_image_value(
                clean_value,
                max_bytes=max_bytes,
            )
        elif clean_value.lower().startswith(("http://", "https://")):
            try:
                async with (
                    httpx.AsyncClient(
                        follow_redirects=True,
                        timeout=_as_float(
                            self.config.get("request_timeout"),
                            300.0,
                            10.0,
                            1800.0,
                        ),
                    ) as client,
                    client.stream("GET", clean_value) as response,
                ):
                    if response.status_code >= 400:
                        raise BridgeError(
                            f"下载生成图片失败（HTTP {response.status_code}）",
                            status_code=response.status_code,
                        )
                    content_length = response.headers.get("content-length", "")
                    try:
                        if content_length and int(content_length) > max_bytes:
                            raise BridgeError(
                                f"{oversize}（实际约 {_human_bytes(int(content_length))}）"
                            )
                    except ValueError:
                        pass
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise BridgeError(oversize)
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    response_mime = response.headers.get("content-type", "")
            except httpx.HTTPError as exc:
                raise BridgeError(f"下载生成图片失败：{exc}") from exc
            raw, mime = decode_image_value(
                raw,
                source=clean_value,
                mime_type=response_mime,
                max_bytes=max_bytes,
            )
        else:
            raise BridgeError("Bridge 返回了不支持的图片地址")

        # Chat platforms reject very large attachments; re-encode rather than
        # throwing away an image the model already spent minutes producing.
        send_limit = self._send_max_bytes()
        if len(raw) > send_limit:
            original_size = len(raw)
            raw, mime = await asyncio.to_thread(_shrink_image_bytes, raw, mime, send_limit)
            logger.info(
                "[arena_image] 生成图片超出发送上限，已压缩：%s -> %s",
                _human_bytes(original_size),
                _human_bytes(len(raw)),
            )

        suffix = mimetypes.guess_extension(mime) or ".png"
        path = self.output_dir / f"image-{int(time.time())}-{uuid.uuid4().hex[:12]}{suffix}"
        try:
            path.write_bytes(raw)
        except OSError as exc:
            raise BridgeError(f"保存生成图片失败：{exc}") from exc
        return path

    def _prune_outputs(self) -> None:
        keep = _as_int(self.config.get("max_saved_outputs"), 32, 4, 500)
        try:
            files = sorted(
                (path for path in self.output_dir.iterdir() if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for stale in files[keep:]:
                stale.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("[arena_image] 清理旧图片失败：%s", exc)
