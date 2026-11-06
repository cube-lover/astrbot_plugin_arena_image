"""Userscript relay: the third transport, driven by an ordinary person's browser.

``bridge_client`` talks to the ``arena-bridge`` container; ``arena_direct`` drives
the ``arena-browser`` container over CDP.  Both need a server-side Chrome.  This
module removes that requirement: a Tampermonkey userscript running in the tab the
visitor already logged Arena into asks the plugin for work, performs it *in the
page*, and pushes the answer back.

The plugin becomes the client of nothing and the server of one small protocol:

* ``GET  {prefix}/hello``               reachability probe (the panel's 测试连通)
* ``POST {prefix}/poll``                long-poll for one job, doubles as heartbeat
* ``POST {prefix}/push``                job result, or a mid-job keepalive
* ``POST {prefix}/report``              unsolicited state / self-test report
* ``GET  {prefix}/userscript.user.js``  the script itself, behind a signed token

Two properties are deliberate and worth keeping if this file is ever edited:

**Arena cookies never leave the browser.**  The relay has no verb that returns a
cookie.  Whether the tab is signed in is decided *in* the tab and only the verdict
crosses the wire (``arena_direct._live_state`` calls ``page.live_state()`` when
the transport provides it, which ``RelayPage`` does).

**There is no eval verb.**  ``VERBS`` is a closed allowlist; the script refuses
anything else.  A plugin that got compromised still cannot run arbitrary
JavaScript in someone's logged-in Arena tab -- it can only ask for the same six
things the CDP transport asks for.

Every request carries an HMAC over ``method.path.ts.nonce.sha256(body)``.  The
shared secret is mandatory: with no secret configured one is generated and stored
in the plugin data directory, because an unauthenticated relay on a public port is
a remote handle on someone's Arena account.

Standard library only.  A stdlib HTTP/1.1 server is more code than reaching for
aiohttp, but the plugin's dependency list is ``httpx>=0.24`` and that is worth
keeping.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .arena_direct import (
    ARENA_ORIGIN,
    ARENA_PAGE_PATH,
    ArenaDirectClient,
    _err,
    _live_state,
    _NEXT_ACTION_RE,
    parse_model_table,
)
from .model_catalog import history_catalog
from .bridge_client import BridgeError

RELAY_PROTOCOL = 1
DEFAULT_RELAY_PORT = 6083
DEFAULT_PATH_PREFIX = "/arena"
SECRET_FILENAME = "arena_relay_secret.txt"

# How long a poll is held open before answering 204.  Below 30s because proxies
# and phone networks like to cut idle connections at 30, and the script treats a
# 204 as "still fine, ask again" rather than an error.
POLL_HOLD_SECONDS = 25.0
# A browser that has not polled or sent a keepalive within this window is gone.
# Larger than the keepalive interval times four: background tabs get their timers
# throttled to roughly once a minute, and a throttled browser is still working.
BROWSER_STALE_SECONDS = 150.0
BROWSER_DROP_SECONDS = 1800.0
KEEPALIVE_HINT_SECONDS = 15.0
# Clock skew allowed on signed requests, and how long a nonce is remembered.
CLOCK_SKEW_SECONDS = 180.0
NONCE_TTL_SECONDS = 600.0
NONCE_LIMIT = 8192
INSTALL_TOKEN_TTL = 1800.0
# Grace added to a job's own timeout before the relay stops waiting, so the
# browser gets to report a real error instead of the relay guessing at one.
JOB_GRACE_SECONDS = 20.0
DEFAULT_JOB_TIMEOUT = 120.0
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 24 * 1024 * 1024
LOG_LIMIT = 40

# The closed verb allowlist.  Adding "eval" here would undo the main safety
# property of this transport; do not.
VERBS = frozenset({"state", "models", "actions", "recaptcha", "reload", "fetch"})

_HTTP_REASONS = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    409: "Conflict",
    413: "Payload Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}

# --- signing -----------------------------------------------------------------


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def canonical_request(method: str, path: str, ts: str, nonce: str, digest: str) -> str:
    """The exact string both sides sign.

    The query string is left out: the only endpoint that carries one authenticates
    with its own token, and including it would break the moment a proxy reordered
    parameters.
    """
    return "\n".join(
        (
            str(method or "").upper(),
            str(path or ""),
            str(ts or ""),
            str(nonce or ""),
            str(digest or ""),
        )
    )


def sign_request(
    secret: str, method: str, path: str, ts: Any, nonce: str, body: bytes
) -> str:
    message = canonical_request(method, path, str(ts), nonce, body_digest(body))
    return hmac.new(
        str(secret or "").encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def new_secret() -> str:
    return secrets.token_hex(32)


def load_secret(data_dir: Path | str | None, configured: str = "") -> str:
    """The configured secret, else a generated one kept in the data directory.

    Generating beats defaulting to "no auth": the relay listens on a real port, so
    an empty secret would hand anyone who found it a remote handle on the
    visitor's signed-in Arena tab.  The install link carries the secret to the
    script, so the operator never has to see this value.
    """
    clean = "".join(str(configured or "").split())
    if len(clean) >= 16:
        return clean
    if not data_dir:
        return new_secret()
    path = Path(data_dir) / SECRET_FILENAME
    with contextlib.suppress(OSError):
        stored = "".join(path.read_text(encoding="utf-8").split())
        if len(stored) >= 16:
            return stored
    secret = new_secret()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(secret, encoding="utf-8")
        with contextlib.suppress(OSError, NotImplementedError):
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    return secret


def install_token(secret: str, *, ttl: float = INSTALL_TOKEN_TTL) -> str:
    """A short-lived bearer token for the script download URL."""
    expires_at = int(time.time() + max(60.0, float(ttl or INSTALL_TOKEN_TTL)))
    nonce = secrets.token_hex(8)
    signature = hmac.new(
        str(secret or "").encode("utf-8"),
        f"install.{expires_at}.{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{expires_at}.{nonce}.{signature}"


def check_install_token(secret: str, token: str) -> bool:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return False
    expires_at, nonce, signature = parts
    if not expires_at.isdigit() or int(expires_at) < time.time():
        return False
    expected = hmac.new(
        str(secret or "").encode("utf-8"),
        f"install.{int(expires_at)}.{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return secrets.compare_digest(expected, signature)


def _ident() -> str:
    return uuid.uuid4().hex[:12]


# --- registry ----------------------------------------------------------------


class RelayJob:
    """One verb sent to one browser, plus the future its caller is awaiting."""

    __slots__ = ("id", "verb", "payload", "timeout", "created_at", "leased_at", "browser_id", "future")

    def __init__(
        self,
        verb: str,
        payload: dict[str, Any],
        *,
        timeout: float,
        browser_id: str,
    ) -> None:
        self.id = _ident()
        self.verb = str(verb)
        self.payload = dict(payload or {})
        self.timeout = max(5.0, float(timeout or DEFAULT_JOB_TIMEOUT))
        self.browser_id = str(browser_id)
        self.created_at = time.time()
        self.leased_at = 0.0
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()

    def wire(self) -> dict[str, Any]:
        """What the browser receives.  Timeouts are in ms to suit JS timers."""
        return {
            "protocol": RELAY_PROTOCOL,
            "job_id": self.id,
            "verb": self.verb,
            "payload": self.payload,
            "timeout_ms": int(self.timeout * 1000),
            "keepalive_ms": int(KEEPALIVE_HINT_SECONDS * 1000),
        }

    def resolve(self, result: Any) -> None:
        if not self.future.done():
            self.future.set_result(result)

    def fail(self, error: BaseException) -> None:
        if not self.future.done():
            self.future.set_exception(error)


class RelayBrowser:
    """A connected userscript: its job queue, its last reported login verdict.

    ``state`` is the dictionary ``arena_direct._live_state`` would have built from
    cookies, except the browser built it itself.  No cookie is ever stored here.
    """

    def __init__(self, browser_id: str, *, label: str = "") -> None:
        self.id = str(browser_id)
        self.label = str(label or "")
        self.queue: asyncio.Queue[RelayJob] = asyncio.Queue()
        self.inflight: dict[str, RelayJob] = {}
        self.state: dict[str, Any] = {}
        self.page_url = ""
        self.user_agent = ""
        self.script_version = ""
        self.remote = ""
        self.paused = False
        self.connected_at = time.time()
        self.last_seen = time.time()
        self.last_job_at = 0.0
        self.state_at = 0.0
        self.polls = 0
        self.jobs_done = 0
        self.jobs_failed = 0

    @property
    def online(self) -> bool:
        return time.time() - self.last_seen <= BROWSER_STALE_SECONDS

    @property
    def ready(self) -> bool:
        return self.online and not self.paused

    @property
    def busy(self) -> bool:
        return bool(self.inflight) or not self.queue.empty()

    def touch(self, **fields: Any) -> None:
        self.last_seen = time.time()
        for key in ("page_url", "user_agent", "script_version", "label", "remote"):
            value = fields.get(key)
            if value:
                setattr(self, key, str(value))
        if "paused" in fields:
            self.paused = bool(fields.get("paused"))
        state = fields.get("state")
        if isinstance(state, dict) and state:
            self.state = sanitize_state(state)
            self.state_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "id": self.id,
            "label": self.label,
            "online": self.online,
            "paused": self.paused,
            "busy": self.busy,
            "page_url": self.page_url,
            "script_version": self.script_version,
            "user_agent": self.user_agent[:120],
            "remote": self.remote,
            "connected_for": int(now - self.connected_at),
            "idle_for": int(now - self.last_seen),
            "state_age": int(now - self.state_at) if self.state_at else None,
            "queued": self.queue.qsize(),
            "inflight": len(self.inflight),
            "polls": self.polls,
            "jobs_done": self.jobs_done,
            "jobs_failed": self.jobs_failed,
            "verified": bool(self.state.get("verified")),
            "has_logged_in": bool(self.state.get("has_logged_in")),
            "session_expires_in": self.state.get("session_expires_in"),
        }


# The only fields a browser may report about its session.  Anything else -- a
# cookie, a bearer token, an e-mail address -- is dropped here rather than
# trusted to a future version of the script.
STATE_FLAGS = (
    "has_arena_auth",
    "has_cf_clearance",
    "has_logged_in",
    "session_logged_in",
    "session_expired",
    "session_refreshed",
    "session_refreshable",
    "verified",
)


def sanitize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Keep the login *verdict*, drop everything that could identify a session."""
    clean: dict[str, Any] = {key: bool(state.get(key)) for key in STATE_FLAGS}
    expires_in = state.get("session_expires_in")
    try:
        clean["session_expires_in"] = (
            max(0, int(expires_in)) if expires_in is not None else None
        )
    except (TypeError, ValueError):
        clean["session_expires_in"] = None
    action = str(state.get("recaptcha_action") or "").strip()
    clean["recaptcha_action"] = action if action in {"chat_submit", "sign_up"} else (
        "chat_submit" if clean["has_logged_in"] else "sign_up"
    )
    # "assumed" means the browser could not read its own cookies (a manager
    # without GM_cookie) and reported an optimistic verdict -- keep that visible
    # so /竞技场浏览器状态 can say so instead of pretending the check was real.
    source = str(state.get("session_source") or "").strip()
    if source == "assumed":
        clean["session_source"] = "assumed"
    else:
        clean["session_source"] = "userscript" if clean["has_arena_auth"] else ""
    clean["page_url"] = str(state.get("page_url") or "")[:300]
    return clean


