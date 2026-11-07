"""Regression tests for the userscript relay transport.

Everything here runs against a real socket with a fake browser on the other end,
because the point of this transport is that the browser is *not* in our process.
Two classes of invariant are locked:

* **Feature parity** -- the model table, the login gate, text-to-image and the
  three-step reference-image upload must all work through the relay's six verbs,
  since ``ArenaDirectClient`` is inherited whole and only the transport differs.
* **Privacy** -- no cookie may cross the wire, and no verb may run arbitrary JS
  in a visitor's signed-in Arena tab.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import socket
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Callable

import httpx

from astrbot_plugin_arena_image import arena_direct, arena_relay, bridge_client

from tests.test_arena_direct_transport import (
    DirectTransportCase,
    FakeHTTPX,
    _chunked_cookies,
    _filler_models,
    _image_model,
    _png_data_url,
    _session_token,
)
from tests.test_arena_image_hardening import FakeEvent, _collect, _make_plugin

SCRIPT_PATH = Path(arena_relay.__file__).resolve().parent / "userscript" / "arena-relay.user.js"
SECRET = "test-secret-0123456789abcdef"

LOGGED_IN = {
    "has_arena_auth": True,
    "has_cf_clearance": True,
    "has_logged_in": True,
    "session_logged_in": True,
    "session_expired": False,
    "session_refreshed": False,
    "session_refreshable": False,
    "session_source": "userscript",
    "session_expires_in": 3600,
    "recaptcha_action": "chat_submit",
    "verified": True,
    "page_url": "https://arena.ai/text/direct",
}
ANONYMOUS = {
    **LOGGED_IN,
    "has_logged_in": False,
    "session_logged_in": False,
    "recaptcha_action": "sign_up",
    "verified": False,
}

ARENA_IMAGE = "https://cdn.arena.ai/generated/apple.png"
STREAM_BODY = (
    'a2:[{"type":"image","image":"https://cdn.arena.ai/generated/apple.png"}]\n'
    'ad:{"finishReason":"stop"}'
)


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextlib.contextmanager
def _captured_logs(logger: Any, sink: list[str]):
    """Collect formatted log lines so a secret in a log line fails a test."""

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            sink.append(record.getMessage())

    handler = _Sink()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _models_json() -> str:
    rows = [_image_model("nano-banana", "11111111-1111-7111-8111-111111111111"), *_filler_models()]
    return json.dumps(rows)


UPLOAD_ACTION = "7f" + "a" * 38
SIGNED_ACTION = "7f" + "b" * 38
ACTIONS = {"generateUploadUrl": UPLOAD_ACTION, "getSignedUrl": SIGNED_ACTION}


class FakeBrowser:
    """A userscript stand-in: polls over real HTTP, answers the six verbs.

    Deliberately not a Python port of the script -- it only has to speak the
    wire, so a protocol change on either side shows up as a failing test here.
    """

    def __init__(
        self,
        relay: arena_relay.RelayServer,
        *,
        browser_id: str = "browser-a",
        label: str = "Win-a",
        state: dict[str, Any] | None = None,
        paused: bool = False,
        actions: dict[str, str] | None = None,
    ) -> None:
        self.relay = relay
        self.id = browser_id
        self.label = label
        self.state = dict(state if state is not None else LOGGED_IN)
        self.paused = bool(paused)
        self.actions = dict(actions if actions is not None else ACTIONS)
        self.seen: list[tuple[str, dict[str, Any]]] = []
        self.fetches: list[dict[str, Any]] = []
        self.verbs: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self.base = f"http://127.0.0.1:{relay.port}{relay.prefix}"
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._stop = False

    # -- wire -----------------------------------------------------------------

    async def call(self, route: str, payload: dict[str, Any] | None = None, *, method: str = "POST"):
        assert self._client is not None
        path = f"{self.relay.prefix}/{route}"
        body = b"" if method == "GET" else json.dumps(payload or {}).encode("utf-8")
        ts = str(int(time.time()))
        nonce = secrets.token_hex(8)
        sign = arena_relay.sign_request(self.relay.secret, method, path, ts, nonce, body)
        return await self._client.request(
            method,
            f"{self.base}/{route}",
            content=body or None,
            headers={
                "Content-Type": "application/json",
                "X-Arena-Ts": ts,
                "X-Arena-Nonce": nonce,
                "X-Arena-Sign": sign,
                "X-Arena-Browser": self.id,
            },
        )

    def fields(self) -> dict[str, Any]:
        return {
            "browser_id": self.id,
            "label": self.label,
            "page_url": self.state.get("page_url") or "https://arena.ai/text/direct",
            "user_agent": "FakeBrowser/1.0",
            "version": "0.1.0",
            "paused": self.paused,
            "state": self.state,
        }

    # -- verbs ----------------------------------------------------------------

    def default_fetch(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = str(payload.get("path") or "")
        if path == "/api/storage/generate-upload-url":
            return {
                "status": 200,
                "headers": {},
                "text": json.dumps({"uploadUrl": "https://r2.example/put", "key": "up/abc"}),
            }
        if path == "/api/storage/get-signed-url":
            return {
                "status": 200,
                "headers": {},
                "text": json.dumps({"url": "https://cdn.arena.ai/ref.png?X-Amz-Expires=3600"}),
            }
        if path == arena_direct.STREAM_CREATE_EVALUATION_PATH:
            return {"status": 200, "headers": {}, "text": STREAM_BODY}
        return {"status": 404, "headers": {}, "text": "not found"}

    async def run(self, verb: str, payload: dict[str, Any]) -> Any:
        override = self.verbs.get(verb)
        if override is not None:
            result = override(payload)
            return await result if asyncio.iscoroutine(result) else result
        if verb == "state":
            return {"state": self.state}
        if verb == "models":
            return {"ok": True, "status": 200, "json": _models_json()}
        if verb == "actions":
            return {"actions": dict(self.actions), "scanned": 12}
        if verb == "recaptcha":
            return {"token": "recaptcha-token", "action": payload.get("action")}
        if verb == "reload":
            return {"ok": True, "reloading": True}
        if verb == "fetch":
            self.fetches.append(payload)
            return self.default_fetch(payload)
        raise RuntimeError(f"unsupported verb {verb}")

    # -- loop -----------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop:
            try:
                reply = await self.call("poll", {**self.fields(), "hold": 2})
            except (httpx.HTTPError, RuntimeError):
                if self._stop:
                    return
                await asyncio.sleep(0.05)
                continue
            if reply.status_code != 200:
                await asyncio.sleep(0.02)
                continue
            job = (reply.json() or {}).get("job")
            if not job:
                continue
            self.seen.append((str(job.get("verb")), dict(job.get("payload") or {})))
            try:
                result = await self.run(str(job.get("verb")), dict(job.get("payload") or {}))
            except Exception as exc:  # the script reports failures, it never dies
                await self.call(
                    "push",
                    {**self.fields(), "job_id": job.get("job_id"), "error": str(exc)},
                )
                continue
            await self.call(
                "push", {**self.fields(), "job_id": job.get("job_id"), "result": result}
            )

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=30.0)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class RelayCase(DirectTransportCase):
    """A live relay on loopback, with fake browsers attached to it."""

    @contextlib.asynccontextmanager
    async def relay(
        self,
        *,
        browsers: int = 1,
        state: dict[str, Any] | None = None,
        paused: bool = False,
        **kwargs: Any,
    ):
        port = _free_port()
        options: dict[str, Any] = {
            "host": "127.0.0.1",
            "port": port,
            "secret": SECRET,
            "data_dir": self.data_dir,
            "public_url": f"http://127.0.0.1:{port}",
            "script_path": SCRIPT_PATH,
        }
        options.update(kwargs)
        server = arena_relay.RelayServer(**options)
        await server.start()
        fakes: list[FakeBrowser] = []
        try:
            for index in range(browsers):
                fake = FakeBrowser(
                    server,
                    browser_id=f"browser-{index}",
                    label=f"Win-{index}",
                    state=state,
                    paused=paused,
                )
                await fake.start()
                fakes.append(fake)
            if fakes:
                await self.attached(server, len(fakes), ready=not paused)
            yield server, fakes
        finally:
            for fake in fakes:
                await fake.stop()
            await server.stop()

    async def attached(
        self, server: arena_relay.RelayServer, count: int, *, ready: bool = True
    ) -> None:
        key = "ready" if ready else "browser_count"
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if int(server.status()[key]) >= count:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"only {server.status()} attached, wanted {count} {key}")

    def user_client(self, server: arena_relay.RelayServer, **kwargs: Any):
        options: dict[str, Any] = {"data_dir": self.data_dir, "acquire_wait": 5.0}
        options.update(kwargs)
        return arena_relay.ArenaUserscriptClient(server, **options)


class SigningTests(unittest.TestCase):
    """The canonical string is a contract with the JavaScript half."""

    def test_canonical_string_is_exactly_five_newline_joined_fields(self) -> None:
        digest = arena_relay.body_digest(b'{"a":1}')
        canonical = arena_relay.canonical_request("post", "/arena/poll", "1700000000", "abcd1234", digest)
        self.assertEqual(
            canonical,
            "\n".join(("POST", "/arena/poll", "1700000000", "abcd1234", digest)),
        )
        self.assertEqual(digest, hashlib.sha256(b'{"a":1}').hexdigest())

    def test_signature_matches_a_hand_rolled_hmac(self) -> None:
        body = b'{"browser_id":"x"}'
        expected = hmac.new(
            SECRET.encode(),
            arena_relay.canonical_request(
                "POST", "/arena/push", "1700000000", "nonce1234", arena_relay.body_digest(body)
            ).encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            arena_relay.sign_request(SECRET, "POST", "/arena/push", 1700000000, "nonce1234", body),
            expected,
        )

    def test_an_empty_body_signs_like_the_scripts_empty_string(self) -> None:
        self.assertEqual(
            arena_relay.body_digest(b""), hashlib.sha256(b"").hexdigest()
        )

    def test_install_token_survives_a_round_trip_but_not_a_tweak(self) -> None:
        token = arena_relay.install_token(SECRET)
        self.assertTrue(arena_relay.check_install_token(SECRET, token))
        self.assertFalse(arena_relay.check_install_token(SECRET + "x", token))
        flipped = token[:-1] + ("1" if token[-1] == "0" else "0")
        self.assertFalse(arena_relay.check_install_token(SECRET, flipped))
        self.assertFalse(arena_relay.check_install_token(SECRET, "not.a.token"))

    def test_an_expired_install_link_stops_serving_the_script(self) -> None:
        stale_at = int(time.time()) - 5
        nonce = "deadbeefdeadbeef"
        signature = hmac.new(
            SECRET.encode(), f"install.{stale_at}.{nonce}".encode(), hashlib.sha256
        ).hexdigest()[:32]
        self.assertFalse(
            arena_relay.check_install_token(SECRET, f"{stale_at}.{nonce}.{signature}")
        )

    def test_a_configured_secret_is_only_honoured_when_long_enough(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            short = arena_relay.load_secret(temp, "abc")
            self.assertNotEqual(short, "abc")
            self.assertGreaterEqual(len(short), 16)
            # Generated once, then reused: reinstalling the script must not
            # invalidate the copy already sitting in someone's Tampermonkey.
            self.assertEqual(short, arena_relay.load_secret(temp, ""))
            self.assertEqual(arena_relay.load_secret(temp, SECRET), SECRET)


class HttpSurfaceTests(RelayCase):
    """What an unauthenticated stranger on the port can and cannot do."""

    def test_an_unsigned_probe_says_hello_but_nothing_else(self) -> None:
        async def scenario() -> None:
            async with self.relay(browsers=0) as (server, _fakes):
                async with httpx.AsyncClient(timeout=10.0) as client:
                    hello = await client.get(f"http://127.0.0.1:{server.port}/arena/hello")
                    poll = await client.post(
                        f"http://127.0.0.1:{server.port}/arena/poll", json={"browser_id": "x"}
                    )
            self.assertEqual(hello.status_code, 200)
            self.assertFalse(hello.json()["signed"])
            self.assertNotIn("fingerprint", hello.json())
            self.assertEqual(poll.status_code, 401)
            self.assertEqual(poll.json()["error"], "missing_signature")

        self.run_async(scenario())

    def test_a_wrong_secret_is_reported_as_wrong_not_as_unreachable(self) -> None:
        async def scenario() -> None:
            async with self.relay(browsers=0) as (server, _fakes):
                async with httpx.AsyncClient(timeout=10.0) as client:
                    path = "/arena/hello"
                    ts = str(int(time.time()))
                    sign = arena_relay.sign_request("wrong-secret-xxxxxxxxxx", "GET", path, ts, "nonce1234", b"")
                    hello = await client.get(
                        f"http://127.0.0.1:{server.port}{path}",
                        headers={"X-Arena-Ts": ts, "X-Arena-Nonce": "nonce1234", "X-Arena-Sign": sign},
                    )
            # 200 on purpose: the panel needs to tell these two apart.
            self.assertEqual(hello.status_code, 200)
            self.assertFalse(hello.json()["ok"])
            self.assertEqual(hello.json()["error"], "bad_signature")

        self.run_async(scenario())

    def test_a_signed_hello_carries_what_the_panel_needs(self) -> None:
        async def scenario() -> dict[str, Any]:
            async with self.relay() as (server, fakes):
                reply = await fakes[0].call("hello", method="GET")
                return reply.json()

        info = self.run_async(scenario())
        self.assertTrue(info["ok"])
        self.assertTrue(info["signed"])
        self.assertEqual(info["protocol"], arena_relay.RELAY_PROTOCOL)
        self.assertEqual(len(info["fingerprint"]), 8)
        self.assertEqual(info["online"], 1)
        # The self-test scans chunks with the server's own regex, never a copy.
        self.assertEqual(info["action_pattern"], arena_direct._NEXT_ACTION_RE)

    def test_replay_and_skew_are_both_refused(self) -> None:
        async def scenario() -> tuple[int, str, int, str]:
            async with self.relay(browsers=0) as (server, _fakes):
                path = "/arena/report"
                body = json.dumps({"browser_id": "replayer"}).encode()
                nonce = "same-nonce-1"
                ts = str(int(time.time()))
                sign = arena_relay.sign_request(SECRET, "POST", path, ts, nonce, body)
                headers = {
                    "Content-Type": "application/json",
                    "X-Arena-Ts": ts,
                    "X-Arena-Nonce": nonce,
                    "X-Arena-Sign": sign,
                }
                old_ts = str(int(time.time()) - 4000)
                old_sign = arena_relay.sign_request(SECRET, "POST", path, old_ts, "nonce-old-1", body)
                async with httpx.AsyncClient(timeout=10.0) as client:
                    url = f"http://127.0.0.1:{server.port}{path}"
                    first = await client.post(url, content=body, headers=headers)
                    again = await client.post(url, content=body, headers=headers)
                    stale = await client.post(
                        url,
                        content=body,
                        headers={
                            **headers,
                            "X-Arena-Ts": old_ts,
                            "X-Arena-Nonce": "nonce-old-1",
                            "X-Arena-Sign": old_sign,
                        },
                    )
            return first.status_code, again.json()["error"], stale.status_code, stale.json()["error"]

        first, replay, stale_status, skew = self.run_async(scenario())
        self.assertEqual(first, 200)
        self.assertEqual(replay, "replayed_nonce")
        self.assertEqual(stale_status, 401)
        self.assertEqual(skew, "clock_skew")


class ScriptServingTests(RelayCase):
    """The install link is the only unsigned route, so it carries its own token."""

    def test_the_served_script_is_fully_rendered(self) -> None:
        async def scenario() -> tuple[int, str]:
            async with self.relay(browsers=0) as (server, _fakes):
                async with httpx.AsyncClient(timeout=10.0) as client:
                    reply = await client.get(server.install_url())
            return reply.status_code, reply.text

        status, text = self.run_async(scenario())
        self.assertEqual(status, 200)
        self.assertNotIn("__RELAY_", text)
        self.assertNotIn("__ARENA_", text)
        self.assertIn(f"secret: '{SECRET}'", text)
        self.assertIn("@grant        GM_xmlhttpRequest", text)
        self.assertIn("@match        https://arena.ai/*", text)
        self.assertIn("@connect      127.0.0.1", text)
        self.assertIn(f"protocol: {arena_relay.RELAY_PROTOCOL}", text)

    def test_a_bad_token_serves_nothing(self) -> None:
        async def scenario() -> tuple[int, dict[str, Any]]:
            async with self.relay(browsers=0) as (server, _fakes):
                async with httpx.AsyncClient(timeout=10.0) as client:
                    reply = await client.get(
                        f"http://127.0.0.1:{server.port}/arena/userscript.user.js?t=nope"
                    )
            return reply.status_code, reply.json()

        status, payload = self.run_async(scenario())
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_install_token")


class FeatureParityTests(RelayCase):
    """Every plugin feature has to survive the transport swap, not just /jjc."""

    MODEL = "nano-banana"

    def test_the_model_list_arrives_through_the_relay(self) -> None:
        async def scenario():
            async with self.relay() as (server, fakes):
                rows = await self.user_client(server).list_models()
                return rows, [verb for verb, _ in fakes[0].seen], list(fakes[0].fetches)

        rows, verbs, fetches = self.run_async(scenario())
        self.assertIn(self.MODEL, [row["id"] for row in rows])
        self.assertTrue(all(row["output_image"] for row in rows))
        # 0.7.12 catalog discovery remains enabled through the fixed read-only
        # fetch adapter; userscript must not regress to the older public table.
        self.assertEqual(verbs, ["models", "fetch"])
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0]["method"], "GET")
        self.assertTrue(fetches[0]["path"].startswith("/api/history/unified?"))

    def test_health_reports_the_relay_and_the_browsers_verdict(self) -> None:
        async def scenario():
            async with self.relay() as (server, _fakes):
                return await self.user_client(server).health()

        health = self.run_async(scenario())
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["transport"], "userscript-relay")
        self.assertEqual(health["browser"], "connected")
        self.assertEqual(health["relay"]["ready"], 1)
        self.assertEqual(health["session_source"], "userscript")
        self.assertEqual(len(health["relay"]["secret_fingerprint"]), 8)
        self.assertEqual(health["browsers"][0]["label"], "Win-0")

    def test_an_offline_relay_explains_itself_instead_of_hanging(self) -> None:
        async def scenario():
            async with self.relay(browsers=0) as (server, _fakes):
                return await self.user_client(server, acquire_wait=0.2).health()

        health = self.run_async(scenario())
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["browser"], "offline")
        self.assertIn("竞技场浏览器接入", health["message"])

    def test_text_to_image_runs_end_to_end(self) -> None:
        async def scenario():
            async with self.relay() as (server, fakes):
                result = await self.user_client(server).complete(
                    model=self.MODEL, prompt="a red apple"
                )
                return result, fakes[0].seen, fakes[0].fetches

        result, seen, fetches = self.run_async(scenario())
        self.assertEqual(bridge_client.image_urls(result), [ARENA_IMAGE])
        self.assertEqual(result["model"], self.MODEL)
        verbs = [verb for verb, _ in seen]
        self.assertEqual(verbs, ["models", "state", "recaptcha", "fetch"])
        self.assertEqual(len(fetches), 1)
        sent = json.loads(fetches[0]["body"])
        self.assertEqual(fetches[0]["path"], arena_direct.STREAM_CREATE_EVALUATION_PATH)
        self.assertEqual(sent["mode"], "direct-battle")
        self.assertEqual(sent["modality"], "image")
        self.assertEqual(sent["recaptchaV3Token"], "recaptcha-token")
        self.assertEqual(sent["userMessage"]["content"], "a red apple")
        self.assertEqual(sent["userMessage"]["experimental_attachments"], [])

    def test_image_to_image_uploads_through_the_visitors_browser(self) -> None:
        fake_httpx = FakeHTTPX()
        self._patch(arena_direct, "httpx", fake_httpx)

        async def scenario():
            async with self.relay() as (server, fakes):
                result = await self.user_client(server).complete(
                    model=self.MODEL, prompt="重画一遍", images=[_png_data_url()]
                )
                return result, fakes[0].seen, fakes[0].fetches

        result, seen, fetches = self.run_async(scenario())
        self.assertEqual(bridge_client.image_urls(result), [ARENA_IMAGE])
        self.assertEqual(
            [verb for verb, _ in seen],
            ["models", "state", "fetch", "fetch", "recaptcha", "fetch"],
        )
        # Steps 1 and 3 are JSON storage POSTs through the visitor's browser;
        # step 2 is the plain presigned PUT from the plugin, not an Action.
        self.assertEqual(len(fetches), 3)
        for fetch, path in zip(fetches[:2], (
            "/api/storage/generate-upload-url", "/api/storage/get-signed-url",
        )):
            self.assertEqual(fetch["path"], path)
            self.assertEqual(fetch["method"], "POST")
            self.assertEqual(fetch["headers"], {
                "Content-Type": "application/json", "Accept": "application/json",
            })
        first = json.loads(fetches[0]["body"])
        self.assertEqual(set(first), {"fileName", "contentType"})
        self.assertTrue(first["fileName"].endswith(".png"))
        self.assertEqual(first["contentType"], "image/png")
        self.assertEqual(json.loads(fetches[1]["body"]), {"key": "up/abc"})
        self.assertEqual(len(fake_httpx.puts), 1)
        self.assertEqual(fake_httpx.puts[0]["url"], "https://r2.example/put")
        self.assertEqual(fake_httpx.puts[0]["headers"]["Content-Type"], "image/png")
        attachments = json.loads(fetches[2]["body"])["userMessage"]["experimental_attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["name"], "up/abc")
        self.assertEqual(attachments[0]["contentType"], "image/png")
        self.assertEqual(attachments[0]["url"], "https://cdn.arena.ai/ref.png?X-Amz-Expires=3600")

    def test_model_health_still_records_what_the_relay_saw(self) -> None:
        async def scenario():
            async with self.relay() as (server, _fakes):
                client = self.user_client(server)
                await client.complete(model=self.MODEL, prompt="a red apple")
                return await client.model_health()

        health = self.run_async(scenario())
        rows = {row["id"]: row for row in health["models"]}
        self.assertEqual(rows[self.MODEL]["status_code"], 200)
        self.assertEqual(health["variants"], [])

    def test_verification_hands_out_the_install_link_never_a_vnc_url(self) -> None:
        async def scenario():
            async with self.relay() as (server, _fakes):
                client = self.user_client(server)
                started = await client.start_interactive_auth()
                latest = await client.latest_interactive_auth_status()
                return started, latest, server.install_url()

        started, latest, link = self.run_async(scenario())
        for payload in (started, latest):
            self.assertEqual(payload["transport"], "userscript-relay")
            self.assertNotIn("vnc_url", payload)
            self.assertTrue(payload["browser_url"].startswith(link.split("?")[0]))
            self.assertIn("t=", payload["browser_url"])
            self.assertEqual(payload["relay_ready"], 1)
        self.assertEqual(latest["status"], "verified")
        self.assertIn("已接入并且登录", latest["message"])
        self.assertIn("实际生成请求", latest["message"])

    def test_without_an_address_verification_says_what_to_configure(self) -> None:
        async def scenario():
            async with self.relay(public_url="") as (server, _fakes):
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    await self.user_client(server).start_interactive_auth()
                return caught.exception

        error = self.run_async(scenario())
        self.assertEqual(error.code, "relay_public_url_missing")
        self.assertIn("relay_public_url", str(error))

    def test_an_anonymous_browser_is_told_to_log_in(self) -> None:
        async def scenario():
            async with self.relay(state=ANONYMOUS) as (server, _fakes):
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    await self.user_client(server).complete(model=self.MODEL, prompt="x")
                return caught.exception

        error = self.run_async(scenario())
        self.assertEqual(error.code, "arena_login_required")
        self.assertIn("登录", str(error))

    def test_a_browser_that_cannot_read_its_cookies_is_still_allowed_through(self) -> None:
        """Tampermonkey-less managers report "assumed"; Arena's 401 decides."""

        async def scenario():
            assumed = {**LOGGED_IN, "session_source": "assumed"}
            async with self.relay(state=assumed) as (server, _fakes):
                client = self.user_client(server)
                result = await client.complete(model=self.MODEL, prompt="a red apple")
                return result, await client.health()

        result, health = self.run_async(scenario())
        self.assertEqual(bridge_client.image_urls(result), [ARENA_IMAGE])
        self.assertEqual(health["session_source"], "assumed")

    def test_a_recaptcha_failure_does_not_block_the_generation(self) -> None:
        def explode(_payload):
            raise RuntimeError("grecaptcha 没加载")

        async def scenario():
            async with self.relay() as (server, fakes):
                fakes[0].verbs["recaptcha"] = explode
                result = await self.user_client(server).complete(
                    model=self.MODEL, prompt="a red apple"
                )
                return result, fakes[0].fetches

        result, fetches = self.run_async(scenario())
        self.assertEqual(bridge_client.image_urls(result), [ARENA_IMAGE])
        self.assertEqual(json.loads(fetches[0]["body"])["recaptchaV3Token"], "")

    def test_two_browsers_never_share_action_ids_or_upload_receipts(self) -> None:
        async def scenario():
            async with self.relay(browsers=2) as (server, fakes):
                pages = []
                for fake in fakes:
                    browser = server._browsers[fake.id]
                    pages.append(arena_relay.RelayPage(server, browser))
                return [page.cache_key for page in pages]

        keys = self.run_async(scenario())
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(all(key.startswith("relay:") for key in keys))


