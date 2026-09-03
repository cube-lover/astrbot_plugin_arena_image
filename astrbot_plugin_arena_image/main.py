"""AstrBot image commands backed by the local LMArenaBridge service."""

from __future__ import annotations

import asyncio
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
    model_is_image_capable,
    response_text,
)

PLUGIN_NAME = "astrbot_plugin_arena_image"
GLOBAL_SELECTION_KEY = "__global__"


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


@register(
    PLUGIN_NAME,
    "Codex",
    "通过 LMArenaBridge 提供模型列表、模型切换、文生图和图生图",
    "0.2.1",
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
        self._model_health_cache: dict[str, int] = {}
        self._model_health_cached_at = 0.0
        self._model_health_lock = asyncio.Lock()
        self._selection_lock = asyncio.Lock()
        self._generation_lock = asyncio.Lock()
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
        )

    @staticmethod
    def _verification_hint(exc: Exception) -> str | None:
        if not isinstance(exc, BridgeError) or not exc.requires_interactive_auth:
            return None
        auth_error = exc.code in {
            "arena_auth_expired",
            "arena_auth_required",
            "arena_login_required",
            "authentication_error",
            "http_401",
            "401",
        } or any(
            marker in str(exc).casefold()
            for marker in (
                "lmarena auth token",
                "arena-auth",
                "auth token has expired",
                "auth token is invalid",
                "login required",
                "login_gate",
            )
        )
        if exc.code == "arena_login_required" or "login_gate" in str(exc).casefold() or "login required" in str(exc).casefold():
            return (
                "检测到 Arena 现在要求登录账号才能出图。\n"
                "请运行：/竞技场验证\n"
                "打开链接后，在服务器浏览器里用 Google 或邮箱登录 Arena，不要只过 Cloudflare。"
            )
        if auth_error:
            return (
                "检测到 Arena 会话 Cookie 已失效或无效。\n"
                "请运行：/竞技场重新绑定，获取新的服务器浏览器链接。\n"
                "打开链接后完成 Arena 登录和 CF/Turnstile 验证，Cookie 会自动保存；"
                "完成后运行：/竞技场验证状态"
            )
        return (
            "检测到服务器端 Arena/Cloudflare 验证。\n"
            "请运行：/竞技场验证（Cookie 失效时也可运行 /竞技场重新绑定）\n"
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
        if not logged_in and status in {"waiting", "expired", "starting"}:
            details.append("重新获取链接：/竞技场重新绑定")
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
            self._models_cache = models
            self._models_cached_at = time.monotonic()
            return list(models)

    async def _fetch_model_health(self, *, force: bool = False) -> dict[str, int]:
        """Read recent Bridge health statuses; missing models have not been tested."""
        ttl = max(0, _as_int(self.config.get("model_health_cache_seconds"), 30, 0, 3600))
        if not force and self._model_health_cache and time.monotonic() - self._model_health_cached_at < ttl:
            return dict(self._model_health_cache)
        async with self._model_health_lock:
            if not force and self._model_health_cache and time.monotonic() - self._model_health_cached_at < ttl:
                return dict(self._model_health_cache)
            payload = await self._client().model_health()
            health: dict[str, int] = {}
            for item in payload.get("models", []) if isinstance(payload, dict) else []:
                if not isinstance(item, dict):
                    continue
                model_id = str(item.get("id") or "").strip()
                try:
                    status_code = int(item.get("status_code") or 0)
                except (TypeError, ValueError):
                    continue
                if model_id and status_code > 0:
                    health[model_id] = status_code
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

    @filter.command("竞技场画图模型", alias={"arena画图模型", "竞技场模型列表", "arena模型列表"})
    async def list_models(self, event: AstrMessageEvent):
        try:
            models = await self._fetch_models(force=True)
            current, _ = await self._resolve_model(event)
            health_map = await self._fetch_model_health()
        except Exception as exc:
            yield event.plain_result(f"读取模型列表失败：{_display_error(exc)}")
            return

        limit = _as_int(self.config.get("model_list_limit"), 50, 1, 200)
        lines = [f"Bridge 可用模型（共 {len(models)} 个）："]
        for index, model in enumerate(models[:limit], start=1):
            model_id = self._model_id(model)
            marker = " ← 当前" if model_id == self._model_id(current) else ""
            status = health_map.get(model_id)
            status_text = f" ({status})" if status else ""
            lines.append(f"{index}. {model_id} [{self._model_kind(model)}]{status_text}{marker}")
        if len(models) > limit:
            lines.append(f"……其余 {len(models) - limit} 个已省略，可调整 model_list_limit。")
        lines.append("用法：/竞技场切换模型 编号或完整模型名（列表只含画图模型）")
        yield event.plain_result("\n".join(lines))

    @filter.command("竞技场切换模型", alias={"arena切换模型"})
    async def switch_model(
        self,
        event: AstrMessageEvent,
        model: GreedyStr = GreedyStr,
    ):
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
    def _interactive_auth_message(payload: dict[str, Any]) -> str:
        message = ArenaImagePlugin._format_verification_status(payload)
        status = str(payload.get("status") or "")
        browser_url = str(payload.get("browser_url") or "").strip()
        if status in {"starting", "waiting"} and browser_url:
            message += f"\n服务器浏览器链接：\n{browser_url}"
        elif status == "verified":
            message += "\n如需重新绑定 Cookie，请运行：/竞技场重新绑定"
        return message

    @filter.command("竞技场验证", alias={"arena验证"})
    async def interactive_verify(self, event: AstrMessageEvent):
        """Open the visible server browser for a manual Arena/CF challenge."""
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

    @filter.command("竞技场重新绑定", alias={"arena重新绑定"})
    async def interactive_rebind(self, event: AstrMessageEvent):
        """Show the link used to refresh the server-side Arena session cookie."""
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
            yield event.plain_result(self._interactive_auth_message(payload))
        except Exception as exc:
            yield event.plain_result(
                f"读取验证状态失败：{_display_error(exc)}\n"
                "需要验证时运行：/竞技场验证；Cookie 失效时运行：/竞技场重新绑定"
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
        max_bytes = _as_int(
            self.config.get("max_image_bytes"),
            10 * 1024 * 1024,
            1024,
            50 * 1024 * 1024,
        )
        result: list[str] = []
        seen_sources: set[str] = set()
        components = getattr(getattr(event, "message_obj", None), "message", []) or []

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
                result.append(
                    data_uri_from_base64(
                        raw_base64,
                        source=source,
                        max_bytes=max_bytes,
                    )
                )
            except Exception as exc:
                # Keep a remote source URL as a fallback.  The Bridge will
                # download it and upload it to Arena, which covers adapters
                # whose Image.convert_to_base64() is unavailable.
                if source.lower().startswith(("http://", "https://")):
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
                if not urls:
                    text = response_text(response).strip()
                    yield event.plain_result(
                        f"模型 {model_id} 返回了文本而不是图片：\n{text or '没有可显示的内容'}"
                    )
                    return
                max_outputs = _as_int(self.config.get("max_output_images"), 1, 1, 4)
                for url in urls[:max_outputs]:
                    path = await self._materialize_output(url)
                    elapsed_seconds = time.monotonic() - generation_started_at
                    result = event.plain_result(
                        f"模型：{model_id}\n"
                        f"画图耗时：{elapsed_seconds:.1f} 秒"
                    )
                    result.chain.append(Image.fromFileSystem(str(path)))
                    yield result
                self._prune_outputs()
            except Exception as exc:
                logger.exception("[arena_image] 生成失败")
                hint = self._verification_hint(exc)
                if hint:
                    yield event.plain_result(hint)
                else:
                    yield event.plain_result(f"生成失败：{_display_error(exc)}")

    async def _materialize_output(self, value: str) -> Path:
        max_bytes = _as_int(
            self.config.get("max_image_bytes"),
            10 * 1024 * 1024,
            1024,
            50 * 1024 * 1024,
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
                            raise BridgeError("生成图片超过大小限制")
                    except ValueError:
                        pass
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise BridgeError("生成图片超过大小限制")
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
