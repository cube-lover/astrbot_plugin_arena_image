"""Direct Chrome-DevTools transport: the plugin drives the server browser itself.

``ArenaBridgeClient`` speaks HTTP to the ``arena-bridge`` container.  This module
exposes the *same* Python interface but talks straight to the ``arena-browser``
container's CDP proxy, which makes the bridge container optional.

Everything happens inside the Arena tab that is already signed in:

* the model table is read from the page's own ``initialModels`` payload;
* the reCAPTCHA token is minted by the page's own ``grecaptcha.enterprise``;
* ``create-evaluation`` is POSTed by the page with ``credentials: 'include'``.

That last point is why this path is *more* robust than copying cookies into a
config file.  Arena rotates ``__cf_bm`` roughly every 30 minutes and the
Supabase auth cookie roughly every hour, so a copied cookie is stale almost as
soon as it is taken, whereas the live page always sends whatever is current.

Only the standard library plus ``httpx`` (already a plugin dependency) is used:
no websocket package, no Playwright, no camoufox.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import mimetypes
import re
import secrets
import struct
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from .bridge_client import BridgeError, decode_image_value

ARENA_ORIGIN = "https://arena.ai"
ARENA_PAGE_PATH = "/text/direct"
STREAM_CREATE_EVALUATION_PATH = "/nextjs-api/stream/create-evaluation"

# The signing key for verification links lives *inside* the browser container:
# docker-compose mounts ``.interactive-link-secret`` there as a read-only secret
# and the noVNC gateway reads the same file.  The plugin can therefore read it
# through CDP instead of asking the operator to copy it into the config, which
# keeps the two sides in sync by construction — a mistyped or rotated key was
# the only way a link could come out unopenable.
BROWSER_LINK_SECRET_FILES = (
    "file:///run/secrets/interactive_link_secret",
    "file:///run/secrets/novnc_gateway_secret",
)
# Same idea for the *public* address of the gateway, which the plugin cannot
# guess: it is not in AstrBot's config and not derivable from the container-name
# CDP URL.  ``setup.sh`` drops it into the browser's state directory so direct
# mode needs no plugin configuration at all on a standard deployment.
BROWSER_GATEWAY_URL_FILES = (
    "file:///data/browser/gateway-url.txt",
    "file:///data/browser/arena-gateway-url.txt",
)
GATEWAY_DEFAULT_PORT = 6081
LINK_SECRET_CACHE_SECONDS = 3600.0
_LINK_SECRET_RE = re.compile(r"^[0-9A-Za-z._\-]{16,256}$")

# ``arena-browser`` only resolves for containers sharing the compose network.
# AstrBot is often installed separately -- another compose project, another
# network, or straight onto the host -- and then the container name resolves to
# nothing.  Rather than making that a configuration question, an unreachable
# endpoint is retried against the addresses the same box would answer on.
# Probed only after the configured address has already failed, so a normal
# deployment never pays for this.
CDP_FALLBACK_HOSTS = ("host.docker.internal", "172.17.0.1", "127.0.0.1")
CDP_PROBE_TIMEOUT = 4.0
CDP_ENDPOINT_CACHE_SECONDS = 600.0
_CDP_ENDPOINTS: dict[str, tuple[str, float]] = {}

# Printed instead of "is arena-browser running?" when nothing answers: it works
# out the network name itself, attaches the running AstrBot container to it, and
# needs no restart, so a beginner can paste it without reading anything first.
CDP_NETWORK_HINT = (
    "docker network connect "
    "$(docker inspect arena-browser "
    "--format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "
    "| awk '{print $1}') astrbot"
)

# Chrome splits a cookie larger than ~4 KB into ``<name>.0``, ``<name>.1`` ...
# The bare name is then *absent* from the browser, so any exact-name lookup
# reports "not logged in", the request is signed with the ``sign_up`` reCAPTCHA
# action, Arena answers 403, and the operator is told their cookie expired.
# Every auth-cookie read in this module therefore goes through
# ``combine_auth_cookie``.
AUTH_COOKIE_NAME = "arena-auth-prod-v1"
_AUTH_CHUNK_RE = re.compile(r"^arena-auth-prod-v1\.(\d+)$")

# Arena hides a gray-test checkpoint by shipping its row without an
# ``organization``.  Those rows generate images perfectly well, so they are
# listed unless the deployment turns them off.
DEFAULT_ALLOWED_STEALTH_MODELS = frozenset({"luna-lisa-alpha"})

# The page HTML is ~540 KB, so the model table is cached rather than re-read on
# every command.  Arena adds rows over hours, never seconds.
MODEL_CACHE_SECONDS = 300.0
ACTION_CACHE_SECONDS = 3600.0
UPLOAD_CACHE_LIMIT = 200

# Same windows the bridge used, so `/竞技场画图模型` keeps annotating identically.
MODEL_HEALTH_TTL_SECONDS = 6 * 60 * 60
MODEL_VARIANT_FAILURE_TTL_SECONDS = 10 * 60

# How long to let the page rotate a stale access token before giving up.  The
# rotation is a single same-origin GET plus Supabase's own timer, so it lands in
# well under a second when it lands at all; the budget only covers a slow page.
SESSION_REFRESH_BUDGET_SECONDS = 8.0
SESSION_REFRESH_POLL_SECONDS = 0.6

_NEXT_ACTION_RE = (
    r'\(0,[a-zA-Z_$][\w$]*\.createServerReference\)\(["\']([\w\d]*?)["\'],'
    r'[a-zA-Z_$][\w$]*\.callServer,void 0,[a-zA-Z_$][\w$]*\.findSourceMapURL,'
    r'["\'](\w+)["\']\)'
)


class CDPError(RuntimeError):
    """A Chrome DevTools Protocol command or transport failure."""


def _err(
    message: str,
    *,
    code: str = "",
    status_code: int | None = None,
    retry_after: float | None = None,
    browser_url: str = "",
) -> BridgeError:
    """Build a ``BridgeError`` whose ``.code`` drives the plugin's hint text."""
    error: dict[str, Any] = {"message": message}
    if code:
        error["code"] = code
    if browser_url:
        error["browser_url"] = browser_url
    return BridgeError(
        message,
        status_code=status_code,
        payload={"error": error},
        retry_after=retry_after,
    )


# --- Arena session cookie decoding (ported from the bridge's auth module) -----
#
# The plugin never stores this value; it only classifies the live cookie so
# `/竞技场验证状态` can tell "not logged in" apart from "logged in but Arena
# refused the request", and so the reCAPTCHA action is derived correctly.


def _base64_payload_variants(value: str):
    """Yield plausible decodings of a base64 body, standard *and* URL-safe.

    ``base64.b64decode`` without ``validate=True`` silently *discards* ``-`` and
    ``_`` instead of failing, so a base64url cookie decodes into garbage rather
    than raising.  Trying both alphabets and letting the JSON parse decide keeps
    that failure impossible.
    """
    cleaned = "".join(str(value or "").split())
    if not cleaned:
        return
    seen: set[str] = set()
    for text in (cleaned, cleaned.replace("-", "+").replace("_", "/")):
        padded = text + "=" * ((4 - (len(text) % 4)) % 4)
        if padded in seen:
            continue
        seen.add(padded)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                raw = decoder(padded.encode("utf-8"))
            except Exception:
                continue
            if raw:
                yield raw