# --- HTTP plumbing -----------------------------------------------------------


class _Reply:
    __slots__ = ("status", "body", "content_type", "headers")

    def __init__(
        self,
        status: int = 200,
        body: bytes | str = b"",
        *,
        content_type: str = "application/json; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = int(status)
        self.body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.content_type = content_type
        self.headers = dict(headers or {})


def _json(status: int, payload: dict[str, Any]) -> _Reply:
    return _Reply(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _deny(status: int, reason: str, message: str = "") -> _Reply:
    return _json(status, {"ok": False, "error": reason, "message": message or reason})


class RelayServer:
    """The relay: one asyncio HTTP server, one job queue per connected browser.

    Owned by the plugin instance, not by the per-command client, because the
    browsers stay connected across commands.  ``open_page()`` is what makes it
    look like ``arena_direct.open_page()`` to code that has no idea a userscript
    is involved.
    """

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_RELAY_PORT,
        secret: str = "",
        data_dir: Path | str | None = None,
        public_url: str = "",
        path_prefix: str = DEFAULT_PATH_PREFIX,
        max_browsers: int = 4,
        job_timeout: float = DEFAULT_JOB_TIMEOUT,
        allow_ips: tuple[str, ...] = (),
        install_ttl: float = INSTALL_TOKEN_TTL,
        script_path: Path | str | None = None,
        logger: Any = None,
    ) -> None:
        self.host = str(host or "0.0.0.0").strip() or "0.0.0.0"
        self.port = int(port or DEFAULT_RELAY_PORT)
        self.secret = load_secret(data_dir, secret)
        self.public_url = str(public_url or "").strip().rstrip("/")
        prefix = "/" + str(path_prefix or DEFAULT_PATH_PREFIX).strip().strip("/")
        self.prefix = prefix.rstrip("/") or DEFAULT_PATH_PREFIX
        self.max_browsers = max(1, int(max_browsers or 4))
        self.job_timeout = max(15.0, float(job_timeout or DEFAULT_JOB_TIMEOUT))
        self.allow_ips = tuple(str(item).strip() for item in allow_ips if str(item).strip())
        self.install_ttl = max(300.0, float(install_ttl or INSTALL_TOKEN_TTL))
        self.script_path = Path(script_path) if script_path else _default_script_path()
        self.logger = logger
        self.started_at = 0.0
        self._server: asyncio.AbstractServer | None = None
        self._reaper: asyncio.Task | None = None
        self._browsers: dict[str, RelayBrowser] = {}
        self._nonces: dict[str, float] = {}
        self._log: list[dict[str, Any]] = []
        self._arrival = asyncio.Event()
        self._bind_error = ""

    # -- lifecycle ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def fingerprint(self) -> str:
        """A safe way to check both sides hold the same secret."""
        return hashlib.sha256(self.secret.encode("utf-8")).hexdigest()[:8]

    async def start(self) -> None:
        if self._server is not None:
            return
        try:
            self._server = await asyncio.start_server(self._handle, self.host, self.port)
        except OSError as exc:
            self._bind_error = f"{type(exc).__name__}: {exc}"
            self._note("error", f"监听失败：{exc}")
            raise _err(
                f"油猴中转服务监听 {self.host}:{self.port} 失败：{exc}"
                "（端口被占用就在插件配置里换一个 relay_port）",
                code="relay_bind_failed",
            ) from exc
        self._bind_error = ""
        self.started_at = time.time()
        self._reaper = asyncio.create_task(self._reap_forever())
        self._note("start", f"监听 {self.host}:{self.port}{self.prefix}")
        self._say("info", f"油猴中转已启动：{self.host}:{self.port}{self.prefix}")

    async def stop(self) -> None:
        reaper, self._reaper = self._reaper, None
        if reaper is not None:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reaper
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for browser in list(self._browsers.values()):
            self._abandon(browser, "relay_stopped", "油猴中转服务已停止。")
        self._browsers.clear()
        self.started_at = 0.0
        self._note("stop", "已停止")

    def _say(self, level: str, message: str) -> None:
        hook = getattr(self.logger, level, None)
        if callable(hook):
            with contextlib.suppress(Exception):
                hook("[arena_image] %s", message)

    def _note(self, kind: str, message: str, **extra: Any) -> None:
        entry: dict[str, Any] = {"at": time.time(), "kind": str(kind), "message": str(message)}
        entry.update(extra)
        self._log.append(entry)
        if len(self._log) > LOG_LIMIT:
            del self._log[: len(self._log) - LOG_LIMIT]

    # -- public surface -------------------------------------------------------

    def install_url(self, base: str = "") -> str:
        """The one-click Tampermonkey install URL, or "" if no address is known."""
        origin = str(base or self.public_url or "").strip().rstrip("/")
        if not origin:
            return ""
        if not origin.startswith(("http://", "https://")):
            origin = f"http://{origin}"
        token = install_token(self.secret, ttl=self.install_ttl)
        return f"{origin}{self.prefix}/userscript.user.js?t={token}"

    def browsers(self) -> list[RelayBrowser]:
        return sorted(self._browsers.values(), key=lambda item: item.connected_at)

    def status(self) -> dict[str, Any]:
        rows = [browser.snapshot() for browser in self.browsers()]
        return {
            "running": self.running,
            "protocol": RELAY_PROTOCOL,
            "listen": f"{self.host}:{self.port}",
            "prefix": self.prefix,
            "public_url": self.public_url,
            "secret_fingerprint": self.fingerprint,
            "uptime": int(time.time() - self.started_at) if self.started_at else 0,
            "bind_error": self._bind_error,
            "browsers": rows,
            "browser_count": len(rows),
            "online": sum(1 for row in rows if row["online"]),
            "ready": sum(1 for row in rows if row["online"] and not row["paused"]),
            "verified": sum(1 for row in rows if row["verified"]),
            "log": list(self._log[-12:]),
        }

    def pick(self) -> RelayBrowser | None:
        """The least busy ready browser; ties broken by who has waited longest.

        Round-robin rather than "first one": with two phones attached, alternating
        spreads Arena's per-account rate limit instead of hammering one session.
        """
        ready = [browser for browser in self._browsers.values() if browser.ready]
        if not ready:
            return None
        return min(
            ready,
            key=lambda item: (
                len(item.inflight) + item.queue.qsize(),
                item.last_job_at,
            ),
        )

    # -- dispatch -------------------------------------------------------------

    def _no_browser_error(self) -> BridgeError:
        link = self.install_url()
        known = list(self._browsers.values())
        if not known:
            return _err(
                "还没有浏览器接入。请在自己的电脑上装好油猴脚本、打开 arena.ai 登录，"
                "再点脚本面板里的「测试连通」。",
                code="relay_no_browser",
                browser_url=link,
            )
        if any(browser.online and browser.paused for browser in known):
            return _err(
                "接入的浏览器被暂停了，请在脚本面板里把「暂停接单」关掉。",
                code="relay_browser_paused",
                browser_url=link,
            )
        idle = min(int(time.time() - browser.last_seen) for browser in known)
        return _err(
            f"接入的浏览器已经 {idle} 秒没有心跳了，"
            "请确认那台电脑没休眠、Arena 标签页还开着。",
            code="relay_browser_offline",
            browser_url=link,
        )

    async def acquire(self, *, wait: float = 0.0) -> RelayBrowser:
        """A ready browser, waiting up to ``wait`` seconds for one to show up."""
        deadline = time.monotonic() + max(0.0, float(wait))
        while True:
            browser = self.pick()
            if browser is not None:
                return browser
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._no_browser_error()
            self._arrival.clear()
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(
                    self._arrival.wait(), timeout=min(1.0, remaining)
                )

    @contextlib.asynccontextmanager
    async def open_page(
        self,
        *,
        timeout: float = 300.0,
        wait: float = 20.0,
        browser_url: str = "",
        browser_id: str = "",
    ):
        """``arena_direct.open_page`` with a userscript behind it instead of CDP.

        The whole chain (models → state → upload → recaptcha → create-evaluation)
        is pinned to the one browser acquired here: the action ids and the
        presigned upload URL only mean anything in the tab that produced them.
        """
        if browser_id:
            browser = self._browsers.get(browser_id)
            if browser is None or not browser.online:
                raise _err("原始 Battle 浏览器已离线，请让同一台浏览器重新接入。", code="relay_browser_lost")
        else:
            browser = await self.acquire(wait=wait)
        try:
            yield RelayPage(self, browser, timeout=timeout, browser_url=browser_url)
        finally:
            browser.last_job_at = time.time()

    async def submit(
        self,
        browser: RelayBrowser,
        verb: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Queue one verb for ``browser`` and wait for its answer."""
        if verb not in VERBS:
            raise _err(f"不支持的中转指令：{verb}", code="relay_bad_verb")
        if not self.running:
            raise _err(
                "油猴中转服务没在运行，请检查插件配置里的监听端口。",
                code="relay_not_running",
            )
        if not browser.online:
            raise self._no_browser_error()
        limit = max(5.0, float(timeout if timeout is not None else self.job_timeout))
        job = RelayJob(verb, payload or {}, timeout=limit, browser_id=browser.id)
        browser.queue.put_nowait(job)
        try:
            result = await asyncio.wait_for(job.future, timeout=limit + JOB_GRACE_SECONDS)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            browser.jobs_failed += 1
            browser.inflight.pop(job.id, None)
            self._note("timeout", f"{verb} 超时", browser=browser.id, job=job.id)
            raise _err(
                f"浏览器没在 {int(limit)} 秒内回应（{verb}）。"
                "如果那台电脑刚才在休眠或者标签页被关了，重新打开 arena.ai 再试一次。",
                code="relay_job_timeout",
                browser_url=self.install_url(),
            ) from exc
        finally:
            browser.inflight.pop(job.id, None)
        browser.jobs_done += 1
        browser.last_job_at = time.time()
        return result if isinstance(result, dict) else {}

    def _abandon(self, browser: RelayBrowser, code: str, message: str) -> None:
        """Fail everything this browser was holding, queue included."""
        error = _err(message, code=code, browser_url=self.install_url())
        for job in list(browser.inflight.values()):
            job.fail(error)
        browser.inflight.clear()
        while True:
            try:
                job = browser.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            job.fail(error)

    async def _reap_forever(self) -> None:
        """Drop dead browsers and stale nonces; nothing else keeps this tidy."""
        while True:
            await asyncio.sleep(5.0)
            now = time.time()
            for browser in list(self._browsers.values()):
                if browser.online:
                    continue
                if browser.inflight or not browser.queue.empty():
                    self._abandon(
                        browser,
                        "relay_browser_lost",
                        "接单的浏览器中途失联了，这一单没能完成，请确认那台电脑还开着。",
                    )
                    self._note("lost", "浏览器失联", browser=browser.id)
                if now - browser.last_seen > BROWSER_DROP_SECONDS:
                    self._browsers.pop(browser.id, None)
            if len(self._nonces) > NONCE_LIMIT or any(
                seen < now - NONCE_TTL_SECONDS for seen in list(self._nonces.values())[:32]
            ):
                self._nonces = {
                    nonce: seen
                    for nonce, seen in self._nonces.items()
                    if seen >= now - NONCE_TTL_SECONDS
                }

    # -- http -----------------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = ""
        with contextlib.suppress(Exception):
            info = writer.get_extra_info("peername")
            peer = str(info[0]) if isinstance(info, (tuple, list)) and info else ""
        try:
            try:
                method, path, query, headers, body = await self._read_request(reader)
            except _RequestError as exc:
                reply = _deny(exc.status, exc.reason)
            except (
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
                ConnectionError,
                asyncio.TimeoutError,
                TimeoutError,
            ):
                return
            else:
                try:
                    reply = await self._route(method, path, query, headers, body, peer)
                except Exception as exc:  # a bug here must not kill the listener
                    self._note("error", f"{type(exc).__name__}: {exc}")
                    reply = _deny(500, "relay_internal_error", str(exc))
            with contextlib.suppress(Exception):
                await self._write(writer, reply)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _write(self, writer: asyncio.StreamWriter, reply: _Reply) -> None:
        reason = _HTTP_REASONS.get(reply.status, "OK")
        lines = [f"HTTP/1.1 {reply.status} {reason}"]
        if reply.status != 204:
            lines.append(f"Content-Type: {reply.content_type}")
            lines.append(f"Content-Length: {len(reply.body)}")
        lines.append("Connection: close")
        lines.append("Cache-Control: no-store")
        lines.append(f"Access-Control-Allow-Origin: {ARENA_ORIGIN}")
        lines.append("Access-Control-Allow-Headers: " + ", ".join(_AUTH_HEADERS))
        lines.append("Access-Control-Allow-Methods: GET, POST, OPTIONS")
        lines.append("Vary: Origin")
        for name, value in reply.headers.items():
            lines.append(f"{name}: {value}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        if reply.status != 204 and reply.body:
            writer.write(reply.body)
        await writer.drain()

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, dict[str, list[str]], dict[str, str], bytes]:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30.0)
        if len(head) > MAX_HEADER_BYTES:
            raise _RequestError(413, "headers_too_large")
        text = head.decode("latin-1")
        lines = text.split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2:
            raise _RequestError(400, "bad_request_line")
        method = parts[0].strip().upper()
        split = urlsplit(parts[1].strip())
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if "chunked" in headers.get("transfer-encoding", "").lower():
            # GM_xmlhttpRequest never chunks; refusing beats mis-parsing.
            raise _RequestError(400, "chunked_not_supported")
        raw_length = headers.get("content-length", "0").strip() or "0"
        if not raw_length.isdigit():
            raise _RequestError(400, "bad_content_length")
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            raise _RequestError(413, "body_too_large")
        body = b""
        if length:
            body = await asyncio.wait_for(reader.readexactly(length), timeout=180.0)
        return method, split.path or "/", parse_qs(split.query), headers, body

    def _allowed(self, peer: str) -> bool:
        if not self.allow_ips:
            return True
        address = str(peer or "")
        return any(address.startswith(prefix) for prefix in self.allow_ips)

    def _authenticate(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> str:
        """"" when the request is properly signed, else a reason to report back."""
        ts = headers.get("x-arena-ts", "").strip()
        nonce = headers.get("x-arena-nonce", "").strip()
        signature = headers.get("x-arena-sign", "").strip().lower()
        if not (ts and nonce and signature):
            return "missing_signature"
        try:
            when = float(ts)
        except ValueError:
            return "bad_timestamp"
        if abs(time.time() - when) > CLOCK_SKEW_SECONDS:
            return "clock_skew"
        if not 8 <= len(nonce) <= 64:
            return "bad_nonce"
        expected = sign_request(self.secret, method, path, ts, nonce, body)
        if not secrets.compare_digest(expected, signature):
            return "bad_signature"
        # Checked *after* the signature so an unsigned caller cannot fill the cache.
        if nonce in self._nonces:
            return "replayed_nonce"
        self._nonces[nonce] = time.time()
        return ""

    async def _route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
        peer: str,
    ) -> _Reply:
        if method == "OPTIONS":
            return _Reply(204)
        if not path.startswith(self.prefix):
            return _deny(404, "not_found")
        route = path[len(self.prefix) :].strip("/").lower()
        if route in {"userscript.user.js", "userscript.js"}:
            return self._serve_script(query)
        if not self._allowed(peer):
            self._note("auth", f"来源 {peer} 不在白名单内")
            return _deny(403, "ip_not_allowed")
        if route == "hello":
            return self._hello(method, path, headers, body)
        if method != "POST":
            return _deny(405, "method_not_allowed")
        reason = self._authenticate(method, path, headers, body)
        if reason:
            self._note("auth", f"{route or '/'} 拒绝：{reason}", remote=peer)
            return _deny(401, reason)
        payload = _load_json(body)
        if payload is None:
            return _deny(400, "bad_json")
        handlers = {"poll": self._poll, "push": self._push, "report": self._report}
        handler = handlers.get(route)
        if handler is None:
            return _deny(404, "not_found")
        try:
            return await handler(payload, peer)
        except _RequestError as exc:
            return _deny(exc.status, exc.reason, exc.message)

    def _hello(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> _Reply:
        base: dict[str, Any] = {
            "ok": True,
            "service": "arena-relay",
            "protocol": RELAY_PROTOCOL,
            "prefix": self.prefix,
            "server_time": time.time(),
            "auth": "hmac-sha256",
        }
        if not headers.get("x-arena-sign"):
            # Unsigned probe: enough for "the port is open", nothing more.
            return _json(200, {**base, "signed": False})
        reason = self._authenticate(method, path, headers, body)
        if reason:
            # 200 with ok:false on purpose -- the panel must be able to tell
            # "reachable but the secret is wrong" from "not reachable at all".
            return _json(200, {**base, "ok": False, "signed": False, "error": reason})
        status = self.status()
        return _json(
            200,
            {
                **base,
                "signed": True,
                "fingerprint": self.fingerprint,
                "browsers": status["browser_count"],
                "online": status["online"],
                "verified": status["verified"],
                "keepalive_ms": int(KEEPALIVE_HINT_SECONDS * 1000),
                "poll_hold_ms": int(POLL_HOLD_SECONDS * 1000),
                # The panel's self-test scans chunks locally; hand it the same
                # regex the real "actions" job uses so there is one source of truth.
                "action_pattern": _NEXT_ACTION_RE,
            },
        )

    def _register(self, payload: dict[str, Any], peer: str) -> RelayBrowser:
        browser_id = str(payload.get("browser_id") or "").strip()[:64]
        if not browser_id:
            raise _RequestError(400, "missing_browser_id")
        browser = self._browsers.get(browser_id)
        if browser is None:
            live = [item for item in self._browsers.values() if item.online]
            if len(live) >= self.max_browsers:
                raise _RequestError(
                    409,
                    "too_many_browsers",
                    f"已经接入 {self.max_browsers} 个浏览器了，"
                    "在插件配置里调大 relay_max_browsers 或先关掉一个。",
                )
            browser = RelayBrowser(browser_id, label=str(payload.get("label") or ""))
            self._browsers[browser_id] = browser
            self._note("join", f"浏览器接入：{browser.label or browser_id}", browser=browser_id)
            self._say("info", f"油猴浏览器已接入：{browser.label or browser_id}")
        fields: dict[str, Any] = {
            "page_url": payload.get("page_url"),
            "user_agent": payload.get("user_agent"),
            "script_version": payload.get("version"),
            "label": payload.get("label"),
            "remote": peer,
            "state": payload.get("state"),
        }
        if "paused" in payload:
            fields["paused"] = bool(payload.get("paused"))
        browser.touch(**fields)
        return browser

    async def _poll(self, payload: dict[str, Any], peer: str) -> _Reply:
        browser = self._register(payload, peer)
        browser.polls += 1
        self._arrival.set()
        if browser.paused:
            return _json(200, {"ok": True, "job": None, "paused": True})
        hold = float(payload.get("hold") or POLL_HOLD_SECONDS)
        deadline = time.monotonic() + min(POLL_HOLD_SECONDS, max(1.0, hold))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _Reply(204)
            try:
                job = await asyncio.wait_for(browser.queue.get(), timeout=remaining)
            except (asyncio.TimeoutError, TimeoutError):
                return _Reply(204)
            if job.future.done():
                # The command that queued this gave up (or was cancelled) while it
                # waited; handing it out would waste a round-trip in someone's tab.
                continue
            job.leased_at = time.time()
            browser.inflight[job.id] = job
            browser.last_seen = time.time()
            return _json(200, {"ok": True, "job": job.wire()})

    async def _push(self, payload: dict[str, Any], peer: str) -> _Reply:
        browser = self._register(payload, peer)
        job_id = str(payload.get("job_id") or "")
        job = browser.inflight.get(job_id)
        if payload.get("keepalive"):
            # Still working.  The heartbeat itself is the touch() in _register.
            return _json(200, {"ok": True, "known": bool(job)})
        if job is None:
            # A late answer after the relay stopped waiting: accepted so the
            # script stops retrying, but there is nobody left to resolve.
            return _json(200, {"ok": True, "stale": True})
        browser.inflight.pop(job_id, None)
        if payload.get("error"):
            browser.jobs_failed += 1
            job.fail(self._job_error(job, payload))
        else:
            result = payload.get("result")
            job.resolve(result if isinstance(result, dict) else {"value": result})
        return _json(200, {"ok": True})

    async def _report(self, payload: dict[str, Any], peer: str) -> _Reply:
        browser = self._register(payload, peer)
        kind = str(payload.get("kind") or "state")[:24]
        message = str(payload.get("message") or "")[:160]
        self._note("report", f"{kind}：{message}" if message else kind, browser=browser.id)
        return _json(200, {"ok": True, "kind": kind, "state": browser.state})

    def _job_error(self, job: RelayJob, payload: dict[str, Any]) -> BridgeError:
        code = str(payload.get("code") or "").strip()
        if code not in _JOB_CODES:
            code = "browser_job_failed"
        message = str(payload.get("error") or "浏览器执行失败")[:400]
        status = payload.get("status")
        self._note("fail", f"{job.verb}：{message}", job=job.id, browser=job.browser_id)
        return _err(
            f"浏览器执行「{job.verb}」失败：{message}",
            code=code,
            status_code=int(status) if isinstance(status, (int, float)) and status else None,
            browser_url=self.install_url(),
        )

    def _serve_script(self, query: dict[str, list[str]]) -> _Reply:
        token = (query.get("t") or [""])[0]
        if not check_install_token(self.secret, token):
            return _deny(
                403,
                "bad_install_token",
                "安装链接无效或已过期，请重新运行 /竞技场浏览器接入 拿一条新的。",
            )
        try:
            template = self.script_path.read_text(encoding="utf-8")
        except OSError as exc:
            self._note("error", f"读不到脚本文件：{exc}")
            return _deny(500, "script_missing", f"读不到脚本文件：{self.script_path}")
        base = f"{self.public_url}{self.prefix}" if self.public_url else self.prefix
        host = urlsplit(self.public_url or "").hostname or ""
        script = render_script(
            template,
            base=base,
            host="".join(ch for ch in host if ch.isalnum() or ch in ".-_") or "*",
            secret=self.secret,
            fingerprint=self.fingerprint,
        )
        self._note("install", "已下发脚本")
        return _Reply(
            200, script, content_type="application/javascript; charset=utf-8"
        )


class _RequestError(Exception):
    """A malformed or unacceptable request, carrying the status to answer with."""

    def __init__(self, status: int, reason: str, message: str = "") -> None:
        super().__init__(reason)
        self.status = int(status)
        self.reason = str(reason)
        self.message = str(message or "")


# Codes a browser may set on a failure; anything else becomes browser_job_failed
# so a compromised or buggy script cannot invent hint text for the operator.
_JOB_CODES = frozenset(
    {
        "browser_job_failed",
        "browser_verb_unsupported",
        "arena_verification_required",
        "browser_offline",
        "browser_busy",
        "recaptcha_unavailable",
        "model_table_empty",
        "page_navigation_failed",
        "job_timeout",
        "job_aborted",
    }
)
_AUTH_HEADERS = (
    "Content-Type",
    "X-Arena-Ts",
    "X-Arena-Nonce",
    "X-Arena-Sign",
    "X-Arena-Browser",
)


def _load_json(body: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads((body or b"{}").decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _default_script_path() -> Path:
    return Path(__file__).resolve().parent / "userscript" / "arena-relay.user.js"


def _js_text(value: Any) -> str:
    """Escape a value for pasting *inside* a JS string literal in the template."""
    # The template quotes with ' , which json.dumps leaves alone -- escape it too.
    return json.dumps(str(value), ensure_ascii=False)[1:-1].replace("'", "\\'")


def render_script(
    template: str, *, base: str, host: str, secret: str, fingerprint: str
) -> str:
    swaps = {
        "__RELAY_BASE__": _js_text(base),
        "__RELAY_HOST__": host,
        "__RELAY_SECRET__": _js_text(secret),
        "__RELAY_FINGERPRINT__": _js_text(fingerprint),
        "__RELAY_PROTOCOL__": str(RELAY_PROTOCOL),
        "__RELAY_POLL_MS__": str(int(POLL_HOLD_SECONDS * 1000)),
        "__RELAY_KEEPALIVE_MS__": str(int(KEEPALIVE_HINT_SECONDS * 1000)),
        "__ARENA_PAGE_PATH__": _js_text(ARENA_PAGE_PATH),
    }
    for token, value in swaps.items():
        template = template.replace(token, value)
    return template


# --- the page ----------------------------------------------------------------


class RelayPage:
    """``ArenaPage``'s coroutines, served by a userscript instead of by CDP.

    Same names, same signatures, same return shapes, so ``ArenaDirectClient`` --
    which is where the entire Arena protocol lives -- runs unmodified on top of
    it.  Two methods deliberately differ in kind rather than in shape:

    * ``cookies()`` always returns nothing.  Cookies stay in the visitor's
      browser; that is the point of this transport.
    * ``evaluate()`` refuses.  There is no verb for arbitrary JavaScript, so a
      compromised plugin cannot run code in someone's signed-in Arena tab.
    """

    def __init__(
        self,
        relay: RelayServer,
        browser: RelayBrowser,
        *,
        timeout: float = 300.0,
        browser_url: str = "",
    ) -> None:
        self.relay = relay
        self.browser = browser
        self.timeout = max(15.0, float(timeout or 300.0))
        self.browser_url = str(browser_url or "")
        self.url = browser.page_url or f"{ARENA_ORIGIN}{ARENA_PAGE_PATH}"
        # Per-browser cache bucket: action ids and upload receipts from one tab
        # are meaningless in another.  See arena_direct.cache_scope().
        self.cache_key = f"relay:{browser.id}"
        self.session_id = browser.id
        self.target_id = ""

    async def _job(
        self,
        verb: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self.relay.submit(
            self.browser, verb, payload or {}, timeout=timeout
        )

    async def evaluate(self, expression: str, *, timeout: float | None = None) -> Any:
        raise _err(
            "油猴中转不提供任意脚本执行，只支持固定的几个指令。",
            code="relay_eval_forbidden",
        )

    async def live_state(self) -> dict[str, Any]:
        """The login verdict, decided inside the visitor's own browser.

        ``arena_direct._live_state`` calls this instead of reading cookies when the
        transport provides it, which is how the Arena session cookie stays out of
        the server entirely.
        """
        result = await self._job("state", timeout=45.0)
        raw = result.get("state")
        state = sanitize_state(raw if isinstance(raw, dict) else result)
        self.browser.state = state
        self.browser.state_at = time.time()
        if state.get("page_url"):
            self.url = str(state["page_url"])
            self.browser.page_url = self.url
        return state

    async def cookies(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """Never any: the relay has no verb that can read a cookie."""
        return []

    async def auth_token(self, *, refresh: bool = False) -> str:
        return ""

    async def refresh_session(self, *, budget: float = 0.0) -> str:
        """Nothing to rotate here -- the tab refreshes its own token."""
        return ""

    async def sitekey(self) -> str:
        return ""

    async def reload(self, *, budget: float = 45.0) -> bool:
        try:
            result = await self._job("reload", timeout=max(30.0, float(budget or 45.0)))
        except BridgeError:
            return False
        return bool(result.get("ok"))

    async def mint_recaptcha(self, action: str) -> str:
        """Empty is tolerated upstream, so a failure here is not fatal."""
        try:
            result = await self._job(
                "recaptcha", {"action": str(action or "chat_submit")}, timeout=60.0
            )
        except BridgeError:
            return ""
        return str(result.get("token") or "")

    async def fetch(
        self,
        path: str,
        *,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        body: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """One same-origin request performed by the page, cookies and all."""
        limit = float(timeout if timeout is not None else self.timeout)
        result = await self._job(
            "fetch",
            {
                "path": str(path),
                "method": str(method or "POST").upper(),
                "headers": {str(k): str(v) for k, v in (headers or {}).items()},
                "body": None if body is None else str(body),
            },
            timeout=limit,
        )
        return {
            "status": int(result.get("status") or 0),
            "headers": result.get("headers") if isinstance(result.get("headers"), dict) else {},
            "text": str(result.get("text") or ""),
        }

    async def history_json(self, path: str, *, timeout: float) -> dict[str, Any]:
        # Reuse the fixed fetch verb, not arbitrary JS. Restrict this adapter to
        # the exact read-only endpoints accepted by HistoryModelCatalog._read.
        response = await self.fetch(
            path, method="GET", headers={"Accept": "application/json"}, timeout=timeout,
        )
        result = {"status": response["status"],
                  "retry_after": response["headers"].get("retry-after"), "body": None}
        if response["status"] != 200:
            return result
        try:
            body = json.loads(response["text"])
        except (ValueError, TypeError):
            return result
        if not isinstance(body, dict):
            return result
        if path.startswith("/api/history/unified?") and isinstance(body.get("entries"), list):
            result["body"] = {
                "entries": [{key: row.get(key) for key in (
                    "id", "modelAId", "modelBId", "modelAOrganization", "modelBOrganization",
                )} for row in body["entries"] if isinstance(row, dict)],
                "pagination": body.get("pagination"),
            }
        elif isinstance(body.get("revealedModels"), list):
            result["body"] = {"revealedModels": body["revealedModels"]}
        return result

    async def model_table(self) -> list[dict[str, Any]]:
        result = await self._job("models", timeout=90.0)
        if not result.get("ok"):
            detail = str(result.get("error") or result.get("status") or "")
            raise _err(
                f"没能从接入的浏览器读到模型列表{('：' + detail) if detail else ''}",
                code="model_table_empty",
                browser_url=self.browser_url,
            )
        return parse_model_table(str(result.get("json") or ""))

    async def next_actions(self) -> dict[str, str]:
        # The pattern travels with the job so the regex stays defined in exactly
        # one place: arena_direct._NEXT_ACTION_RE, next to the CDP transport that
        # uses the same one.
        result = await self._job(
            "actions", {"pattern": _NEXT_ACTION_RE}, timeout=120.0
        )

        actions = result.get("actions")
        if not isinstance(actions, dict):
            return {}
        return {str(key): str(value) for key, value in actions.items() if key and value}


# --- the client --------------------------------------------------------------


class ArenaUserscriptClient(ArenaDirectClient):
    """``ArenaDirectClient`` with the userscript relay under it.

    Everything that matters -- the model table, the reCAPTCHA action, the
    create-evaluation body, the three-step reference-image upload, the stream
    parser, the model health window -- is inherited unchanged.  Only the six
    transport methods and the operator-facing wording differ, because the browser
    is now somebody's own Chrome instead of a container.
    """

    def __init__(
        self,
        relay: RelayServer,
        *,
        timeout: float = 300.0,
        rate_limit_retries: int = 2,
        rate_limit_max_wait: float = 30.0,
        allow_stealth_models: bool = True,
        allowed_stealth_models: frozenset[str] | None = None,
        data_dir: Path | str | None = None,
        acquire_wait: float = 20.0,
    ) -> None:
        super().__init__(
            # A sentinel, not an address: the parent refuses an empty CDP url and
            # nothing in this subclass ever opens a CDP connection.
            "userscript://relay",
            timeout=timeout,
            rate_limit_retries=rate_limit_retries,
            rate_limit_max_wait=rate_limit_max_wait,
            allow_stealth_models=allow_stealth_models,
            allowed_stealth_models=allowed_stealth_models,
            data_dir=data_dir,
        )
        self.relay = relay
        self.acquire_wait = max(0.0, float(acquire_wait))

    def _history_for_page(self, page: RelayPage):
        # No history metadata is shared between accounts/browser identities.
        directory = (self._data_dir / "relay-catalogs" /
                     hashlib.sha256(page.cache_key.encode("utf-8")).hexdigest()[:24]
                     if self._data_dir else None)
        self._history_catalog = history_catalog(page.cache_key, directory)
        return self._history_catalog

    def _link(self) -> str:
        return self.relay.install_url()

    def _page(self, *, timeout: float | None = None):
        return self.relay.open_page(
            timeout=float(timeout if timeout is not None else self.timeout),
            wait=self.acquire_wait,
            browser_url=self._link(),
        )

    def _battle_page(self, context: dict):
        scope = str(context.get("browser_scope") or "")
        if not scope.startswith("relay:"):
            raise _err("这场 Battle 不属于当前油猴通道。", code="relay_browser_lost")
        return self.relay.open_page(
            timeout=self.timeout, wait=self.acquire_wait,
            browser_url=self._link(), browser_id=scope[len("relay:"):],
        )

    async def health(self) -> dict[str, Any]:
        """Relay state first, browser state second -- both are failure modes."""
        status = self.relay.status()
        payload: dict[str, Any] = {
            "status": "degraded",
            "transport": "userscript-relay",
            "browser": "connected" if status["ready"] else "offline",
            "relay": {
                key: status[key]
                for key in (
                    "running",
                    "listen",
                    "prefix",
                    "public_url",
                    "browser_count",
                    "online",
                    "ready",
                    "verified",
                    "uptime",
                    "secret_fingerprint",
                )
            },
            "browsers": status["browsers"],
        }
        if not status["running"]:
            payload["message"] = (
                "油猴中转服务没有在运行。"
                + (f"监听失败：{status['bind_error']}" if status["bind_error"] else "")
            )
            return payload
        if not status["ready"]:
            payload["message"] = (
                "还没有可用的浏览器接入。请私聊运行 /竞技场浏览器接入 装脚本，"
                "然后打开 arena.ai 登录，再点脚本面板里的「测试连通」。"
            )
            return payload
        try:
            async with self._page(timeout=60.0) as page:
                state = await _live_state(page)
        except BridgeError as exc:
            payload["message"] = str(exc)
            return payload
        healthy = bool(state.get("has_logged_in"))
        payload["status"] = "healthy" if healthy else "degraded"
        payload["page_url"] = state.get("page_url") or ""
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
                "浏览器接上了，但 Arena 还是匿名会话，请在那台电脑上登录 Arena 账号。"
            )
        return payload

    async def start_interactive_auth(self) -> dict[str, Any]:
        """"Verification" here means: install the script, then log in yourself."""
        link = self._link()
        if not link:
            raise _err(
                "拿不到安装链接：插件不知道自己对外的地址。请在插件配置里填"
                "「油猴中转对外地址」（relay_public_url），"
                f"例如 http://服务器IP:{self.relay.port}",
                code="relay_public_url_missing",
            )
        now = time.time()
        record = {
            "session_id": uuid.uuid4().hex,
            "status": "waiting",
            "created_at": now,
            "expires_at": now + self.relay.install_ttl,
            "browser_url": link,
        }
        return self._auth_payload(record, await self._current_state())

    def _auth_payload(
        self, record: dict[str, Any], state: dict[str, Any] | None
    ) -> dict[str, Any]:
        payload = super()._auth_payload(record, state)
        status = self.relay.status()
        payload["transport"] = "userscript-relay"
        # Always the current install link: a record left over from another
        # transport must never hand out a noVNC address here.
        payload["browser_url"] = self._link()
        payload.pop("vnc_url", None)
        payload["relay_browsers"] = status["browser_count"]
        payload["relay_online"] = status["online"]
        payload["relay_ready"] = status["ready"]
        return payload

    def _auth_message(self, status: str, state: dict[str, Any]) -> str:
        online = self.relay.status()["ready"]
        if status == "verified":
            return "浏览器已接入并且登录了 Arena。画图是否可用以实际生成请求为准。"
        if status == "expired":
            return "安装链接过期了，重新运行 /竞技场浏览器接入 拿一条新的。"
        if not online:
            return (
                "还没有浏览器接入：请用下面的链接安装油猴脚本，"
                "然后打开 arena.ai 登录，再点脚本面板里的「测试连通」。"
            )
        if state.get("session_refreshable"):
            return "浏览器在线，登录还在，只是访问令牌过期了，过一会儿再发一次命令即可。"
        if state.get("has_arena_auth") and not state.get("has_logged_in"):
            return "浏览器在线，但 Arena 还是匿名状态，请在那台电脑上登录 Arena 账号。"
        return "浏览器在线，但还没拿到 Arena 会话，请在那台电脑上打开 arena.ai 登录一次。"

    def _require_session(self, state: dict[str, Any], browser_url: str) -> None:
        """The parent's checks with wording that fits somebody's own browser."""
        if state.get("session_expired"):
            if state.get("session_refreshable"):
                raise _err(
                    "登录还在，但访问令牌过期了，这次没自动续上。\n"
                    "过十几秒再发一次命令通常就好了；一直这样就在那台电脑上刷新一下 arena.ai。",
                    code="arena_auth_stale",
                    browser_url=browser_url,
                )
            raise _err(
                "接入的浏览器里 Arena 会话已过期。\n请在那台电脑上重新登录一次 arena.ai。",
                code="arena_auth_expired",
                browser_url=browser_url,
            )
        if not state.get("has_arena_auth"):
            raise _err(
                "接入的浏览器里没有 Arena 会话。\n"
                "请在那台电脑上打开 arena.ai 登录，登录后脚本面板会显示「已登录」。",
                code="arena_auth_required",
                browser_url=browser_url,
            )
        if not state.get("has_logged_in"):
            raise _err(
                "Arena 现在是匿名会话，登录后才能出图。\n"
                "请在那台电脑的 arena.ai 上用 Google 或邮箱登录。",
                # Same code as the CDP transport: main.py keys its "go log in"
                # hint off it, and an anonymous session is not a missing one.
                code="arena_login_required",
                browser_url=browser_url,
            )