class PrivacyTests(RelayCase):
    """The two promises that make this transport acceptable to a visitor."""

    def test_the_page_object_cannot_read_a_cookie_or_run_javascript(self) -> None:
        async def scenario():
            async with self.relay() as (server, fakes):
                page = arena_relay.RelayPage(server, server._browsers[fakes[0].id])
                cookies = await page.cookies(refresh=True)
                token = await page.auth_token()
                rotated = await page.refresh_session()
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    await page.evaluate("document.cookie")
                return cookies, token, rotated, caught.exception

        cookies, token, rotated, error = self.run_async(scenario())
        self.assertEqual(cookies, [])
        self.assertEqual(token, "")
        self.assertEqual(rotated, "")
        self.assertEqual(error.code, "relay_eval_forbidden")

    def test_there_is_no_verb_for_arbitrary_code(self) -> None:
        self.assertEqual(
            set(arena_relay.VERBS),
            {"state", "models", "actions", "recaptcha", "reload", "fetch"},
        )

        async def scenario():
            async with self.relay() as (server, fakes):
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    await server.submit(server._browsers[fakes[0].id], "eval", {})
                return caught.exception

        self.assertEqual(self.run_async(scenario()).code, "relay_bad_verb")

    def test_a_state_report_is_reduced_to_the_verdict(self) -> None:
        smuggled = {
            **LOGGED_IN,
            "cookie": "arena-auth-prod-v1=base64-secret",
            "access_token": _session_token(),
            "cookies": _chunked_cookies(_session_token()),
            "recaptcha_action": "../../etc/passwd",
            "page_url": "https://arena.ai/" + "x" * 900,
        }
        clean = arena_relay.sanitize_state(smuggled)
        self.assertNotIn("cookie", clean)
        self.assertNotIn("cookies", clean)
        self.assertNotIn("access_token", clean)
        self.assertEqual(clean["recaptcha_action"], "chat_submit")
        self.assertEqual(len(clean["page_url"]), 300)
        self.assertEqual(clean["session_source"], "userscript")
        self.assertTrue(clean["has_logged_in"])

    def test_an_assumed_verdict_stays_visible_as_assumed(self) -> None:
        clean = arena_relay.sanitize_state({**LOGGED_IN, "session_source": "assumed"})
        self.assertEqual(clean["session_source"], "assumed")
        # An unknown source is normalised rather than echoed back.
        odd = arena_relay.sanitize_state({**LOGGED_IN, "session_source": "whatever"})
        self.assertEqual(odd["session_source"], "userscript")

    def test_a_reported_cookie_never_reaches_the_relays_snapshot(self) -> None:
        async def scenario():
            async with self.relay() as (server, fakes):
                fakes[0].state = {**LOGGED_IN, "cookie": "arena-auth-prod-v1=leak"}
                await fakes[0].call("report", {**fakes[0].fields(), "kind": "selftest", "message": "ok"})
                return server.status()

        status = self.run_async(scenario())
        blob = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("leak", blob)
        self.assertNotIn("arena-auth-prod-v1", blob)