def _decode_session_envelope(token: str) -> dict[str, Any] | None:
    """Decode the ``base64-<json>`` Supabase session envelope Arena stores."""
    text = str(token or "").strip()
    if not text.startswith("base64-"):
        return None
    body = text[len("base64-") :]
    if not body:
        return None
    for raw in _base64_payload_variants(body):
        try:
            value = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    return None


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    text = str(token or "").strip()
    if text.count(".") < 2:
        return None
    parts = text.split(".")
    body = str(parts[1] or "")
    if not body:
        return None
    try:
        body += "=" * ((4 - (len(body) % 4)) % 4)
        value = json.loads(base64.urlsafe_b64decode(body.encode("utf-8")).decode("utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def session_expiry_epoch(token: str) -> int | None:
    """Unix seconds when the session expires, or ``None`` when unknown."""
    envelope = _decode_session_envelope(token)
    if isinstance(envelope, dict):
        try:
            expires_at = envelope.get("expires_at")
            if expires_at is not None:
                return int(expires_at)
        except Exception:
            pass
        access = str(envelope.get("access_token") or "").strip()
        payload = _decode_jwt_payload(access) if access else None
        if isinstance(payload, dict) and payload.get("exp") is not None:
            try:
                return int(payload["exp"])
            except Exception:
                pass
    payload = _decode_jwt_payload(token)
    if isinstance(payload, dict) and payload.get("exp") is not None:
        try:
            return int(payload["exp"])
        except Exception:
            return None
    return None


def session_is_expired(token: str, *, skew_seconds: int = 30) -> bool:
    """``True`` only when expiry is *known* and past; opaque formats say no."""
    expiry = session_expiry_epoch(token)
    if expiry is None:
        return False
    return time.time() >= float(expiry) - float(max(0, skew_seconds))


def session_is_plausible(token: str) -> bool:
    """Whether the value looks like a live Arena session at all."""
    text = str(token or "").strip()
    if not text:
        return False
    if text.startswith("base64-"):
        envelope = _decode_session_envelope(text)
        if not isinstance(envelope, dict):
            return False
        if str(envelope.get("access_token") or "").count(".") < 2:
            return False
        return not session_is_expired(text)
    if text.count(".") >= 2:
        return len(text) >= 100 and not session_is_expired(text)
    return False


def _flag_is_true(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and int(value) == 1:
        return True
    return str(value or "").strip().casefold() in {"true", "1", "yes"}


def session_is_logged_in(token: str) -> bool:
    """``True`` only for a non-anonymous session that can pass Arena's login gate."""
    if not session_is_plausible(token):
        return False
    envelope = _decode_session_envelope(token) or {}
    if not isinstance(envelope, dict):
        envelope = {}
    access = str(envelope.get("access_token") or "").strip()
    jwt = (_decode_jwt_payload(access) if access else None) or _decode_jwt_payload(token) or {}
    if not isinstance(jwt, dict):
        jwt = {}
    user = envelope.get("user") if isinstance(envelope.get("user"), dict) else {}
    supabase_user = (
        envelope.get("supabaseUser")
        if isinstance(envelope.get("supabaseUser"), dict)
        else {}
    )
    app_meta = jwt.get("app_metadata") if isinstance(jwt.get("app_metadata"), dict) else {}
    if any(
        _flag_is_true(value)
        for value in (
            jwt.get("is_anonymous"),
            envelope.get("is_anonymous"),
            user.get("is_anonymous"),
            user.get("isAnonymous"),
            supabase_user.get("is_anonymous"),
        )
    ):
        return False
    if jwt.get("is_anonymous") is False:
        return True
    for item in (
        jwt.get("email"),
        user.get("email"),
        supabase_user.get("email"),
        user.get("emailProvider"),
        app_meta.get("provider"),
        app_meta.get("providers"),
    ):
        if isinstance(item, list) and any(str(part or "").strip() for part in item):
            return True
        if str(item or "").strip():
            return True
    return False


def session_can_refresh(token: str) -> bool:
    """Whether an expired session is merely stale rather than logged out.

    Arena's access token lives about an hour; the refresh token beside it lives
    for weeks.  A browser left idle overnight therefore holds an *expired*
    access token belonging to a *still valid* login, and the page rotates it by
    itself on the next request.  Treating that as "Cookie 失效，请重新绑定" is
    the single most annoying way this plugin can be wrong, so the recoverable
    case is detected explicitly instead of being lumped in with a real logout.
    """
    envelope = _decode_session_envelope(token)
    if not isinstance(envelope, dict):
        return False
    if not str(envelope.get("refresh_token") or "").strip():
        return False
    access = str(envelope.get("access_token") or "").strip()
    if access.count(".") < 2:
        return False
    jwt = _decode_jwt_payload(access) or {}
    user = envelope.get("user") if isinstance(envelope.get("user"), dict) else {}
    if _flag_is_true(jwt.get("is_anonymous")) or _flag_is_true(user.get("is_anonymous")):
        return False
    # An anonymous Supabase session also carries a refresh token, so the login
    # itself has to be evidenced: a role or an address, not just the shape.
    if str(jwt.get("role") or "").strip().casefold() == "authenticated":
        return True
    return bool(str(jwt.get("email") or user.get("email") or "").strip())


def combine_auth_cookie(cookies: Any) -> str:
    """Rebuild ``arena-auth-prod-v1`` from however Chrome happens to store it.

    The value is ~4.6 KB, above Chrome's per-cookie limit, so the live browser
    holds ``arena-auth-prod-v1.0`` and ``arena-auth-prod-v1.1`` and the bare name
    does not exist.  Chunks are joined in numeric index order -- string sorting
    would corrupt the value once a tenth chunk appears.  The ``base64-`` prefix
    only ever appears on the first chunk, so it is re-added when missing.
    """
    if not isinstance(cookies, list):
        return ""
    whole = ""
    chunks: dict[int, str] = {}
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if not value:
            continue
        if name == AUTH_COOKIE_NAME:
            whole = value
            continue
        match = _AUTH_CHUNK_RE.match(name)
        if match:
            chunks[int(match.group(1))] = value
    token = whole or "".join(chunks[index] for index in sorted(chunks))
    if token and not token.startswith("base64-"):
        token = f"base64-{token}"
    return token


def cookie_value(cookies: Any, name: str) -> str:
    if not isinstance(cookies, list):
        return ""
    for item in cookies:
        if isinstance(item, dict) and str(item.get("name") or "") == name:
            return str(item.get("value") or "").strip()
    return ""


def cookie_expiry(cookies: Any, name: str) -> float:
    if not isinstance(cookies, list):
        return 0.0
    for item in cookies:
        if isinstance(item, dict) and str(item.get("name") or "") == name:
            try:
                return float(item.get("expires") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


# --- identifiers -------------------------------------------------------------


def new_uuid7() -> str:
    """A UUIDv7: 48-bit unix-ms prefix, then version/variant bits, then random.

    Arena's own client generates message ids this way and derives model row
    creation dates from the same prefix, so keeping the format matters.
    """
    millis = int(time.time() * 1000)
    raw = bytearray(millis.to_bytes(6, "big") + secrets.token_bytes(10))
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def uuid7_created_at(value: Any) -> int | None:
    """Unix seconds encoded in a UUIDv7, or ``None`` for older UUIDv4 rows."""
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.version != 7:
        return None
    millis = int(parsed.hex[:12], 16)
    return millis // 1000 if millis else None


# --- interactive verification link ------------------------------------------


def build_interactive_link(gateway_url: str, *, expires_at: float, secret: str) -> str:
    """Sign the short-lived noVNC gateway link the operator opens.

    The gateway validates ``hmac_sha256(secret, "<expiry>.<nonce>")``; the VNC
    password stays server-side and never reaches the chat message.
    """
    base = str(gateway_url or "").strip().rstrip("/")
    key = str(secret or "").strip()
    if not base or not key:
        return ""
    expiry = str(max(0, int(expires_at)))
    nonce = secrets.token_urlsafe(24)
    payload = f"{expiry}.{nonce}"
    signature = hmac.new(key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{base}/v/{quote(f'{payload}.{signature}', safe='')}"


def gateway_base_from(*candidates: str) -> str:
    """Best gateway origin among the addresses the operator already gave us.

    The gateway is published on :data:`GATEWAY_DEFAULT_PORT` of the same host
    that serves noVNC, so a configured ``browser_vnc_url`` is enough to derive
    it and the operator does not have to type a second address.
    """
    for candidate in candidates:
        text = str(candidate or "").strip().rstrip("/")
        if not text:
            continue
        parts = urlsplit(text if "//" in text else f"http://{text}")
        netloc = parts.netloc
        if not netloc:
            continue
        try:
            port = parts.port
        except ValueError:
            port = None
        if port == GATEWAY_DEFAULT_PORT and parts.path:
            # Already a gateway URL, keep whatever path prefix it carries.
            return text
        # Case is preserved on purpose: ``urlsplit().hostname`` lowercases, and
        # the operator's own text is what has to match their reverse proxy.
        host = netloc.rsplit("@", 1)[-1]
        host = host[: host.index("]") + 1] if host.startswith("[") and "]" in host else host.split(":", 1)[0]
        if not host:
            continue
        return f"{parts.scheme or 'http'}://{host}:{GATEWAY_DEFAULT_PORT}"
    return ""


def looks_like_link_secret(value: str) -> bool:
    """Reject HTML, error pages and empty reads before they become a bad link."""
    text = str(value or "").strip()
    return bool(_LINK_SECRET_RE.fullmatch(text))


def cdp_fallback_endpoints(endpoint: str) -> list[str]:
    """Addresses to try when ``endpoint`` itself does not answer.

    Keeps the configured scheme, port and path and only swaps the host, so a
    deployment that moved the CDP proxy to another port still gets sensible
    candidates.  Hosts that already look like an address rather than a container
    name are left alone: if the operator typed an IP and it is down, guessing a
    different machine would be wrong.
    """
    text = str(endpoint or "").strip().rstrip("/")
    if not text:
        return []
    parts = urlsplit(text if "//" in text else f"http://{text}")
    host = (parts.hostname or "").strip()
    if not host or host in CDP_FALLBACK_HOSTS:
        return []
    # A dotted or bracketed address is a deliberate choice, not a container name.
    if host.replace(".", "").isdigit() or ":" in host or host.startswith("["):
        return []
    port = f":{parts.port}" if parts.port else ""
    tail = parts.path.rstrip("/")
    scheme = parts.scheme or "http"
    return [f"{scheme}://{candidate}{port}{tail}" for candidate in CDP_FALLBACK_HOSTS]


async def probe_cdp_endpoint(endpoint: str, *, timeout: float = CDP_PROBE_TIMEOUT) -> dict[str, Any]:
    """Return ``/json/version`` for ``endpoint``, or ``{}`` if it does not answer."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{endpoint}/json/version", headers={"Accept": "application/json"}
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


# --- CDP transport -----------------------------------------------------------


class CDPWebSocket:
    """Minimal async RFC 6455 client for the browser's CDP proxy.

    The proxy in front of the server browser does not complete Playwright's
    websocket handshake reliably, and pulling in a websocket library for four
    CDP calls is not worth a dependency, so the frames are built by hand.
    """

    def __init__(self, endpoint: str, *, timeout: float = 30.0) -> None:
        self.endpoint = str(endpoint or "").strip().rstrip("/")
        self.timeout = max(5.0, float(timeout))
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 0

    async def __aenter__(self) -> CDPWebSocket:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _websocket_url(self) -> str:
        parts = urlsplit(self.endpoint)
        if parts.scheme in {"ws", "wss"}:
            return self.endpoint
        if parts.scheme not in {"http", "https"}:
            raise _err("服务器浏览器 CDP 地址格式无效", code="browser_unavailable")

        configured = self.endpoint
        # A previous command already found which address answers on this box.
        cached, found_at = _CDP_ENDPOINTS.get(configured, ("", 0.0))
        if cached and time.time() - found_at < CDP_ENDPOINT_CACHE_SECONDS:
            self.endpoint = cached

        version = await probe_cdp_endpoint(self.endpoint, timeout=min(30.0, self.timeout))
        if not version:
            # The configured address goes first when a cached one was tried and
            # has since gone stale: a container rename or a network change must
            # not pin the plugin to an address that no longer answers.
            candidates = [configured] if self.endpoint != configured else []
            candidates += [
                candidate
                for candidate in cdp_fallback_endpoints(configured)
                if candidate != self.endpoint
            ]
            for candidate in candidates:
                version = await probe_cdp_endpoint(candidate)
                if version:
                    self.endpoint = candidate
                    if candidate == configured:
                        _CDP_ENDPOINTS.pop(configured, None)
                    else:
                        _CDP_ENDPOINTS[configured] = (candidate, time.time())
                    break
        if not version:
            _CDP_ENDPOINTS.pop(configured, None)
            raise _err(
                "连不上服务器浏览器（arena-browser 没启动，或者 AstrBot 和它不在同一个"
                " Docker 网络）。先看容器在不在：docker ps | grep arena-browser；"
                "在的话把 AstrBot 接进同一个网络，不用重启任何容器：\n"
                f"{CDP_NETWORK_HINT}",
                code="browser_unavailable",
            )
        url = str(version.get("webSocketDebuggerUrl") or "").strip()
        if not url:
            raise _err("服务器浏览器未返回 CDP WebSocket 地址", code="browser_unavailable")
        return url

    async def connect(self) -> None:
        if not self.endpoint:
            raise _err("未配置服务器浏览器 CDP 地址", code="browser_unavailable")
        websocket_url = await self._websocket_url()
        parts = urlsplit(websocket_url)
        host = parts.hostname
        if not host:
            raise _err("服务器浏览器 CDP 主机地址无效", code="browser_unavailable")
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

        # A proxy that forwards /json/version verbatim reports Chrome's own
        # loopback address; the configured endpoint is the reachable one.
        endpoint_parts = urlsplit(self.endpoint)
        if host in {"127.0.0.1", "localhost"} and endpoint_parts.hostname:
            host = endpoint_parts.hostname
            port = endpoint_parts.port or port

        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=True if parts.scheme == "wss" else None),
                timeout=min(30.0, self.timeout),
            )
        except Exception as exc:
            raise _err("连接服务器浏览器 CDP 失败", code="browser_unavailable") from exc

        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        authority = f"{host}:{port}"
        scheme = "https" if parts.scheme == "wss" else "http"
        self._writer.write(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {authority}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Origin: {scheme}://{authority}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        await self._writer.drain()
        try:
            head = await asyncio.wait_for(
                self._reader.readuntil(b"\r\n\r\n"),
                timeout=min(30.0, self.timeout),
            )
        except Exception as exc:
            await self.close()
            raise _err("服务器浏览器 CDP 握手超时", code="browser_unavailable") from exc
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            await self.close()
            raise _err("服务器浏览器 CDP 握手被拒绝", code="browser_unavailable")

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        try:
            await self._send_frame(0x8, b"", writer=writer)
        except Exception:
            pass
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def _send_frame(
        self,
        opcode: int,
        payload: bytes,
        *,
        writer: asyncio.StreamWriter | None = None,
    ) -> None:
        target = writer or self._writer
        if target is None:
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
        target.write(
            bytes(header) + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        )
        await target.drain()

    async def _read_exactly(self, size: int) -> bytes:
        if self._reader is None:
            raise CDPError("CDP 连接未建立")
        return await self._reader.readexactly(size)

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        first, second = await self._read_exactly(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self._read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._read_exactly(8))[0]
        mask = await self._read_exactly(4) if second & 0x80 else b""
        payload = await self._read_exactly(length) if length else b""
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    async def _read_message(self, timeout: float) -> dict[str, Any]:
        chunks: list[bytes] = []
        started = False
        while True:
            fin, opcode, payload = await asyncio.wait_for(self._read_frame(), timeout=timeout)
            if opcode == 0x8:
                raise CDPError("服务器浏览器 CDP 连接已关闭")
            if opcode == 0x9:
                await self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x2}:
                started, chunks = True, [payload]
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
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one CDP command and return its result.

        ``timeout`` is per-command on purpose: reading the model table takes a
        second while an image generation holds the socket for a minute or more.
        """
        self._next_id += 1
        command_id = self._next_id
        message: dict[str, Any] = {"id": command_id, "method": str(method)}
        if params:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        await self._send_frame(0x1, json.dumps(message, separators=(",", ":")).encode("utf-8"))
        deadline = max(5.0, float(timeout if timeout is not None else self.timeout))
        while True:
            try:
                value = await self._read_message(deadline)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise CDPError(f"服务器浏览器响应超时（{method} 超过 {deadline:.0f} 秒）") from exc
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                raise CDPError(f"服务器浏览器连接中断：{type(exc).__name__}") from exc
            if value.get("id") != command_id:
                continue
            error = value.get("error")
            if error:
                raise CDPError(
                    str(error.get("message") if isinstance(error, dict) else error)
                )
            result = value.get("result")
            return result if isinstance(result, dict) else {}

    async def evaluate(
        self,
        expression: str,
        *,
        session_id: str,
        timeout: float | None = None,
    ) -> Any:
        """Run one expression in the page and return its value."""
        result = await self.command(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            session_id=session_id,
            timeout=timeout,
        )
        details = result.get("exceptionDetails")
        if details:
            raise CDPError(f"页面脚本执行失败：{str(details)[:200]}")
        return (result.get("result") or {}).get("value")


# --- model table -------------------------------------------------------------


def model_capability(model: Any, section: str, capability: str) -> bool:
    """Read one capability flag defensively across Arena's schema variants."""
    capabilities = model.get("capabilities") if isinstance(model, dict) else None
    if not isinstance(capabilities, dict) and isinstance(model, dict) and any(
        key in model
        for key in (
            "outputCapabilities",
            "inputCapabilities",
            "output_capabilities",
            "input_capabilities",
        )
    ):
        capabilities = model
    if not isinstance(capabilities, dict):
        return False
    values = capabilities.get(section)
    if not isinstance(values, dict):
        alternate = {
            "outputCapabilities": "output_capabilities",
            "inputCapabilities": "input_capabilities",
        }.get(section)
        values = capabilities.get(alternate) if alternate else None
    if not isinstance(values, dict):
        return False
    value = values.get(capability)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def model_has_supported_output(model: Any) -> bool:
    return any(
        model_capability(model, "outputCapabilities", capability)
        for capability in ("text", "search", "image")
    )


def public_model_entry(model: dict[str, Any]) -> dict[str, Any]:
    """Shape one Arena row exactly like the bridge's ``/api/v1/models`` entry."""
    created = uuid7_created_at(model.get("id"))
    return {
        "id": model.get("publicName"),
        "object": "model",
        "created": created or int(time.time()),
        "created_at": created,
        "owned_by": model.get("organization") or "lmarena",
        "capabilities": (
            model.get("capabilities") if isinstance(model.get("capabilities"), dict) else {}
        ),
        "output_image": model_capability(model, "outputCapabilities", "image"),
        "input_image": model_capability(model, "inputCapabilities", "image"),
    }


def parse_model_table(raw: str) -> list[dict[str, Any]]:
    """Parse the ``initialModels`` JSON text lifted out of the page."""
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # Older builds embed the array still escaped for a JS string literal.
        try:
            value = json.loads(text.encode("utf-8", "surrogatepass").decode("unicode_escape"))
        except Exception:
            return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


# --- in-page JavaScript ------------------------------------------------------
#
# Each expression is a self-contained async IIFE that resolves to a plain value
# so `Runtime.evaluate` can return it by value.  They never throw: a rejected
# promise would surface as an opaque CDP exception instead of a diagnosable
# status, so every one of them catches and reports.

_MODEL_TABLE_JS = """
(async () => {
  try {
    const r = await fetch('%(path)s', {credentials: 'include'});
    const t = await r.text();
    const escaped = t.match(/\\\\"initialModels\\\\":([\\s\\S]*?),\\\\"initialModel[A-Z]Id/);
    if (escaped) {
      let json = escaped[1];
      try { json = JSON.parse('"' + json + '"'); } catch (e) {}
      return {ok: true, status: r.status, json: json};
    }
    const plain = t.match(/"initialModels":([\\s\\S]*?),"initialModel[A-Z]Id/);
    if (plain) return {ok: true, status: r.status, json: plain[1]};
    return {ok: false, status: r.status, len: t.length};
  } catch (e) {
    return {ok: false, status: 0, error: String(e)};
  }
})()
"""

_SITEKEY_JS = """
(() => {
  try {
    const s = [...document.scripts].map(x => x.src)
      .find(u => /recaptcha\\/(enterprise|api)\\.js\\?render=/.test(u || ''));
    if (s) return s.split('render=')[1].split('&')[0];
    const m = document.documentElement.innerHTML
      .match(/recaptcha\\/(?:enterprise|api)\\.js\\?render=([0-9A-Za-z_-]{8,200})/);
    return m ? m[1] : '';
  } catch (e) { return ''; }
})()
"""

_SESSION_WAKE_JS = """
(async () => {
  const out = {visible: document.visibilityState, status: 0};
  try {
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
  } catch (e) {}
  try {
    const r = await fetch('%(path)s', {credentials: 'include', cache: 'no-store'});
    out.status = r.status;
    await r.text();
  } catch (e) { out.error = String(e); }
  return out;
})()
"""

_NEXT_ACTION_JS = """
(async () => {
  const found = {};
  let scanned = 0;
  try {
    const pattern = %(pattern)s;
    const sources = [...document.scripts].map(s => s.src).filter(Boolean).slice(0, 120);
    for (const url of sources) {
      try {
        const text = await (await fetch(url)).text();
        scanned++;
        const re = new RegExp(pattern, 'g');
        let m;
        while ((m = re.exec(text)) !== null) { found[m[2]] = m[1]; }
      } catch (e) {}
    }
  } catch (e) {}
  return {scanned: scanned, actions: found};
})()
"""


def _mint_recaptcha_js(sitekey: str, action: str) -> str:
    """Ask the page's own reCAPTCHA client for a token.

    Minting in the page is what keeps this working: the token is bound to the
    site key, the browser fingerprint and the visitor cookie that Arena will see
    on the very next request.
    """
    return (
        "(async () => { try { "
        "if (!window.grecaptcha || !window.grecaptcha.enterprise) return ''; "
        "return await new Promise((resolve) => { "
        "let done = false; "
        "const finish = (v) => { if (!done) { done = true; resolve(v || ''); } }; "
        "setTimeout(() => finish(''), 20000); "
        "window.grecaptcha.enterprise.ready(async () => { try { "
        f"finish(await window.grecaptcha.enterprise.execute({json.dumps(sitekey)}, "
        f"{{action: {json.dumps(action)}}})); "
        "} catch (e) { finish(''); } }); }); "
        "} catch (e) { return ''; } })()"
    )


def _page_fetch_js(
    path: str,
    *,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    body: str | None = None,
) -> str:
    """Perform one same-origin request from inside the signed-in page.

    ``credentials: 'include'`` is the whole point: Chrome attaches the current
    auth cookie, the current ``__cf_bm`` and the current Cloudflare clearance,
    none of which the plugin ever has to see or store.
    """
    request_headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "text/event-stream",
    }
    request_headers.update(headers or {})
    parts = [
        "(async () => { try { const r = await fetch(",
        json.dumps(path),
        ", { method: ",
        json.dumps(str(method or "POST").upper()),
        ", headers: ",
        json.dumps(request_headers),
        ", credentials: 'include'",
    ]
    if body is not None:
        parts.extend([", body: ", json.dumps(body)])
    parts.append(
        " }); const t = await r.text(); const hs = {}; "
        "r.headers.forEach((v, k) => { hs[k] = v; }); "
        "return {status: r.status, headers: hs, text: t}; } "
        "catch (e) { return {status: 0, headers: {}, text: 'FETCH_ERROR:' + String(e)}; } })()"
    )
    return "".join(parts)


# --- shared state ------------------------------------------------------------
#
# ``main.py`` builds a fresh client for every command, so anything worth keeping
# between commands lives here.  One browser per deployment means one cache.

_LOCK = asyncio.Lock()
_MODELS: list[dict[str, Any]] = []
_MODELS_AT = 0.0
_ACTIONS: dict[str, str] = {}
_ACTIONS_AT = 0.0
_UPLOADS: dict[str, dict[str, Any]] = {}
_SESSIONS: dict[str, dict[str, Any]] = {}
# cdp_url -> (secret, read_at).  Read once per hour per browser: the file only
# changes when the operator regenerates it and re-creates the containers.
_LINK_SECRETS: dict[str, tuple[str, float]] = {}
_GATEWAY_URLS: dict[str, tuple[str, float]] = {}
_HEALTH: dict[str, dict[str, Any]] = {"models": {}, "variants": {}}
_HEALTH_PATH: Path | None = None


# --- model health ------------------------------------------------------------
#
# `/竞技场画图模型` annotates each row with the last upstream result, so the
# operator can see "⚠500 3小时前" instead of picking a broken checkpoint again.
# Records survive a plugin reload because they live next to the plugin data.


def _load_health(path: Path) -> None:
    global _HEALTH_PATH
    if _HEALTH_PATH == path and (_HEALTH["models"] or _HEALTH["variants"]):
        return
    _HEALTH_PATH = path
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if not isinstance(value, dict):
        return
    now = time.time()
    for section, ttl in (("models", MODEL_HEALTH_TTL_SECONDS), ("variants", MODEL_VARIANT_FAILURE_TTL_SECONDS)):
        stored = value.get(section)
        if not isinstance(stored, dict):
            continue
        kept: dict[str, Any] = {}
        for key, item in stored.items():
            if not isinstance(item, dict):
                continue
            try:
                checked_at = float(item.get("checked_at") or 0)
            except (TypeError, ValueError):
                continue
            if checked_at > 0 and now - checked_at <= ttl:
                kept[str(key)] = item
        _HEALTH[section] = kept


def _save_health() -> None:
    path = _HEALTH_PATH
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({**_HEALTH, "saved_at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        return


def _moderation_blocked(message: str) -> bool:
    """A refusal is the model working, not the checkpoint being down."""
    lowered = (message or "").casefold()
    return any(
        marker in lowered
        for marker in (
            "content moderation",
            "flagged",
            "safety system",
            "violates",
            "not allowed",
            "policy",
        )
    )


def _record_health(
    public_name: str,
    model_id: str,
    status_code: int | None,
    message: str = "",
) -> None:
    now = time.time()
    healthy = status_code == 200
    if public_name:
        _HEALTH["models"][public_name] = {
            "id": public_name,
            "status_code": int(status_code) if status_code is not None else None,
            "checked_at": now,
            "message": (message or "")[:200],
        }
    if model_id:
        if healthy or _moderation_blocked(message):
            _HEALTH["variants"].pop(model_id, None)
        else:
            _HEALTH["variants"][model_id] = {
                "id": model_id,
                "status_code": int(status_code) if status_code is not None else None,
                "checked_at": now,
                "message": (message or "")[:200],
            }
    _save_health()


def _variant_failed_recently(model_id: str) -> bool:
    record = _HEALTH["variants"].get(model_id)
    if not isinstance(record, dict):
        return False
    try:
        checked_at = float(record.get("checked_at") or 0)
    except (TypeError, ValueError):
        return False
    return checked_at > 0 and time.time() - checked_at <= MODEL_VARIANT_FAILURE_TTL_SECONDS


def _health_snapshot() -> dict[str, Any]:
    """Shape consumed by `main.py::_fetch_model_health` (id/status_code/checked_at)."""
    now = time.time()
    rows: list[dict[str, Any]] = []
    for record in _HEALTH["models"].values():
        if not isinstance(record, dict):
            continue
        try:
            checked_at = float(record.get("checked_at") or 0)
        except (TypeError, ValueError):
            continue
        if checked_at <= 0 or now - checked_at > MODEL_HEALTH_TTL_SECONDS:
            continue
        rows.append(
            {
                "id": record.get("id"),
                "status_code": record.get("status_code"),
                "checked_at": checked_at,
                "message": record.get("message") or "",
            }
        )
    rows.sort(key=lambda item: float(item.get("checked_at") or 0), reverse=True)
    variants = [
        record
        for record in _HEALTH["variants"].values()
        if isinstance(record, dict) and _variant_failed_recently(str(record.get("id") or ""))
    ]
    return {
        "models": rows,
        "variants": variants,
        "ttl_seconds": MODEL_HEALTH_TTL_SECONDS,
        "variant_ttl_seconds": MODEL_VARIANT_FAILURE_TTL_SECONDS,
    }


# --- the attached tab --------------------------------------------------------


class ArenaPage:
    """One attached Arena tab plus the handful of operations the plugin needs."""

    def __init__(
        self,
        cdp: CDPWebSocket,
        session_id: str,
        url: str,
        *,
        target_id: str = "",
    ) -> None:
        self.cdp = cdp
        self.session_id = session_id
        self.url = url
        self.target_id = target_id
        self._cookies: list[dict[str, Any]] | None = None

    async def evaluate(self, expression: str, *, timeout: float | None = None) -> Any:
        return await self.cdp.evaluate(
            expression, session_id=self.session_id, timeout=timeout
        )

    async def cookies(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self._cookies is not None and not refresh:
            return self._cookies
        records: list[dict[str, Any]] = []
        for method, session in (
            ("Network.getAllCookies", self.session_id),
            ("Storage.getCookies", None),
        ):
            try:
                result = await self.cdp.command(method, session_id=session, timeout=20.0)
            except CDPError:
                continue
            value = result.get("cookies")
            if isinstance(value, list) and value:
                records = [item for item in value if isinstance(item, dict)]
                break
        self._cookies = records
        return records

    async def auth_token(self, *, refresh: bool = False) -> str:
        """The live session token, reassembled from Chrome's cookie chunks."""
        return combine_auth_cookie(await self.cookies(refresh=refresh))

    async def refresh_session(
        self,
        *,
        budget: float = SESSION_REFRESH_BUDGET_SECONDS,
    ) -> str:
        """Let the page rotate a stale access token, then re-read the cookie.

        Three nudges, cheapest first, because which one does the work depends on
        the deploy: activating the tab restarts Supabase's own refresh timer
        (it stops while the tab is hidden), the visibility/focus events make an
        already-foreground tab recover immediately, and the same-origin GET goes
        through Arena's SSR middleware, which rotates the cookie server-side.
        None of them touches what the operator sees, and all of them are
        idempotent -- the loop below is what decides whether they worked.
        """
        if self.target_id:
            with contextlib.suppress(CDPError):
                await self.cdp.command(
                    "Target.activateTarget", {"targetId": self.target_id}, timeout=15.0
                )
        with contextlib.suppress(CDPError):
            await self.evaluate(
                _SESSION_WAKE_JS % {"path": ARENA_PAGE_PATH}, timeout=30.0
            )
        deadline = time.monotonic() + max(0.0, float(budget))
        token = combine_auth_cookie(await self.cookies(refresh=True))
        while session_is_expired(token) and time.monotonic() < deadline:
            await asyncio.sleep(SESSION_REFRESH_POLL_SECONDS)
            token = combine_auth_cookie(await self.cookies(refresh=True))
        return token

    async def sitekey(self) -> str:
        try:
            value = await self.evaluate(_SITEKEY_JS, timeout=20.0)
        except CDPError:
            return ""
        return str(value or "").strip()

    async def mint_recaptcha(self, action: str) -> str:
        """Mint a reCAPTCHA v3 token for ``action`` inside the page.

        Returning an empty string is not fatal on its own: Arena accepts an
        empty token from an already-signed-in session often enough that failing
        the whole request here would be worse than trying.
        """
        sitekey = await self.sitekey()
        if not sitekey:
            return ""
        try:
            value = await self.evaluate(_mint_recaptcha_js(sitekey, action), timeout=45.0)
        except CDPError:
            return ""
        return str(value or "")

    async def fetch(
        self,
        path: str,
        *,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        body: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        value = await self.evaluate(
            _page_fetch_js(path, method=method, headers=headers, body=body),
            timeout=timeout,
        )
        if not isinstance(value, dict):
            raise CDPError("页面请求没有返回结果")
        return {
            "status": int(value.get("status") or 0),
            "headers": value.get("headers") if isinstance(value.get("headers"), dict) else {},
            "text": str(value.get("text") or ""),
        }

    async def model_table(self) -> list[dict[str, Any]]:
        value = await self.evaluate(
            _MODEL_TABLE_JS % {"path": ARENA_PAGE_PATH}, timeout=60.0
        )
        if not isinstance(value, dict) or not value.get("ok"):
            detail = ""
            if isinstance(value, dict):
                detail = str(value.get("error") or value.get("status") or "")
            raise CDPError(f"没能从页面读到模型列表{('：' + detail) if detail else ''}")
        return parse_model_table(str(value.get("json") or ""))

    async def next_actions(self) -> dict[str, str]:
        """Rediscover the Next.js server-action IDs from the live JS chunks.

        Arena rebuilds these IDs on every deploy, so they are read from whatever
        bundle the page actually loaded instead of being pinned in config.
        """
        value = await self.evaluate(
            _NEXT_ACTION_JS % {"pattern": json.dumps(_NEXT_ACTION_RE)}, timeout=120.0
        )
        actions = value.get("actions") if isinstance(value, dict) else None
        if not isinstance(actions, dict):
            return {}
        return {str(k): str(v) for k, v in actions.items() if k and v}


@contextlib.asynccontextmanager
async def open_page(cdp_url: str, *, timeout: float = 30.0, browser_url: str = ""):
    """Attach to the signed-in Arena tab: getTargets → attachToTarget(flatten)."""
    async with CDPWebSocket(cdp_url, timeout=timeout) as cdp:
        try:
            targets = await cdp.command("Target.getTargets", timeout=30.0)
        except CDPError as exc:
            raise _err(
                f"连不上服务器浏览器：{exc}",
                code="browser_unavailable",
                browser_url=browser_url,
            ) from exc
        pages = [
            item
            for item in (targets.get("targetInfos") or [])
            if isinstance(item, dict)
            and item.get("type") == "page"
            and "arena.ai" in str(item.get("url") or "")
        ]
        if not pages:
            raise _err(
                "服务器浏览器里没有打开的 Arena 页面，请先用验证链接登录一次。",
                code="arena_verification_required",
                browser_url=browser_url,
            )
        chosen = next(
            (p for p in pages if ARENA_PAGE_PATH in str(p.get("url") or "")), pages[0]
        )
        attached = await cdp.command(
            "Target.attachToTarget",
            {"targetId": chosen.get("targetId"), "flatten": True},
            timeout=30.0,
        )
        session_id = str(attached.get("sessionId") or "")
        if not session_id:
            raise _err(
                "无法接入 Arena 页面（CDP 拒绝 attach）。",
                code="arena_verification_required",
                browser_url=browser_url,
            )
        try:
            yield ArenaPage(
                cdp,
                session_id,
                str(chosen.get("url") or ""),
                target_id=str(chosen.get("targetId") or ""),
            )
        except CDPError as exc:
            # Everything above this module speaks ``BridgeError``, so a CDP
            # transport failure is translated once, here, instead of leaking a
            # RuntimeError into the command handlers.
            raise _err(
                str(exc), code="browser_transport_failed", browser_url=browser_url
            ) from exc


async def read_browser_file(
    cdp: CDPWebSocket,
    url: str,
    *,
    attempts: int = 12,
    timeout: float = 10.0,
) -> str:
    """Read a text file from inside the browser container, via a scratch tab.

    Used for the link-signing secret, which is mounted into ``arena-browser``
    but not into AstrBot.  The tab is opened in the background and closed again
    in ``finally`` so the operator never sees it and no target leaks if the read
    fails.  Returns ``""`` rather than raising: every caller has a fallback.
    """
    target_id = ""
    try:
        created = await cdp.command(
            "Target.createTarget", {"url": url, "background": True}, timeout=timeout
        )
        target_id = str(created.get("targetId") or "")
        if not target_id:
            return ""
        attached = await cdp.command(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
            timeout=timeout,
        )
        session_id = str(attached.get("sessionId") or "")
        if not session_id:
            return ""
        for _ in range(max(1, int(attempts))):
            text = await cdp.evaluate(
                "document.body ? document.body.innerText : ''",
                session_id=session_id,
                timeout=timeout,
            )
            if text:
                return str(text)
            # file:// targets report a load before the renderer has painted the
            # text node, so a short poll beats a single read.
            await asyncio.sleep(0.4)
        return ""
    except CDPError:
        return ""
    finally:
        if target_id:
            with contextlib.suppress(CDPError):
                await cdp.command(
                    "Target.closeTarget", {"targetId": target_id}, timeout=timeout
                )


# --- SSE ---------------------------------------------------------------------
#
# Arena's stream is a line protocol rather than real SSE events:
#   ag:"reasoning"      a0:"text chunk"     ac:[citations]
#   a2:[{"type":"image","image":"https://…"}]
#   a3:"error string"   ad:{"finishReason":"stop"}
# Some deployments prefix every line with ``data: ``.

_IMAGE_KEYS = ("image", "url", "data", "b64_json")


def _collect_image(part: Any, images: list[str]) -> None:
    if isinstance(part, str):
        if part.strip():
            images.append(part.strip())
        return
    if not isinstance(part, dict):
        return
    kind = str(part.get("type") or "").casefold()
    if kind and kind not in {"image", "image_url", "file", "output_image"}:
        return
    for key in _IMAGE_KEYS:
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            images.append(value.strip())
            return
        if isinstance(value, dict):
            nested = value.get("url") or value.get("image")
            if isinstance(nested, str) and nested.strip():
                images.append(nested.strip())
                return


def parse_stream(body: str) -> dict[str, Any]:
    """Fold the streamed lines into text, image URLs, error and finish reason."""
    text_parts: list[str] = []
    images: list[str] = []
    error = ""
    finish_reason = ""
    for raw_line in (body or "").splitlines():
        line = raw_line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or ":" not in line:
            continue
        prefix, _, remainder = line.partition(":")
        remainder = remainder.strip()
        if not remainder:
            continue
        try:
            value = json.loads(remainder)
        except json.JSONDecodeError:
            value = remainder
        if prefix == "a0":
            if isinstance(value, str):
                text_parts.append(value)
        elif prefix == "a2":
            if isinstance(value, list):
                for part in value:
                    _collect_image(part, images)
            else:
                _collect_image(value, images)
        elif prefix == "a3":
            error = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        elif prefix == "ad" and isinstance(value, dict):
            finish_reason = str(value.get("finishReason") or "")
    unique: list[str] = []
    for url in images:
        if url not in unique:
            unique.append(url)
    return {
        "text": "".join(text_parts).strip(),
        "images": unique,
        "error": error.strip(),
        "finish_reason": finish_reason,
    }


# --- live session state ------------------------------------------------------

_CHALLENGE_URL_MARKERS = ("/login", "/signin", "/sign-in", "/auth/")


def signed_url_expiry(url: str) -> float | None:
    """Expiry of an S3/R2 pre-signed URL, so an upload is reused not repeated."""
    try:
        params = parse_qs(urlsplit(str(url or "")).query)
        date_value = (params.get("X-Amz-Date") or [""])[0]
        expires_value = (params.get("X-Amz-Expires") or [""])[0]
        if not date_value or not expires_value:
            return None
        moment = datetime.strptime(date_value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
        return moment.timestamp() + int(expires_value)
    except (ValueError, TypeError, IndexError, AttributeError):
        return None


def _url_is_challenged(url: str) -> bool:
    lowered = str(url or "").casefold()
    return any(marker in lowered for marker in _CHALLENGE_URL_MARKERS)


async def _live_state(page: ArenaPage) -> dict[str, Any]:
    """Classify the browser's *current* session without storing any cookie."""
    cookies = await page.cookies(refresh=True)
    token = combine_auth_cookie(cookies)
    refreshed = False
    # A token that expired while the tab sat idle is not a logout: the refresh
    # token beside it is still good, and the page rotates it on the next
    # request.  Rotate it here, before judging, so the first command after a
    # quiet night does not answer "Cookie 失效，请重新绑定" about a login that
    # is perfectly fine.
    if token and session_is_expired(token) and session_can_refresh(token):
        rotated = await page.refresh_session()
        if rotated and not session_is_expired(rotated):
            token = rotated
            refreshed = True
            cookies = await page.cookies()
        elif rotated:
            token = rotated
    plausible = session_is_plausible(token)
    expired = bool(token) and session_is_expired(token)
    logged_in = bool(token) and session_is_logged_in(token) and not expired
    expiry = session_expiry_epoch(token) if token else None
    challenged = _url_is_challenged(page.url)
    return {
        "has_arena_auth": bool(token) and plausible,
        "has_cf_clearance": bool(cookie_value(cookies, "cf_clearance")),
        "has_logged_in": logged_in,
        "session_logged_in": logged_in,
        "session_expired": expired,
        "session_refreshed": refreshed,
        "session_refreshable": bool(token) and expired and session_can_refresh(token),
        "session_source": "live-browser" if token else "",
        "session_expires_in": (
            max(0, int(expiry - time.time())) if isinstance(expiry, int) else None
        ),
        "recaptcha_action": "chat_submit" if logged_in else "sign_up",
        "verified": logged_in and not challenged,
        "page_url": page.url,
    }


# --- client ------------------------------------------------------------------


class ArenaDirectClient:
    """Same interface as ``ArenaBridgeClient``, no bridge container required.

    ``main.py`` only chooses which of the two to build; every command above it
    keeps calling the same seven methods with the same arguments.
    """

    def __init__(
        self,
        cdp_url: str,
        *,
        gateway_url: str = "",
        vnc_url: str = "",
        link_secret: str = "",
        link_ttl: float = 900.0,
        timeout: float = 300.0,
        rate_limit_retries: int = 2,
        rate_limit_max_wait: float = 30.0,
        allow_stealth_models: bool = True,
        allowed_stealth_models: frozenset[str] | None = None,
        data_dir: Path | str | None = None,
    ) -> None:
        self.cdp_url = str(cdp_url or "").strip().rstrip("/")
        self.gateway_url = str(gateway_url or "").strip().rstrip("/")
        self.vnc_url = str(vnc_url or "").strip()
        self._link_secret = str(link_secret or "")
        self.link_ttl = max(120.0, min(1800.0, float(link_ttl or 900.0)))
        self.timeout = max(10.0, float(timeout or 300.0))
        self.rate_limit_retries = max(0, int(rate_limit_retries))
        self.rate_limit_max_wait = max(1.0, float(rate_limit_max_wait or 30.0))
        self.allow_stealth_models = bool(allow_stealth_models)
        self.allowed_stealth_models = frozenset(
            name.casefold()
            for name in (
                allowed_stealth_models
                if allowed_stealth_models is not None
                else DEFAULT_ALLOWED_STEALTH_MODELS
            )
        )
        if data_dir:
            _load_health(Path(data_dir) / "arena_model_health.json")
        if not self.cdp_url:
            raise BridgeError("服务器浏览器 CDP 地址为空")
        # Last link minted with an auto-discovered secret, so error messages can
        # still carry a working URL without opening a CDP session of their own.
        self._link_cache: tuple[str, float] = ("", 0.0)
        self._mint_failure = ""

    # -- plumbing -------------------------------------------------------------

    def _gateway_base(self) -> str:
        """The gateway origin: configured, or derived from the noVNC address."""
        if self.gateway_url:
            return self.gateway_url
        return gateway_base_from(self.vnc_url)

    def _link(self) -> str:
        """A link for error messages: signed if we can, cached if we already did."""
        base = self._gateway_base()
        if base and self._link_secret:
            try:
                return build_interactive_link(
                    base,
                    expires_at=time.time() + self.link_ttl,
                    secret=self._link_secret,
                )
            except Exception:
                return self.vnc_url
        cached, expires_at = self._link_cache
        if cached and time.time() < expires_at:
            return cached
        return self.vnc_url

    async def _resolve_link_secret(self, cdp: CDPWebSocket) -> str:
        """Configured secret, else read it out of the browser container.

        Reading beats configuring: the gateway validates against the same file,
        so an auto-read key cannot drift out of sync, and nothing sensitive has
        to be pasted into the plugin config.
        """
        if self._link_secret:
            return self._link_secret
        cached, read_at = _LINK_SECRETS.get(self.cdp_url, ("", 0.0))
        if cached and time.time() - read_at < LINK_SECRET_CACHE_SECONDS:
            return cached
        for source in BROWSER_LINK_SECRET_FILES:
            value = "".join(str(await read_browser_file(cdp, source)).split())
            if looks_like_link_secret(value):
                _LINK_SECRETS[self.cdp_url] = (value, time.time())
                return value
        return ""

    async def _resolve_gateway_base(self, cdp: CDPWebSocket) -> str:
        """Configured address, else the one the deployment left in the browser."""
        base = self._gateway_base()
        if base:
            return base
        cached, read_at = _GATEWAY_URLS.get(self.cdp_url, ("", 0.0))
        if cached and time.time() - read_at < LINK_SECRET_CACHE_SECONDS:
            return cached
        for source in BROWSER_GATEWAY_URL_FILES:
            raw = str(await read_browser_file(cdp, source)).strip().splitlines()
            text = raw[0].strip() if raw else ""
            if not text.startswith(("http://", "https://")):
                continue
            candidate = gateway_base_from(text)
            if candidate:
                _GATEWAY_URLS[self.cdp_url] = (candidate, time.time())
                return candidate
        return ""

    async def _mint_link(self, cdp: CDPWebSocket) -> str:
        """The link the operator opens, minted with whatever we could discover.

        ``self._mint_failure`` records *which* half was missing so the caller can
        say something more useful than "link unavailable".
        """
        self._mint_failure = ""
        base = await self._resolve_gateway_base(cdp)
        if not base:
            self._mint_failure = "gateway"
            return self.vnc_url
        secret = await self._resolve_link_secret(cdp)
        if not secret:
            self._mint_failure = "secret"
            return self.vnc_url
        expires_at = time.time() + self.link_ttl
        link = build_interactive_link(base, expires_at=expires_at, secret=secret)
        if link:
            self._link_cache = (link, expires_at)
        else:
            self._mint_failure = "sign"
        return link or self.vnc_url

    def _page(self, *, timeout: float | None = None):
        return open_page(
            self.cdp_url,
            timeout=float(timeout if timeout is not None else self.timeout),
            browser_url=self._link(),
        )

    def _stealth_allowed(self, public_name: Any) -> bool:
        if self.allow_stealth_models:
            return True
        return str(public_name or "").strip().casefold() in self.allowed_stealth_models

    def _keep_model(self, model: Any) -> bool:
        """Exactly the bridge's `/api/v1/models` filter, kept row for row."""
        if not isinstance(model, dict):
            return False
        if not model_has_supported_output(model):
            return False
        if model.get("userSelectable") is False:
            return False
        return bool(model.get("organization")) or self._stealth_allowed(
            model.get("publicName")
        )

    async def _models(self, page: ArenaPage, *, refresh: bool = False) -> list[dict[str, Any]]:
        global _MODELS, _MODELS_AT
        async with _LOCK:
            fresh = _MODELS and time.time() - _MODELS_AT <= MODEL_CACHE_SECONDS
            if fresh and not refresh:
                return list(_MODELS)
        table = await page.model_table()
        if len(table) < 10:
            # A truncated table would silently hide most models; keep the old one.
            async with _LOCK:
                if _MODELS:
                    return list(_MODELS)
            raise _err("竞技场模型列表异常（行数过少），请稍后再试。", code="model_table_empty")
        async with _LOCK:
            _MODELS = table
            _MODELS_AT = time.time()
        return list(table)

    def _variants(self, models: list[dict[str, Any]], public_name: str) -> list[dict[str, Any]]:
        wanted = str(public_name or "").strip().casefold()
        rows = [
            model
            for model in models
            if isinstance(model, dict)
            and str(model.get("publicName") or "").strip().casefold() == wanted
            and model.get("userSelectable") is not False
            and model.get("id")
        ]
        image_rows = [row for row in rows if model_has_supported_output(row)]
        return image_rows or rows

    @staticmethod
    def _pick_variant(rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Newest checkpoint that has not failed in the last 10 minutes.

        Arena keeps several rows per public name; the newest one is usually the
        live checkpoint, but when it starts answering 500 the older row still
        works, which is what the variant health window is for.
        """
        ordered = sorted(
            rows,
            key=lambda row: uuid7_created_at(row.get("id")) or 0,
            reverse=True,
        )
        for row in ordered:
            if not _variant_failed_recently(str(row.get("id") or "")):
                return row
        return ordered[0]

    @staticmethod
    def _modality(model: dict[str, Any]) -> str:
        if model_capability(model, "outputCapabilities", "image"):
            return "image"
        if model_capability(model, "inputCapabilities", "search") or model_capability(
            model, "outputCapabilities", "search"
        ):
            return "search"
        return "chat"

    # -- mirrored API ---------------------------------------------------------

    async def list_models(self) -> list[dict[str, Any]]:
        async with self._page(timeout=90.0) as page:
            models = await self._models(page)
        return [public_model_entry(model) for model in models if self._keep_model(model)]

    async def model_health(self) -> dict[str, Any]:
        return _health_snapshot()

    async def health(self) -> dict[str, Any]:
        """Reachability of the server browser plus the live login verdict."""
        async with self._page(timeout=60.0) as page:
            state = await _live_state(page)
        healthy = bool(state.get("has_logged_in"))
        payload: dict[str, Any] = {
            "status": "healthy" if healthy else "degraded",
            "transport": "direct-cdp",
            "browser": "connected",
            "page_url": state.get("page_url") or "",
        }
        payload.update(
            {
                key: state.get(key)
                for key in (
                    "has_arena_auth",
                    "has_cf_clearance",
                    "has_logged_in",
                    "session_source",
                    "session_expires_in",
                    "session_refreshed",
                    "session_refreshable",
                    "recaptcha_action",
                )
            }
        )
        if not healthy:
            payload["message"] = (
                "登录还在，但访问令牌过期了、这次没能自动续期，稍后再试一次即可。"
                if state.get("session_refreshable")
                else "服务器浏览器可用，但 Arena 还是匿名会话，请私聊运行 /竞技场验证 后登录账号。"
            )
        return payload

    # -- interactive verification --------------------------------------------

    async def _ensure_target(self, cdp: CDPWebSocket, session_id: str) -> dict[str, Any]:
        """Reuse the Arena tab if there is one, otherwise open it.

        Reuse matters: the operator drives a single visible Chrome, and creating
        a tab per verification round would leave a pile of them behind.
        """
        targets = await cdp.command("Target.getTargets", timeout=30.0)
        pages = [
            item
            for item in (targets.get("targetInfos") or [])
            if isinstance(item, dict)
            and item.get("type") == "page"
            and "arena.ai" in str(item.get("url") or "")
        ]
        if pages:
            chosen = next(
                (p for p in pages if ARENA_PAGE_PATH in str(p.get("url") or "")), pages[0]
            )
            target_id = str(chosen.get("targetId") or "")
            with contextlib.suppress(CDPError):
                await cdp.command(
                    "Target.activateTarget", {"targetId": target_id}, timeout=20.0
                )
            return {"target_id": target_id, "url": str(chosen.get("url") or ""), "created": False}
        url = f"{ARENA_ORIGIN}/?mode=direct#lm-bridge-auth-{session_id}"
        created = await cdp.command("Target.createTarget", {"url": url}, timeout=30.0)
        target_id = str(created.get("targetId") or "")
        if not target_id:
            raise _err(
                "服务器浏览器无法打开 Arena 页面。",
                code="browser_unavailable",
                browser_url=self._link(),
            )
        return {"target_id": target_id, "url": url, "created": True}

    def _auth_payload(
        self,
        record: dict[str, Any],
        state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = time.time()
        expires_at = float(record.get("expires_at") or 0)
        expired = expires_at > 0 and now >= expires_at
        verified = bool((state or {}).get("verified"))
        if verified:
            status = "verified"
        elif expired:
            status = "expired"
        else:
            status = str(record.get("status") or "waiting")
        payload: dict[str, Any] = {
            "session_id": str(record.get("session_id") or ""),
            "status": status,
            "browser_url": str(record.get("browser_url") or ""),
            "vnc_url": self.vnc_url,
            "transport": "direct-cdp",
            "created_at": record.get("created_at"),
            "expires_at": expires_at or None,
            "expires_in": max(0, int(expires_at - now)) if expires_at else None,
        }
        payload.update(state or {})
        payload["status"] = status
        payload["verified"] = verified
        payload["message"] = self._auth_message(status, state or {})
        return payload

    @staticmethod
    def _auth_message(status: str, state: dict[str, Any]) -> str:
        if status == "verified":
            return "服务器浏览器已登录 Arena，可以直接画图。"
        if status == "expired":
            return "验证链接已过期，请重新运行 /竞技场验证 获取新链接。"
        if state.get("session_refreshable"):
            return "登录还在，只是访问令牌过期了，稍等一会儿或再发一次命令就会自动续期。"
        if state.get("has_arena_auth") and not state.get("has_logged_in"):
            return "已拿到 Arena 会话，但仍是匿名状态，请在服务器浏览器里登录账号。"
        return "请打开验证链接，在服务器浏览器里完成 Cloudflare 验证并登录 Arena 账号。"

    async def start_interactive_auth(self) -> dict[str, Any]:
        session_id = uuid.uuid4().hex
        async with CDPWebSocket(self.cdp_url, timeout=60.0) as cdp:
            try:
                target = await self._ensure_target(cdp, session_id)
            except CDPError as exc:
                raise _err(
                    f"连不上服务器浏览器：{exc}", code="browser_unavailable"
                ) from exc
            # Minted inside the CDP session on purpose: that is what lets the
            # address and the signing key be discovered from the browser itself
            # instead of being copied into the plugin config.
            link = await self._mint_link(cdp)
            session = ""
            with contextlib.suppress(CDPError):
                attached = await cdp.command(
                    "Target.attachToTarget",
                    {"targetId": target["target_id"], "flatten": True},
                    timeout=30.0,
                )
                session = str(attached.get("sessionId") or "")
            state = None
            if session:
                with contextlib.suppress(CDPError):
                    state = await _live_state(
                        ArenaPage(
                            cdp,
                            session,
                            target["url"],
                            target_id=str(target.get("target_id") or ""),
                        )
                    )
        if not link:
            if self._mint_failure == "secret":
                raise _err(
                    "拿不到验证链接：网关地址有了，但签名密钥既没配置也没能从服务器浏览器"
                    "读到（/run/secrets/interactive_link_secret 不可读）。",
                    code="interactive_link_unavailable",
                )
            raise _err(
                "还差一个地址：请在插件配置里把 browser_gateway_url 填成"
                f" http://服务器IP:{GATEWAY_DEFAULT_PORT}（签名密钥会自动读取，其它都不用填）。",
                code="interactive_link_unavailable",
            )
        now = time.time()
        record = {
            "session_id": session_id,
            "status": "waiting",
            "created_at": now,
            "expires_at": now + self.link_ttl,
            "browser_url": link,
            "target_id": target["target_id"],
        }
        async with _LOCK:
            for key, value in list(_SESSIONS.items()):
                if float(value.get("expires_at") or 0) < now - 3600:
                    _SESSIONS.pop(key, None)
            _SESSIONS[session_id] = record
        return self._auth_payload(record, state)

    async def _current_state(self) -> dict[str, Any] | None:
        try:
            async with self._page(timeout=60.0) as page:
                return await _live_state(page)
        except (BridgeError, CDPError):
            return None

    async def interactive_auth_status(self, session_id: str) -> dict[str, Any]:
        clean_id = str(session_id or "").strip()
        if not clean_id:
            raise BridgeError("验证会话编号为空")
        async with _LOCK:
            record = dict(_SESSIONS.get(clean_id) or {})
        state = await self._current_state()
        if not record:
            # The plugin was reloaded: the browser still holds the real answer,
            # so report that instead of failing with "unknown session".
            now = time.time()
            record = {
                "session_id": clean_id,
                "status": "waiting",
                "created_at": now,
                "expires_at": now + self.link_ttl,
                "browser_url": self._link(),
            }
        return self._auth_payload(record, state)

    async def latest_interactive_auth_status(self) -> dict[str, Any]:
        async with _LOCK:
            records = sorted(
                (dict(value) for value in _SESSIONS.values()),
                key=lambda item: float(item.get("created_at") or 0),
                reverse=True,
            )
        state = await self._current_state()
        if records:
            return self._auth_payload(records[0], state)
        now = time.time()
        record = {
            "session_id": "",
            "status": "waiting",
            "created_at": now,
            "expires_at": now + self.link_ttl,
            "browser_url": self._link(),
        }
        return self._auth_payload(record, state)

    # -- reference-image upload ----------------------------------------------

    async def _next_actions(self, page: ArenaPage) -> dict[str, str]:
        """Cached Next.js server-action IDs, rediscovered from the live bundle."""
        global _ACTIONS, _ACTIONS_AT
        async with _LOCK:
            if _ACTIONS and time.time() - _ACTIONS_AT <= ACTION_CACHE_SECONDS:
                return dict(_ACTIONS)
        actions = await page.next_actions()
        if actions:
            async with _LOCK:
                _ACTIONS = actions
                _ACTIONS_AT = time.time()
        return dict(actions)

    @staticmethod
    def _flight_payload(body: str) -> dict[str, Any] | None:
        """Read the ``1:`` line of a Next.js flight response."""
        for line in (body or "").strip().splitlines():
            if line.startswith("1:"):
                try:
                    value = json.loads(line[2:])
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
        return None

    async def _upload_reference(self, page: ArenaPage, value: str) -> dict[str, Any]:
        """Upload one reference image and return its attachment entry.

        Steps 1 and 3 must run inside the page (they need the session cookies and
        Cloudflare clearance); step 2 is a plain signed R2 PUT, so it goes out
        directly from the plugin.
        """
        raw, mime = decode_image_value(value)
        digest = hashlib.md5(raw).hexdigest()
        async with _LOCK:
            cached = dict(_UPLOADS.get(digest) or {})
        if cached and float(cached.get("expiry") or 0) > time.time() + 60:
            return {
                "name": cached["name"],
                "contentType": cached["contentType"],
                "url": cached["url"],
            }

        actions = await self._next_actions(page)
        upload_action = actions.get("generateUploadUrl") or ""
        signed_action = actions.get("getSignedUrl") or ""
        if not upload_action or not signed_action:
            raise _err(
                "没能从竞技场页面读到上传接口（Next-Action）ID，图生图暂时不可用。",
                code="upload_action_missing",
            )
        extension = mimetypes.guess_extension(mime) or ".png"
        filename = f"{digest[:12]}{'.jpg' if extension == '.jpe' else extension}"
        headers = {"Accept": "text/x-component", "Referer": f"{ARENA_ORIGIN}/?mode=direct"}

        first = await page.fetch(
            ARENA_PAGE_PATH,
            headers={**headers, "Next-Action": upload_action},
            body=json.dumps([filename, mime]),
            timeout=60.0,
        )
        payload = self._flight_payload(first.get("text") or "")
        data = payload.get("data") if isinstance(payload, dict) else None
        upload_url = str((data or {}).get("uploadUrl") or "")
        key = str((data or {}).get("key") or "")
        if first.get("status") != 200 or not upload_url or not key:
            raise _err(
                f"申请参考图上传地址失败（HTTP {first.get('status')}）。",
                code="upload_url_failed",
                status_code=int(first.get("status") or 0) or None,
            )

        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                put = await client.put(
                    upload_url, content=raw, headers={"Content-Type": mime}
                )
            if put.status_code >= 400:
                raise _err(
                    f"上传参考图失败（HTTP {put.status_code}）。",
                    code="upload_failed",
                    status_code=put.status_code,
                )
        except httpx.HTTPError as exc:
            raise _err(f"上传参考图失败：{exc}", code="upload_failed") from exc

        third = await page.fetch(
            ARENA_PAGE_PATH,
            headers={**headers, "Next-Action": signed_action},
            body=json.dumps([key]),
            timeout=60.0,
        )
        payload = self._flight_payload(third.get("text") or "")
        data = payload.get("data") if isinstance(payload, dict) else None
        download_url = str((data or {}).get("url") or "")
        if third.get("status") != 200 or not download_url:
            raise _err(
                f"获取参考图下载地址失败（HTTP {third.get('status')}）。",
                code="upload_signed_url_failed",
                status_code=int(third.get("status") or 0) or None,
            )

        entry = {"name": filename, "contentType": mime, "url": download_url}
        async with _LOCK:
            if len(_UPLOADS) >= UPLOAD_CACHE_LIMIT:
                for stale in list(_UPLOADS)[: max(1, UPLOAD_CACHE_LIMIT // 10)]:
                    _UPLOADS.pop(stale, None)
            _UPLOADS[digest] = {
                **entry,
                "key": key,
                "expiry": signed_url_expiry(download_url) or (time.time() + 3600),
            }
        return entry

    # -- generation -----------------------------------------------------------

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        clean_model = str(model or "").strip()
        clean_prompt = str(prompt or "").strip()
        image_values = [str(image).strip() for image in (images or []) if str(image).strip()]
        if not clean_model:
            raise BridgeError("模型名不能为空")
        if not clean_prompt and not image_values:
            raise BridgeError("提示词和参考图不能同时为空")

        attempt = 0
        while True:
            try:
                return await self._generate(clean_model, clean_prompt, image_values)
            except BridgeError as exc:
                if not exc.is_rate_limited or attempt >= self.rate_limit_retries:
                    raise
                attempt += 1
                delay = float(exc.retry_after or 0) or min(15.0, 5.0 * attempt)
                await asyncio.sleep(max(1.0, min(self.rate_limit_max_wait, delay)))

    async def _resolve(
        self, page: ArenaPage, public_name: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        models = await self._models(page)
        rows = self._variants(models, public_name)
        if not rows:
            models = await self._models(page, refresh=True)
            rows = self._variants(models, public_name)
        if not rows:
            raise _err(
                f"竞技场里没有可选的模型「{public_name}」，请运行 /竞技场画图模型 重新挑一个。",
                code="model_not_found",
                status_code=404,
            )
        return self._pick_variant(rows), rows

    @staticmethod
    def _require_session(state: dict[str, Any], browser_url: str) -> None:
        """Refuse early, with the wording that matches the actual problem.

        Expiry is checked *first* on purpose: an expired token is not
        "plausible", so `has_arena_auth` is already False by then and the
        missing-cookie branch would otherwise report "Cookie 缺失" for a cookie
        that is sitting right there, merely stale.
        """
        if state.get("session_expired"):
            if state.get("session_refreshable"):
                raise _err(
                    "登录还在，但访问令牌过期了，自动续期这次没成功。\n"
                    "过十几秒再发一次命令通常就好了；一直这样再运行 /竞技场重新绑定。",
                    code="arena_auth_stale",
                    browser_url=browser_url,
                )
            raise _err(
                "服务器浏览器里的 Arena 会话已过期（arena-auth 已失效）。\n"
                "请管理员私聊运行 /竞技场重新绑定 后重新登录。",
                code="arena_auth_expired",
                browser_url=browser_url,
            )
        if not state.get("has_arena_auth"):
            raise _err(
                "服务器浏览器里没有可用的 Arena 会话（arena-auth Cookie 缺失）。\n"
                "请管理员私聊运行 /竞技场重新绑定 后重新登录。",
                code="arena_auth_required",
                browser_url=browser_url,
            )
        if not state.get("has_logged_in"):
            raise _err(
                "Arena 现在是匿名会话，登录后才能出图（login required）。\n"
                "请管理员私聊运行 /竞技场验证，在服务器浏览器里用 Google 或邮箱登录。",
                code="arena_login_required",
                browser_url=browser_url,
            )

    @staticmethod
    def _retry_after(headers: dict[str, Any]) -> float | None:
        for key, value in (headers or {}).items():
            if str(key).casefold() == "retry-after":
                try:
                    return max(0.0, float(str(value).strip()))
                except (TypeError, ValueError):
                    return None
        return None

    async def _generate(
        self,
        public_name: str,
        prompt: str,
        images: list[str],
    ) -> dict[str, Any]:
        browser_url = self._link()
        async with self._page(timeout=self.timeout) as page:
            model, _rows = await self._resolve(page, public_name)
            model_id = str(model.get("id") or "")
            modality = self._modality(model)

            state = await _live_state(page)
            self._require_session(state, browser_url)

            attachments = [await self._upload_reference(page, value) for value in images]
            token = await page.mint_recaptcha(str(state.get("recaptcha_action") or "chat_submit"))
            payload = {
                "id": new_uuid7(),
                "mode": "direct-battle",
                "modelAId": model_id,
                "userMessageId": new_uuid7(),
                "modelAMessageId": new_uuid7(),
                "modelBMessageId": new_uuid7(),
                "userMessage": {
                    "content": prompt or "请根据参考图生成图片",
                    "experimental_attachments": attachments,
                    "metadata": {},
                },
                "modality": modality,
                "recaptchaV3Token": token or "",
            }
            response = await page.fetch(
                STREAM_CREATE_EVALUATION_PATH,
                method="POST",
                body=json.dumps(payload, ensure_ascii=False),
                timeout=self.timeout,
            )

        status = int(response.get("status") or 0)
        body = str(response.get("text") or "")
        stream = parse_stream(body)
        _record_health(public_name, model_id, status, stream.get("error") or "")
        error = str(stream.get("error") or "")
        # A refusal arrives as a normal 200 with an ``a3:`` line.  That is the
        # model answering, so it is reported as text rather than raised as a
        # transport failure -- raising would send the operator to /竞技场验证
        # for something verification cannot fix.
        if status != 200 or (
            error and not stream.get("images") and not _moderation_blocked(error)
        ):
            self._raise_upstream(
                status=status,
                body=body,
                stream=stream,
                headers=response.get("headers") or {},
                state=state,
                browser_url=browser_url,
                public_name=public_name,
            )
        return self._openai_payload(public_name, model_id, stream)

    def _raise_upstream(
        self,
        *,
        status: int,
        body: str,
        stream: dict[str, Any],
        headers: dict[str, Any],
        state: dict[str, Any],
        browser_url: str,
        public_name: str,
    ) -> None:
        """Turn an upstream failure into the error code the UI reacts to."""
        error = str(stream.get("error") or "")
        detail = (error or body)[:300].strip()
        lowered = f"{error} {body[:2000]}".casefold()

        if status == 429 or "too many requests" in lowered or "rate limit" in lowered:
            raise _err(
                f"竞技场上游限流：{detail}",
                code="rate_limited",
                status_code=429,
                retry_after=self._retry_after(headers),
            )
        if status == 0:
            raise _err(
                f"页面请求没有发出去：{detail}",
                code="browser_transport_failed",
                browser_url=browser_url,
            )
        if "not available for user selection" in lowered:
            raise _err(
                f"竞技场不允许手动选择模型「{public_name}」，请换一个模型。",
                code="model_not_selectable",
                status_code=400,
            )
        if status == 401 or "auth token has expired" in lowered or "arena-auth" in lowered:
            raise _err(
                f"Arena 会话被拒绝（arena auth）：{detail}",
                code="arena_auth_expired",
                status_code=status or None,
                browser_url=browser_url,
            )
        if "login" in lowered and ("required" in lowered or "gate" in lowered):
            raise _err(
                f"Arena 要求登录账号才能出图（login required）：{detail}",
                code="arena_login_required",
                status_code=status or None,
                browser_url=browser_url,
            )
        challenged = any(
            marker in lowered
            for marker in ("recaptcha", "captcha", "cloudflare", "turnstile", "just a moment")
        )
        if challenged or status == 403:
            code = (
                "arena_session_rejected"
                if state.get("has_logged_in")
                else "arena_verification_required"
            )
            raise _err(
                f"竞技场风控拦下了这次请求（HTTP {status}）：{detail}",
                code=code,
                status_code=status or 403,
                browser_url=browser_url,
            )
        raise _err(
            f"竞技场上游错误（HTTP {status}）：{detail or '没有返回内容'}",
            code=f"http_{status}" if status else "upstream_error",
            status_code=status or None,
        )

    @staticmethod
    def _openai_payload(
        public_name: str,
        model_id: str,
        stream: dict[str, Any],
    ) -> dict[str, Any]:
        """Shape the result exactly like the bridge's chat-completions reply."""
        images = list(stream.get("images") or [])
        text = str(stream.get("text") or "")
        if images:
            content = "\n".join(f"![Generated Image]({url})" for url in images)
        else:
            content = text or str(stream.get("error") or "")
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if images:
            # Carried structurally as well as in markdown, so image extraction
            # never depends on the markdown surviving intact.
            message["images"] = images
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": public_name,
            "arena_model_id": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": str(stream.get("finish_reason") or "stop") or "stop",
                }
            ],
        }
