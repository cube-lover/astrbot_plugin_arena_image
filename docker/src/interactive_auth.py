"""Interactive server-browser authentication for Arena/Cloudflare challenges.

The bridge already has access to the server-side browser used by the
deployment.  This module exposes a small coordinator around that browser:

* open an Arena tab in a dedicated CDP slot;
* let the operator complete a challenge through the existing noVNC page;
* poll the tab for Arena/Cloudflare cookies;
* persist only the cookies needed by the bridge.

The CDP proxy used by the existing browser service does not pass the
Playwright websocket handshake reliably, so the small websocket client below
uses only the Python standard library.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import Request as UrlRequest
from urllib.request import urlopen


class InteractiveAuthError(RuntimeError):
    """Raised when the configured server browser cannot be controlled."""


class CDPError(RuntimeError):
    """Raised for a Chrome DevTools Protocol command failure."""


def _read_secret_file(path: str) -> str:
    """Read a local deployment secret without ever including it in a response."""
    clean_path = str(path or "").strip()
    if not clean_path:
        return ""
    try:
        with open(clean_path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _interactive_link_secret() -> str:
    """Resolve the signing key from a file first, then an environment variable."""
    return (
        _read_secret_file(os.environ.get("LM_BRIDGE_INTERACTIVE_LINK_SECRET_FILE", ""))
        or os.environ.get("LM_BRIDGE_INTERACTIVE_LINK_SECRET", "").strip()
    )


def _make_interactive_link_token(expires_at: float, secret: str) -> str:
    """Create an opaque, expiring token understood by the noVNC gateway."""
    expiry = str(max(0, int(expires_at)))
    nonce = secrets.token_urlsafe(24)
    payload = f"{expiry}.{nonce}"
    signature = hmac.new(
        secret.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def _build_interactive_gateway_url(
    gateway_url: str,
    *,
    expires_at: float,
    secret: str,
) -> str:
    """Build the public gateway URL while keeping the VNC password server-side."""
    base = str(gateway_url or "").strip().rstrip("/")
    if not base or not secret:
        return ""
    token = _make_interactive_link_token(expires_at, secret)
    return f"{base}/v/{quote(token, safe='')}"


def _http_json(url: str, timeout: float) -> dict[str, Any]:
    request = UrlRequest(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured local endpoint
        value = json.loads(response.read().decode("utf-8", errors="replace"))
    return value if isinstance(value, dict) else {}


class _CDPWebSocket:
    """Minimal async RFC 6455 client for the browser CDP proxy."""

    def __init__(self, endpoint: str, *, timeout: float = 15.0) -> None:
        self.endpoint = str(endpoint or "").strip().rstrip("/")
        self.timeout = max(2.0, float(timeout))
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 0

    async def __aenter__(self) -> "_CDPWebSocket":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        if not self.endpoint:
            raise InteractiveAuthError("未配置服务器浏览器 CDP 地址")

        endpoint_parts = urlsplit(self.endpoint)
        if endpoint_parts.scheme in {"http", "https"}:
            version_url = self.endpoint + "/json/version"
            try:
                version = await asyncio.to_thread(_http_json, version_url, self.timeout)
            except Exception as exc:
                raise InteractiveAuthError("服务器浏览器 CDP 接口不可用") from exc
            websocket_url = str(version.get("webSocketDebuggerUrl") or "").strip()
            if not websocket_url:
                raise InteractiveAuthError("服务器浏览器未返回 CDP WebSocket 地址")
        elif endpoint_parts.scheme in {"ws", "wss"}:
            websocket_url = self.endpoint
        else:
            raise InteractiveAuthError("服务器浏览器 CDP 地址格式无效")

        parts = urlsplit(websocket_url)
        host = parts.hostname
        if not host:
            raise InteractiveAuthError("服务器浏览器 CDP 主机地址无效")
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        # The proxy's /json/version response normally already rewrites the
        # host.  Keep a fallback for proxies that return localhost there.
        if host in {"127.0.0.1", "localhost"} and endpoint_parts.hostname:
            host = endpoint_parts.hostname
            port = endpoint_parts.port or port

        ssl_context = True if parts.scheme == "wss" else None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ssl_context),
                timeout=self.timeout,
            )
        except Exception as exc:
            raise InteractiveAuthError("连接服务器浏览器 CDP 失败") from exc

        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        host_header = f"{host}:{port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: {'https' if parts.scheme == 'wss' else 'http'}://{host_header}\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(request)
        await writer.drain()
        try:
            response_headers = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=self.timeout,
            )
        except Exception as exc:
            writer.close()
            raise InteractiveAuthError("服务器浏览器 CDP 握手超时") from exc

        first_line = response_headers.split(b"\r\n", 1)[0]
        if b" 101 " not in first_line:
            writer.close()
            raise InteractiveAuthError("服务器浏览器 CDP 握手被拒绝")

        self._reader = reader
        self._writer = writer

    async def close(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None:
            return
        try:
            await self._send_frame(0x8, b"")
        except Exception:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def _read_exactly(self, size: int) -> bytes:
        if self._reader is None:
            raise CDPError("CDP 连接未建立")
        return await self._reader.readexactly(size)

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._writer is None:
            raise CDPError("CDP 连接未建立")
        length = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = secrets.token_bytes(4)
        header.extend(mask)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._writer.write(bytes(header) + masked)
        await self._writer.drain()

    async def _receive_frame_with_fin(self) -> tuple[bool, int, bytes]:
        first, second = await self._read_exactly(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self._read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._read_exactly(8))[0]

        masked = bool(second & 0x80)
        mask = await self._read_exactly(4) if masked else b""
        payload = await self._read_exactly(length) if length else b""
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return fin, opcode, payload

    async def _receive_message(self) -> dict[str, Any]:
        chunks: list[bytes] = []
        started = False
        while True:
            fin, opcode, payload = await asyncio.wait_for(
                self._receive_frame_with_fin(),
                timeout=self.timeout,
            )
            if opcode == 0x8:
                raise CDPError("服务器浏览器 CDP 连接已关闭")
            if opcode == 0x9:
                await self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x2}:
                started = True
                chunks = [payload]
            elif opcode == 0x0 and started:
                chunks.append(payload)
            else:
                continue
            if not fin:
                continue
            try:
                value = json.loads(b"".join(chunks).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CDPError("服务器浏览器返回了无效 CDP 消息") from exc
            return value if isinstance(value, dict) else {}

    async def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self._next_id += 1
        command_id = self._next_id
        message: dict[str, Any] = {
            "id": command_id,
            "method": str(method),
        }
        if params:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        await self._send_frame(
            0x1,
            json.dumps(message, separators=(",", ":")).encode("utf-8"),
        )

        while True:
            value = await self._receive_message()
            if value.get("id") != command_id:
                continue
            if value.get("error"):
                error = value.get("error")
                raise CDPError(str(error.get("message") if isinstance(error, dict) else error))
            result = value.get("result")
            return result if isinstance(result, dict) else {}

def _safe_cookie_records(cookies: object) -> list[dict[str, str]]:
    """Keep only cookies used by the bridge, never returning their values."""
    if not isinstance(cookies, list):
        return []
    names = {
        "arena-auth-prod-v1",
        "arena-auth-prod-v1.0",
        "arena-auth-prod-v1.1",
        "cf_clearance",
        "__cf_bm",
        "_cfuvid",
        "provisional_user_id",
    }
    result: list[dict[str, str]] = []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name in names and value:
            result.append({"name": name, "value": value})
    return result


def _has_valid_persisted_arena_auth(config: object) -> bool:
    """Check persisted auth locations without exposing the cookie value."""
    if not isinstance(config, dict):
        return False

    candidates: list[str] = []
    tokens = config.get("auth_tokens")
    if isinstance(tokens, list):
        candidates.extend(str(value or "").strip() for value in tokens)
    candidates.append(str(config.get("auth_token") or "").strip())
    cookie_store = config.get("browser_cookies")
    if isinstance(cookie_store, dict):
        candidates.append(str(cookie_store.get("arena-auth-prod-v1") or "").strip())
        first = str(cookie_store.get("arena-auth-prod-v1.0") or "").strip()
        second = str(cookie_store.get("arena-auth-prod-v1.1") or "").strip()
        if first:
            candidates.append(first + second)

    try:
        from .auth import is_arena_auth_token_expired
    except Exception:
        is_arena_auth_token_expired = None

    for token in candidates:
        if not token:
            continue
        try:
            if is_arena_auth_token_expired and is_arena_auth_token_expired(
                token,
                skew_seconds=0,
            ):
                continue
        except Exception:
            pass
        return True
    return False


def _has_persisted_cf_clearance(config: object) -> bool:
    if not isinstance(config, dict):
        return False
    if str(config.get("cf_clearance") or "").strip():
        return True
    cookies = config.get("browser_cookies")
    return isinstance(cookies, dict) and bool(str(cookies.get("cf_clearance") or "").strip())


def _cookie_value(cookies: list[dict[str, str]], name: str) -> str:
    for item in cookies:
        if str(item.get("name") or "") == name:
            return str(item.get("value") or "").strip()
    return ""


def _live_session_is_verified(
    cookies: list[dict[str, str]],
    *,
    title: str = "",
    url: str = "",
    body_text: str = "",
) -> tuple[bool, bool, bool, bool]:
    """Return (has_cf, has_auth, logged_in, verified) from the live browser tab only."""
    names = {str(item.get("name") or "") for item in cookies}
    has_cf = "cf_clearance" in names and bool(_cookie_value(cookies, "cf_clearance"))
    auth_cookie = _cookie_value(cookies, "arena-auth-prod-v1")
    if not auth_cookie:
        first = _cookie_value(cookies, "arena-auth-prod-v1.0")
        second = _cookie_value(cookies, "arena-auth-prod-v1.1")
        auth_cookie = f"{first}{second}" if first else ""
    if auth_cookie and not auth_cookie.startswith("base64-"):
        auth_cookie = f"base64-{auth_cookie}"
    try:
        from .auth import is_logged_in_arena_auth_token, is_probably_valid_arena_auth_token

        has_auth = bool(auth_cookie) and is_probably_valid_arena_auth_token(auth_cookie)
        logged_in = has_auth and is_logged_in_arena_auth_token(auth_cookie)
    except Exception:
        has_auth = bool(auth_cookie)
        logged_in = False
    lower_title = str(title or "").casefold()
    lower_url = str(url or "").casefold()
    lower_body = str(body_text or "").casefold()
    challenge_visible = (
        "just a moment" in lower_title
        or "challenge" in lower_url
        or "cf-chl-" in lower_url
        or "turnstile" in lower_title
        or "cloudflare" in lower_title
    )
    # A Supabase/Arena cookie can remain present and even carry a future
    # expiry after the server has revoked the session.  In that state the old
    # checker incorrectly reported "verified" while the page had already
    # returned to the login screen.  Treat explicit login/session-expired
    # markers and auth routes as authoritative evidence that the live session
    # is not usable.
    logged_out_page = (
        any(
            marker in lower_url
            for marker in (
                "/login",
                "/signin",
                "/sign-in",
                "/auth/",
            )
        )
        or any(
            marker in lower_title
            for marker in (
                "sign in",
                "log in",
                "登录",
            )
        )
        or any(
            marker in lower_body
            for marker in (
                "session expired",
                "you have been signed out",
                "you've been signed out",
                "please sign in",
                "sign in to continue",
                "log in to continue",
                "重新登录",
                "请登录",
            )
        )
    )
    if logged_out_page:
        logged_in = False
    verified = has_cf and logged_in and not challenge_visible and not logged_out_page
    return has_cf, has_auth, logged_in, verified


def _local_storage_arena_auth_cookie(storage: object) -> str:
    """Recover an Arena session encoded in Supabase/localStorage data."""
    if not isinstance(storage, dict):
        return ""
    try:
        from .auth import maybe_build_arena_auth_cookie_from_signup_response_body
    except Exception:
        return ""
    for raw in storage.values():
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            cookie = maybe_build_arena_auth_cookie_from_signup_response_body(text)
        except Exception:
            cookie = None
        if not cookie:
            continue
        try:
            from .auth import is_arena_auth_token_expired

            if is_arena_auth_token_expired(cookie, skew_seconds=0):
                continue
        except Exception:
            pass
        return str(cookie).strip()
    return ""


@dataclass
class _AuthSession:
    session_id: str
    target_id: str
    browser_url: str
    created_at: float
    expires_at: float
    status: str = "starting"
    has_cf_clearance: bool = False
    has_arena_auth: bool = False
    has_logged_in: bool = False
    last_error: str = ""
    monitor_task: asyncio.Task[Any] | None = None


class InteractiveAuthManager:
    """Coordinate one-time manual verification sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, _AuthSession] = {}
        self._lock = asyncio.Lock()
        self._persist_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_attempt_at = 0.0

    @staticmethod
    def _settings(config: dict[str, Any]) -> tuple[str, str, str, int, float]:
        cdp_url = str(
            config.get("interactive_browser_cdp_url")
            or os.environ.get("LM_BRIDGE_BROWSER_CDP_URL")
            or ""
        ).strip()
        browser_url = str(
            config.get("interactive_browser_vnc_url")
            or os.environ.get("LM_BRIDGE_BROWSER_VNC_URL")
            or ""
        ).strip()
        # The gateway is optional for backward compatibility.  When present,
        # it serves a short-lived wrapper that injects the VNC password only
        # into the browser-side iframe and never into the chat message.
        gateway_url = str(
            config.get("interactive_browser_gateway_url")
            or os.environ.get("LM_BRIDGE_BROWSER_GATEWAY_URL")
            or ""
        ).strip()
        try:
            ttl = max(
                120,
                min(1800, int(config.get("interactive_auth_ttl_seconds", 900))),
            )
        except (TypeError, ValueError):
            ttl = 900
        try:
            poll = max(
                1.0,
                min(15.0, float(config.get("interactive_auth_poll_seconds", 3))),
            )
        except (TypeError, ValueError):
            poll = 3.0
        return cdp_url, browser_url, gateway_url, ttl, poll

    @staticmethod
    def _public_result(session: _AuthSession) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "status": session.status,
            "verified": session.status == "verified",
            "browser_url": session.browser_url,
            "expires_at": int(session.expires_at),
            "has_cf_clearance": session.has_cf_clearance,
            "has_arena_auth": session.has_arena_auth,
            "has_logged_in": session.has_logged_in,
            "message": (
                (
                    "CF 已通过，但仍是匿名会话。Arena 现在要求登录账号才能出图，请在页面用 Google 或邮箱登录后，再运行 /竞技场验证状态。"
                    if session.has_cf_clearance and session.has_arena_auth and not session.has_logged_in
                    else (
                        "CF 已通过，但 Arena 会话尚未获取；请在浏览器中完成登录并等待页面加载，完成后运行 /竞技场验证状态。"
                        if session.has_cf_clearance and not session.has_arena_auth
                        else "请打开服务器浏览器链接，先过 Cloudflare，再登录 Arena 账号（Google/邮箱）；完成后运行 /竞技场验证状态。"
                    )
                )
                if session.status in {"starting", "waiting"}
                else (
                    "服务器浏览器验证已完成，可以重试画图命令。"
                    if session.status == "verified"
                    else "服务器浏览器验证会话已结束，请重新运行验证命令。"
                )
            ),
        }

    async def _create_browser_target(self, cdp_url: str, session_id: str) -> str:
        url = f"https://arena.ai/?mode=direct#lm-bridge-auth-{session_id}"
        async with _CDPWebSocket(cdp_url) as cdp:
            try:
                result = await cdp.command("Target.createTarget", {"url": url})
                target_id = str(result.get("targetId") or "").strip()
                if target_id:
                    attached = await cdp.command(
                        "Target.attachToTarget",
                        {"targetId": target_id, "flatten": True},
                    )
                    session_token = str(attached.get("sessionId") or "").strip()
                    if session_token:
                        await cdp.command(
                            "Page.navigate",
                            {"url": url},
                            session_id=session_token,
                        )
                        await cdp.command(
                            "Target.activateTarget",
                            {"targetId": target_id},
                        )
                    return target_id
            except CDPError:
                # A restricted CDP proxy may disallow Target.createTarget.
                # Reuse a visible Arena page as a fallback.
                pass

            targets = await cdp.command("Target.getTargets")
            infos = targets.get("targetInfos")
            if not isinstance(infos, list):
                raise InteractiveAuthError("服务器浏览器没有可用页面")
            candidate = next(
                (
                    item
                    for item in infos
                    if isinstance(item, dict)
                    and item.get("type") == "page"
                    and "arena.ai" in str(item.get("url") or "")
                ),
                next(
                    (
                        item
                        for item in infos
                        if isinstance(item, dict) and item.get("type") == "page"
                    ),
                    None,
                ),
            )
            if not isinstance(candidate, dict):
                raise InteractiveAuthError("服务器浏览器没有可用页面")
            target_id = str(candidate.get("targetId") or "").strip()
            if not target_id:
                raise InteractiveAuthError("服务器浏览器页面标识无效")
            attached = await cdp.command(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
            session_token = str(attached.get("sessionId") or "").strip()
            if session_token:
                await cdp.command(
                    "Page.navigate",
                    {"url": url},
                    session_id=session_token,
                )
            return target_id

    async def start(self, config: dict[str, Any]) -> dict[str, Any]:
        cdp_url, browser_url, gateway_url, ttl, poll = self._settings(config)
        if not cdp_url or not browser_url:
            raise InteractiveAuthError(
                "未配置服务器浏览器。请设置 LM_BRIDGE_BROWSER_CDP_URL 和 LM_BRIDGE_BROWSER_VNC_URL。"
            )

        now = time.time()
        async with self._lock:
            active = next(
                (
                    session
                    for session in reversed(list(self._sessions.values()))
                    if session.expires_at > now
                    and session.status in {"starting", "waiting"}
                ),
                None,
            )
            if active is not None:
                return self._public_result(active)

        session_id = secrets.token_urlsafe(18)
        target_id = await self._create_browser_target(cdp_url, session_id)
        link_url = _build_interactive_gateway_url(
            gateway_url,
            expires_at=now + ttl,
            secret=_interactive_link_secret(),
        )
        session = _AuthSession(
            session_id=session_id,
            target_id=target_id,
            browser_url=link_url or browser_url,
            created_at=now,
            expires_at=now + ttl,
            status="waiting",
        )
        async with self._lock:
            self._sessions[session_id] = session
        session.monitor_task = asyncio.create_task(self._monitor(session_id, poll))
        session.monitor_task.add_done_callback(self._consume_task)
        return self._public_result(session)

    @staticmethod
    def _consume_task(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            # The public status endpoint carries the useful failure state.
            pass

    async def _target_snapshot(
        self,
        cdp_url: str,
        target_id: str,
    ) -> tuple[str, str, str, str, str, list[dict[str, str]]]:
        async with _CDPWebSocket(cdp_url) as cdp:
            targets = await cdp.command("Target.getTargets")
            infos = targets.get("targetInfos")
            target: dict[str, Any] | None = None
            if isinstance(infos, list):
                for item in infos:
                    if isinstance(item, dict) and str(item.get("targetId") or "") == target_id:
                        target = item
                        break
                if target is None:
                    target = next(
                        (
                            item
                            for item in infos
                            if isinstance(item, dict)
                            and item.get("type") == "page"
                            and "arena.ai" in str(item.get("url") or "")
                        ),
                        None,
                    )
            if not isinstance(target, dict):
                raise InteractiveAuthError("验证页面已关闭")
            current_target_id = str(target.get("targetId") or target_id)
            attached = await cdp.command(
                "Target.attachToTarget",
                {"targetId": current_target_id, "flatten": True},
            )
            session_token = str(attached.get("sessionId") or "").strip()
            if not session_token:
                raise InteractiveAuthError("无法连接验证页面")

            page_data: dict[str, Any] = {}
            try:
                evaluated = await cdp.command(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "({title: document.title, url: location.href, "
                            "ua: navigator.userAgent, storage: (() => {"
                            "try { const s = window.localStorage; const o = {};"
                            "for (let i = 0; i < s.length; i++) {"
                            "const k = s.key(i); if (!k) continue;"
                            "if (/(auth|session|supabase|sb-|provisional)/i.test(k)) "
                            "o[k] = String(s.getItem(k) || '');"
                            "} return o; } catch (e) { return {}; }"
                            "})(), bodyText: (document.body && "
                            "String(document.body.innerText || '').slice(0, 12000) || '')})"
                        ),
                        "returnByValue": True,
                    },
                    session_id=session_token,
                )
                result = evaluated.get("result")
                if isinstance(result, dict) and isinstance(result.get("value"), dict):
                    page_data = result["value"]
            except CDPError:
                page_data = {}

            cookies_result: dict[str, Any]
            try:
                cookies_result = await cdp.command(
                    "Network.getAllCookies",
                    session_id=session_token,
                )
            except CDPError:
                cookies_result = await cdp.command(
                    "Storage.getCookies",
                    session_id=session_token,
                )
            cookies = _safe_cookie_records(cookies_result.get("cookies"))
            local_storage_cookie = _local_storage_arena_auth_cookie(
                page_data.get("storage")
            )
            if local_storage_cookie and not any(
                item.get("name") == "arena-auth-prod-v1" for item in cookies
            ):
                cookies.append(
                    {
                        "name": "arena-auth-prod-v1",
                        "value": local_storage_cookie,
                    }
                )
            return (
                current_target_id,
                str(page_data.get("title") or target.get("title") or ""),
                str(page_data.get("url") or target.get("url") or ""),
                str(page_data.get("ua") or ""),
                str(page_data.get("bodyText") or ""),
                cookies,
            )

    async def _persist_cookies(
        self,
        cookies: list[dict[str, str]],
        user_agent: str = "",
    ) -> tuple[bool, bool]:
        names = {str(item.get("name") or "") for item in cookies}
        has_cf = "cf_clearance" in names
        cookie_values = {
            str(item.get("name") or ""): str(item.get("value") or "")
            for item in cookies
            if isinstance(item, dict)
        }
        has_auth = _has_valid_persisted_arena_auth(
            {"browser_cookies": cookie_values}
        )
        if not cookies:
            return has_cf, has_auth

        async with self._persist_lock:
            try:
                from . import main as bridge_main

                # Keep the live browser cookie available to the request path
                # immediately.  Previously the interactive status checker
                # wrote config.json but did not update the in-memory token,
                # so the next image request could still select an expired
                # auth_tokens entry.
                bridge_main._capture_ephemeral_arena_auth_token_from_cookies(cookies)
                config = bridge_main.get_config()
                if bridge_main._upsert_browser_session_into_config(
                    config,
                    cookies,
                    user_agent=user_agent or None,
                ):
                    bridge_main.save_config(config)
            except Exception:
                # Persisting is best-effort; the browser profile remains
                # usable even if a transient config write fails.
                pass
        return has_cf, has_auth

    async def sync_live_browser_session(self, config: dict[str, Any]) -> dict[str, bool]:
        """Synchronize the current server-browser session before an upstream request.

        The visible Arena page can remain logged in while config.json still
        contains an older, revoked auth cookie.  Read the live browser tab
        without creating a new tab, persist the current cookies, and return
        the same precise flags used by the interactive verification status.
        """
        cdp_url, _, _, _, _ = self._settings(config)
        if not cdp_url:
            return {
                "has_cf_clearance": False,
                "has_arena_auth": False,
                "has_logged_in": False,
                "verified": False,
            }
        try:
            _, title, url, user_agent, body_text, cookies = await self._target_snapshot(
                cdp_url,
                "",
            )
            await self._persist_cookies(cookies, user_agent)
            _, _, logged_in, verified = _live_session_is_verified(
                cookies,
                title=title,
                url=url,
                body_text=body_text,
            )
            return {
                "has_cf_clearance": bool(
                    any(
                        str(item.get("name") or "") == "cf_clearance"
                        and str(item.get("value") or "").strip()
                        for item in cookies
                    )
                ),
                "has_arena_auth": bool(
                    any(
                        str(item.get("name") or "")
                        in {"arena-auth-prod-v1", "arena-auth-prod-v1.0"}
                        and str(item.get("value") or "").strip()
                        for item in cookies
                    )
                ),
                "has_logged_in": bool(logged_in),
                "verified": bool(verified),
            }
        except Exception:
            return {
                "has_cf_clearance": False,
                "has_arena_auth": False,
                "has_logged_in": False,
                "verified": False,
            }

    async def _maybe_refresh_persisted_auth(self, config: dict[str, Any]) -> bool:
        """Try the non-interactive refresh path before asking the operator to log in."""
        if _has_valid_persisted_arena_auth(config):
            return True
        now = time.monotonic()
        if now - self._last_refresh_attempt_at < 60.0:
            return False
        async with self._refresh_lock:
            now = time.monotonic()
            if now - self._last_refresh_attempt_at < 60.0:
                return _has_valid_persisted_arena_auth(self._load_config())
            self._last_refresh_attempt_at = now
            try:
                from .auth import maybe_refresh_expired_auth_tokens

                refreshed = await maybe_refresh_expired_auth_tokens()
            except Exception:
                refreshed = None
            return bool(refreshed) or _has_valid_persisted_arena_auth(self._load_config())

    async def check(self, session_id: str, config: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            session = self._sessions.get(str(session_id))
        if session is None:
            raise InteractiveAuthError("验证会话不存在或已过期")

        now = time.time()
        if now >= session.expires_at:
            session.status = "expired"
            return self._public_result(session)

        cdp_url, _, _, _, _ = self._settings(config)
        try:
            target_id, title, url, user_agent, body_text, cookies = await self._target_snapshot(
                cdp_url,
                session.target_id,
            )
            session.target_id = target_id
            has_cf, has_auth = await self._persist_cookies(cookies, user_agent)
            live_cf, live_auth, logged_in, verified = _live_session_is_verified(
                cookies,
                title=title,
                url=url,
                body_text=body_text,
            )
            session.has_cf_clearance = live_cf
            session.has_arena_auth = live_auth
            session.has_logged_in = logged_in
            session.status = "verified" if verified else "waiting"
            session.last_error = ""
        except Exception as exc:
            session.status = "waiting"
            session.last_error = type(exc).__name__
        return self._public_result(session)

    async def _monitor(self, session_id: str, poll: float) -> None:
        while True:
            async with self._lock:
                session = self._sessions.get(session_id)
            if session is None or session.status in {"verified", "expired", "error"}:
                return
            if time.time() >= session.expires_at:
                session.status = "expired"
                return
            try:
                await self.check(session_id, self._load_config())
            except Exception as exc:
                session.status = "waiting"
                session.last_error = type(exc).__name__
            await asyncio.sleep(poll)

    @staticmethod
    def _load_config() -> dict[str, Any]:
        try:
            from . import main as bridge_main

            return bridge_main.get_config()
        except Exception:
            return {}

    async def status(self, session_id: str, config: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self.check(session_id, config)
        except InteractiveAuthError as exc:
            # A bridge restart clears in-memory sessions while AstrBot may
            # still hold the previous id.  Reuse the newest active session
            # when possible; otherwise return an expired marker so clients
            # can start a fresh session instead of surfacing a dead id.
            if str(exc) != "验证会话不存在或已过期":
                raise
            try:
                return await self.latest(config)
            except InteractiveAuthError:
                return {
                    "session_id": str(session_id),
                    "status": "expired",
                    "verified": False,
                    "browser_url": "",
                    "expires_at": int(time.time()),
                    "has_cf_clearance": False,
                    "has_arena_auth": False,
                    "has_logged_in": False,
                    "message": "验证会话已结束，请重新运行 /竞技场验证。",
                }

    async def latest(self, config: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        async with self._lock:
            candidates = list(self._sessions.values())
        candidates = [session for session in candidates if session.expires_at > now]
        if not candidates:
            raise InteractiveAuthError("当前没有验证会话")
        session = max(candidates, key=lambda item: item.created_at)
        return await self.check(session.session_id, config)

    async def close(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            task = session.monitor_task
            if task is not None and not task.done():
                task.cancel()
        if sessions:
            await asyncio.gather(
                *[
                    session.monitor_task
                    for session in sessions
                    if session.monitor_task is not None
                ],
                return_exceptions=True,
            )