class FailureWordingTests(RelayCase):
    """Each failure has to name the thing the operator can actually fix."""

    def test_no_browser_at_all(self) -> None:
        async def scenario():
            async with self.relay(browsers=0) as (server, _fakes):
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    await self.user_client(server, acquire_wait=0.1).list_models()
                return caught.exception, server.port

        error, port = self.run_async(scenario())
        self.assertEqual(error.code, "relay_no_browser")
        link = str(error.payload["error"]["browser_url"])
        self.assertTrue(
            link.startswith(f"http://127.0.0.1:{port}/arena/userscript.user.js?t="), link
        )
        self.assertIn("测试连通", str(error))

    def test_a_paused_browser_says_paused(self) -> None:
        async def scenario():
            async with self.relay(paused=True) as (server, _fakes):
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    await self.user_client(server, acquire_wait=0.1).list_models()
                return caught.exception

        error = self.run_async(scenario())
        self.assertEqual(error.code, "relay_browser_paused")
        self.assertIn("暂停接单", str(error))

    def test_a_browser_that_never_answers_times_out(self) -> None:
        self._patch(arena_relay, "JOB_GRACE_SECONDS", 0.05)

        async def scenario():
            async def forever(_payload):
                await asyncio.sleep(30)

            async with self.relay() as (server, fakes):
                fakes[0].verbs["state"] = forever
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    await server.submit(server._browsers[fakes[0].id], "state", {}, timeout=5.0)
                return caught.exception

        error = self.run_async(scenario())
        self.assertEqual(error.code, "relay_job_timeout")
        self.assertIn("5 秒", str(error))

    def test_a_browser_that_disappears_fails_what_it_was_holding(self) -> None:
        async def scenario():
            async with self.relay() as (server, fakes):
                browser = server._browsers[fakes[0].id]
                job = arena_relay.RelayJob("state", {}, timeout=30.0, browser_id=browser.id)
                browser.inflight[job.id] = job
                server._abandon(browser, "relay_browser_lost", "浏览器掉线了。")
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    await job.future
                return caught.exception

        self.assertEqual(self.run_async(scenario()).code, "relay_browser_lost")

    def test_a_browser_cannot_invent_its_own_error_code(self) -> None:
        async def scenario():
            async with self.relay() as (server, fakes):
                def liar(_payload):
                    raise RuntimeError("我说了算")

                fakes[0].verbs["models"] = liar
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    await self.user_client(server).list_models()
                return caught.exception

        error = self.run_async(scenario())
        self.assertEqual(error.code, "browser_job_failed")
        self.assertIn("我说了算", str(error))

    def test_an_empty_model_table_is_reported_as_such(self) -> None:
        async def scenario():
            async with self.relay() as (server, fakes):
                fakes[0].verbs["models"] = lambda _payload: {"ok": False, "status": 403}
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    await self.user_client(server).list_models()
                return caught.exception

        error = self.run_async(scenario())
        self.assertEqual(error.code, "model_table_empty")

    def test_more_browsers_than_allowed_are_turned_away(self) -> None:
        async def scenario():
            async with self.relay(max_browsers=1) as (server, fakes):
                second = FakeBrowser(server, browser_id="browser-9")
                second._client = httpx.AsyncClient(timeout=10.0)
                try:
                    reply = await second.call("poll", {**second.fields(), "hold": 1})
                finally:
                    await second._client.aclose()
                return reply.status_code, reply.json()

        status, payload = self.run_async(scenario())
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "too_many_browsers")
        self.assertIn("relay_max_browsers", payload["message"])


class UserscriptSourceTests(unittest.TestCase):
    """Static checks on the shipped .js: cheap, and they catch drift early."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCRIPT_PATH.read_text(encoding="utf-8")

    def test_the_metadata_block_grants_what_the_transport_needs(self) -> None:
        for line in (
            "// @match        https://arena.ai/*",
            "// @run-at       document-idle",
            "// @noframes",
            # Page fetch cannot reach http://server:port from an https page.
            "// @grant        GM_xmlhttpRequest",
            # The auth cookie is httpOnly, so only GM_cookie can classify it.
            "// @grant        GM_cookie",
            "// @connect      __RELAY_HOST__",
        ):
            self.assertIn(line, self.text)

    def test_every_relay_verb_is_implemented(self) -> None:
        table = re.search(r"const VERBS = \{(.+?)\n  \};", self.text, re.S)
        assert table is not None, "VERBS table not found"
        implemented = set(re.findall(r"(\w+):\s", table.group(1)))
        self.assertEqual(implemented, set(arena_relay.VERBS))

    def test_the_script_has_no_way_to_run_arbitrary_code(self) -> None:
        for forbidden in ("eval(", "new Function(", "innerHTML = job", "GM_download"):
            self.assertNotIn(forbidden, self.text)

    def test_the_canonical_string_is_built_in_the_same_order(self) -> None:
        self.assertIn(
            "[String(method).toUpperCase(), path, ts, nonce, digest].join('\\n')",
            self.text,
        )
        self.assertIn("'X-Arena-Sign': sign", self.text)
        self.assertIn("'X-Arena-Browser': STATE.browserId", self.text)

    def test_the_self_test_covers_the_image_to_image_actions(self) -> None:
        # The whole point of the 自检 button: prove img2img will work before the
        # operator sends a real /jjc from QQ.
        self.assertIn("'generateUploadUrl', 'getSignedUrl'", self.text)
        self.assertIn("上传通道（图生图）", self.text)
        for label in ("测试连通", "自检", "暂停接单"):
            self.assertIn(f">{label}</button>", self.text)

    def test_the_page_talks_to_arena_with_its_own_credentials(self) -> None:
        self.assertIn("credentials: 'include'", self.text)
        self.assertIn("text/plain;charset=UTF-8", self.text)
        self.assertIn("text/event-stream", self.text)

    def test_the_chunked_auth_cookie_is_joined_numerically(self) -> None:
        self.assertIn("arena-auth-prod-v1", self.text)
        self.assertIn("base64-", self.text)
        self.assertIn("navigator.locks", self.text)

    def test_the_version_matches_the_metadata_block(self) -> None:
        meta = re.search(r"// @version\s+(\S+)", self.text)
        cfg = re.search(r"version: '([^']+)'", self.text)
        assert meta is not None and cfg is not None
        self.assertEqual(meta.group(1), cfg.group(1))


class PluginWiringTests(unittest.TestCase):
    """Rollback layer 1 again: one config key selects the userscript transport.

    Without this the whole relay is unreachable -- there would be no way for an
    operator to pick it, and no command that hands out the install link.
    """

    def setUp(self) -> None:
        self.main, self.plugin = _make_plugin(self, self.config())

    @staticmethod
    def config(**extra: Any) -> dict[str, Any]:
        options: dict[str, Any] = {
            "transport_mode": "userscript",
            "relay_bind_host": "127.0.0.1",
            "relay_port": _free_port(),
            "relay_secret": SECRET,
            "relay_public_url": "",
            "relay_max_browsers": 2,
            "relay_job_timeout": 45,
            "relay_acquire_wait": 3,
            "relay_lease_seconds": 900,
        }
        options.update(extra)
        return options

    def collect(self, agen) -> list[Any]:
        """Run one command handler and stop the relay inside the same loop.

        ``asyncio.run`` per call would leave a bound socket owned by a closed
        loop, which is exactly the kind of leak that makes the next test flaky.
        """

        async def runner() -> list[Any]:
            try:
                return [item async for item in agen]
            finally:
                await self.plugin.terminate()

        return asyncio.run(runner())

    # -- selection ------------------------------------------------------------

    def test_the_config_key_selects_the_userscript_client(self) -> None:
        self.assertEqual(self.plugin._transport_mode(), "userscript")
        client = self.plugin._client()
        self.assertIsInstance(client, arena_relay.ArenaUserscriptClient)
        # Inherited whole: that is what keeps every feature working.
        self.assertIsInstance(client, arena_direct.ArenaDirectClient)
        self.assertEqual(client.acquire_wait, 3.0)
        self.assertIs(client.relay, self.plugin._relay_server())

    def test_the_relay_is_configured_from_the_plugin_config(self) -> None:
        relay = self.plugin._relay_server()
        self.assertEqual(relay.host, "127.0.0.1")
        self.assertEqual(relay.secret, SECRET)
        self.assertEqual(relay.max_browsers, 2)
        self.assertEqual(relay.job_timeout, 45.0)
        self.assertEqual(relay.install_ttl, 900.0)
        self.assertIs(self.plugin._relay_server(), relay)

    def test_the_other_two_modes_never_build_a_relay(self) -> None:
        for mode in ("bridge", "direct", "", "nonsense"):
            with self.subTest(mode=mode):
                _main, plugin = _make_plugin(self, {"transport_mode": mode})
                plugin._client()
                self.assertIsNone(plugin._relay)

    def test_a_bind_failure_does_not_kill_the_plugin(self) -> None:
        async def scenario() -> tuple[bool, str]:
            blocker = await asyncio.start_server(
                lambda *_: None, "127.0.0.1", self.plugin.config["relay_port"]
            )
            try:
                # initialize() must swallow it: health() is where the operator
                # sees the bind error, and a raise here would unload the plugin.
                await self.plugin.initialize()
                report = await self.plugin._client().health()
            finally:
                blocker.close()
                await blocker.wait_closed()
                await self.plugin.terminate()
            return self.plugin._relay_server().running, str(report.get("message") or "")

        running, message = asyncio.run(scenario())
        self.assertFalse(running)
        self.assertIn("监听失败", message)

    def test_terminate_releases_the_port(self) -> None:
        port = int(self.plugin.config["relay_port"])

        async def scenario() -> bool:
            await self.plugin.initialize()
            assert self.plugin._relay_server().running
            await self.plugin.terminate()
            probe = await asyncio.start_server(lambda *_: None, "127.0.0.1", port)
            probe.close()
            await probe.wait_closed()
            return True

        self.assertTrue(asyncio.run(scenario()))
        self.assertIsNone(self.plugin._relay)

    def test_startup_logging_never_prints_an_install_token(self) -> None:
        self.plugin.config["relay_public_url"] = "http://HOST:6083"
        lines: list[str] = []

        async def scenario() -> None:
            try:
                await self.plugin.initialize()
            finally:
                await self.plugin.terminate()

        with _captured_logs(self.main.logger, lines):
            asyncio.run(scenario())
        blob = "\n".join(lines)
        self.assertIn("HOST:6083", blob)
        self.assertNotIn("?t=", blob)
        self.assertNotIn(SECRET, blob)

    # -- commands -------------------------------------------------------------

    def test_the_install_link_is_private_chat_only(self) -> None:
        self.plugin.config["relay_public_url"] = "http://HOST:6083"
        results = self.collect(self.plugin.relay_enroll(FakeEvent(private=False)))
        self.assertEqual(len(results), 1)
        self.assertNotIn("HOST:6083", results[0].text)
        self.assertIn("私聊", results[0].text)

    def test_enrolling_hands_out_a_signed_install_link(self) -> None:
        self.plugin.config["relay_public_url"] = "http://127.0.0.1:1"
        results = self.collect(self.plugin.relay_enroll(FakeEvent(private=True)))
        text = results[0].text
        self.assertIn("Tampermonkey", text)
        self.assertIn("测试连通", text)
        self.assertIn("/userscript.user.js?t=", text)
        token = text.split("?t=", 1)[1].split()[0]
        self.assertTrue(arena_relay.check_install_token(SECRET, token))

    def test_enrolling_without_a_public_url_names_the_missing_key(self) -> None:
        results = self.collect(self.plugin.relay_enroll(FakeEvent(private=True)))
        self.assertIn("relay_public_url", results[0].text)

    def test_enrolling_in_bridge_mode_says_which_key_to_flip(self) -> None:
        _main, plugin = _make_plugin(self, {"transport_mode": "bridge"})
        results = _collect(plugin.relay_enroll(FakeEvent(private=True)))
        self.assertIn("transport_mode", results[0].text)
        self.assertIsNone(plugin._relay)

    def test_browser_status_reports_the_relay_and_every_browser(self) -> None:
        async def scenario() -> str:
            relay = self.plugin._relay_server()
            await relay.start()
            fake = FakeBrowser(relay, browser_id="browser-0", label="Win-0")
            await fake.start()
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and not relay.status()["ready"]:
                await asyncio.sleep(0.02)
            try:
                results = [
                    item async for item in self.plugin.relay_status(FakeEvent(private=True))
                ]
            finally:
                await fake.stop()
                await self.plugin.terminate()
            return results[0].text

        text = asyncio.run(scenario())
        self.assertIn("运行中", text)
        self.assertIn("Win-0", text)
        self.assertIn("已登录", text)
        self.assertIn(self.plugin._relay_server().fingerprint, text)

    def test_browser_status_in_bridge_mode_points_at_the_other_command(self) -> None:
        _main, plugin = _make_plugin(self, {"transport_mode": "bridge"})
        results = _collect(plugin.relay_status(FakeEvent(private=True)))
        self.assertIn("/竞技场验证状态", results[0].text)
        self.assertIsNone(plugin._relay)

    def test_verify_in_userscript_mode_explains_the_script_instead(self) -> None:
        self.plugin.config["relay_public_url"] = "http://127.0.0.1:1"
        results = self.collect(self.plugin.interactive_verify(FakeEvent(private=True)))
        text = results[0].text
        self.assertIn("油猴", text)
        self.assertNotIn("进入验证浏览器", text)
        self.assertIn("/竞技场浏览器状态", text)

    def test_rebind_in_userscript_mode_does_not_promise_a_server_cookie(self) -> None:
        self.plugin.config["relay_public_url"] = "http://127.0.0.1:1"
        results = self.collect(self.plugin.interactive_rebind(FakeEvent(private=True)))
        text = results[0].text
        self.assertNotIn("Bridge 会自动读取", text)
        self.assertIn("自己的浏览器", text)

    def test_the_new_commands_are_admin_only(self) -> None:
        for name in ("relay_enroll", "relay_status"):
            with self.subTest(command=name):
                handler = getattr(self.main.ArenaImagePlugin, name)
                self.assertEqual(
                    getattr(handler, "arena_permission", None),
                    self.main.filter.PermissionType.ADMIN,
                )

    # -- hints ----------------------------------------------------------------

    def test_every_relay_failure_code_gets_an_actionable_hint(self) -> None:
        codes = (
            "relay_no_browser",
            "relay_browser_paused",
            "relay_browser_offline",
            "relay_browser_lost",
            "relay_job_timeout",
            "relay_bind_failed",
            "relay_not_running",
            "relay_stopped",
            "relay_public_url_missing",
            "relay_bad_verb",
        )
        for code in codes:
            with self.subTest(code=code):
                error = bridge_client.BridgeError(
                    "原始说明",
                    payload={"error": {"code": code, "message": "原始说明"}},
                )
                hint = self.main.ArenaImagePlugin._browser_hint(error)
                self.assertIsNotNone(hint)
                self.assertIn("原始说明", hint)
                self.assertTrue(
                    "/竞技场浏览器状态" in hint
                    or "/竞技场浏览器接入" in hint
                    or "relay_port" in hint,
                    hint,
                )

    def test_the_browser_hint_never_leaks_the_install_link(self) -> None:
        error = bridge_client.BridgeError(
            "还没有浏览器接入。",
            payload={
                "error": {
                    "code": "relay_no_browser",
                    "message": "还没有浏览器接入。",
                    "browser_url": "http://HOST:6083/arena/userscript.user.js?t=SECRETTOKEN",
                }
            },
        )
        hint = self.main.ArenaImagePlugin._browser_hint(error)
        self.assertNotIn("SECRETTOKEN", hint)
        self.assertIn("/竞技场浏览器接入", hint)

    def test_a_non_relay_error_is_left_to_the_other_hints(self) -> None:
        error = bridge_client.BridgeError(
            "Arena 会话已过期",
            payload={"error": {"code": "arena_auth_expired"}},
        )
        self.assertIsNone(self.main.ArenaImagePlugin._browser_hint(error))
        self.assertIsNone(self.main.ArenaImagePlugin._browser_hint(RuntimeError("boom")))

    def test_login_hints_stop_talking_about_a_server_browser(self) -> None:
        plugin = self.main.ArenaImagePlugin
        for code in ("arena_login_required", "arena_auth_expired", "arena_session_rejected"):
            with self.subTest(code=code):
                error = bridge_client.BridgeError(
                    "请在那台电脑上重新登录一次 arena.ai。",
                    payload={"error": {"code": code, "message": "x"}},
                )
                relay_hint = plugin._verification_hint(error, mode="userscript")
                self.assertIsNotNone(relay_hint)
                self.assertNotIn("服务器浏览器", relay_hint)
                self.assertNotIn("/竞技场重新绑定", relay_hint)
                # The CDP wording must be untouched for the other two modes.
                server_hint = plugin._verification_hint(error)
                self.assertIn("服务器浏览器" if code != "arena_session_rejected" else "/竞技场验证", server_hint)

    def test_the_status_line_admits_an_optimistic_login_check(self) -> None:
        text = self.main.ArenaImagePlugin._format_verification_status(
            {
                "status": "verified",
                "verified": True,
                "has_logged_in": True,
                "session_source": "assumed",
                "session_expires_in": None,
            },
            mode="userscript",
        )
        self.assertIn("乐观推断", text)
        self.assertNotIn("服务器浏览器", text)


class ConfigSchemaTests(unittest.TestCase):
    """A key the panel needs but the schema hides is a key nobody can set."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(arena_relay.__file__).resolve().parent
        cls.schema = json.loads(
            (root / "_conf_schema.json").read_text(encoding="utf-8-sig")
        )

    def test_userscript_is_an_offered_transport_mode(self) -> None:
        self.assertIn("userscript", self.schema["transport_mode"]["options"])
        self.assertEqual(self.schema["transport_mode"]["default"], "bridge")

    def test_every_relay_key_the_plugin_reads_is_documented(self) -> None:
        for key, expected in (
            ("relay_bind_host", "0.0.0.0"),
            ("relay_port", 6083),
            ("relay_public_url", ""),
            ("relay_secret", ""),
            ("relay_lease_seconds", 1800),
            ("relay_job_timeout", 120),
            ("relay_acquire_wait", 20),
            ("relay_max_browsers", 4),
            ("relay_allow_ips", []),
        ):
            with self.subTest(key=key):
                self.assertIn(key, self.schema)
                self.assertEqual(self.schema[key]["default"], expected)

    def test_the_shared_secret_is_marked_secret(self) -> None:
        self.assertTrue(self.schema["relay_secret"].get("secret"))

    def test_the_plugin_only_reads_documented_relay_keys(self) -> None:
        source = (Path(arena_relay.__file__).resolve().parent / "main.py").read_text(
            encoding="utf-8"
        )
        used = set(re.findall(r"""config\.get\(\s*["'](relay_[a-z_]+)["']""", source))
        self.assertTrue(used)
        self.assertEqual(used - set(self.schema), set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
