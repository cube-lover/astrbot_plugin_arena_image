"""Offline contracts for the current JSON storage reference-image upload.

All browser requests and signed PUTs are strict in-memory fakes. Successful
fixtures describe the protocol, not a claim that the live API accepted an upload.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx

from astrbot_plugin_arena_image import arena_direct, arena_relay, bridge_client

from .test_arena_direct_transport import (
    DirectTransportCase, FakeHTTPX, FakePage, _flight, _png_bytes, _png_data_url,
)

ALLOCATE = "/api/storage/generate-upload-url"
SIGN = "/api/storage/get-signed-url"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
NOW = 2_000_000_000.0
DIGEST = hashlib.md5(_png_bytes()).hexdigest()
KEY = "uploads/offline-reference.png"
PUT_URL = "https://storage.example.invalid/put?X-Amz-Signature=offline-put-secret"
SIGNED_URL = (
    "https://storage.example.invalid/get?X-Amz-Date="
    + datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    + "&X-Amz-Expires=3600&X-Amz-Signature=offline-get-secret"
)


def _reply(value, *, status=200, headers=None):
    return {"status": status, "headers": dict(headers or {}), "text": json.dumps(value)}


def _allocated(key=KEY):
    return _reply({"uploadUrl": PUT_URL, "key": key})


def _signed(url=SIGNED_URL):
    return _reply({"url": url})


class StoragePage(FakePage):
    def __init__(self, responses=(), *, scope="cdp:offline-storage-a"):
        super().__init__(responses=list(responses))
        self.cache_key = scope
        self.on_fetch = None

    async def fetch(self, path, **kwargs):
        if not self._responses:
            raise AssertionError(f"unexpected offline storage request: {path}")
        if self.on_fetch is not None:
            self.on_fetch(path)
        return await super().fetch(path, **kwargs)

    async def next_actions(self):
        raise AssertionError("REST uploads must not discover Next Actions")

    async def reload(self, **kwargs):
        self.reload_calls += 1
        raise AssertionError("REST mutations must not reload or retry")


class StorageHTTPX(FakeHTTPX):
    """Signed PUT capture, optional observer/failure; never a real HTTP client."""

    def __init__(self, status=200):
        super().__init__(status)
        self.options = []
        self.error = None
        self.on_put = None

    def AsyncClient(self, **kwargs):  # noqa: N802
        self.options.append(kwargs)
        client = super().AsyncClient(**kwargs)
        original = client.put

        async def put(url, *, content=None, headers=None):
            result = await original(url, content=content, headers=headers)
            if self.on_put is not None:
                self.on_put(url, content, headers)
            if self.error is not None:
                raise self.error
            return result

        client.put = put
        return client


class ImageUploadRESTTests(DirectTransportCase):
    def setUp(self):
        super().setUp()
        self.httpx = StorageHTTPX()
        self._patch(arena_direct, "httpx", self.httpx)
        self._patch(arena_direct.time, "time", lambda: NOW)

    def uploader(self, page):
        client = self.client(page)
        for name in ("_next_actions", "_upload_once"):
            forbidden = AsyncMock(side_effect=AssertionError("legacy upload path invoked"))
            self._patch(client, name, forbidden)
            self.addCleanup(forbidden.assert_not_awaited)
        return client

    def upload(self, page, client=None):
        return self.run_async((client or self.uploader(page))._upload_reference(page, _png_data_url()))

    def assert_unconfirmed(self, page, *, requests, puts):
        self.assertEqual(len(page.requests), requests)
        self.assertEqual(len(self.httpx.puts), puts)
        self.assertEqual(page.reload_calls, 0)
        self.assertEqual(page.action_reads, 0)
        self.assertEqual(arena_direct.cache_scope(page)["uploads"], {})

    def assert_sanitized(self, error):
        public = str(error) + repr(error) + json.dumps(error.payload, ensure_ascii=False)
        for hidden in (PUT_URL, SIGNED_URL, "offline-put-secret", "offline-get-secret", "do-not-echo"):
            self.assertNotIn(hidden, public)

    def test_json_put_json_contract_and_cache_only_after_signed_confirmation(self):
        page = StoragePage([_allocated(), _signed()])
        client = self.uploader(page)
        uploads = arena_direct.cache_scope(page)["uploads"]
        page.on_fetch = lambda _path: self.assertEqual(uploads, {})

        def observe_put(url, content, headers):
            self.assertEqual(uploads, {})
            self.assertEqual(url, PUT_URL)
            self.assertEqual(content, _png_bytes())
            self.assertEqual(headers, {"Content-Type": "image/png"})

        self.httpx.on_put = observe_put
        entry = self.upload(page, client)
        self.assertEqual(entry, {"name": KEY, "contentType": "image/png", "url": SIGNED_URL})
        self.assertEqual([r["path"] for r in page.requests], [ALLOCATE, SIGN])
        for request in page.requests:
            self.assertEqual(request["method"], "POST")
            self.assertEqual(request["headers"], HEADERS)
            self.assertEqual(request["timeout"], 60.0)
        self.assertEqual(json.loads(page.requests[0]["body"]), {
            "fileName": DIGEST[:12] + ".png", "contentType": "image/png",
        })
        self.assertEqual(json.loads(page.requests[1]["body"]), {"key": KEY})
        self.assertEqual(self.httpx.options, [{"timeout": 90.0}])
        self.assertEqual(uploads[DIGEST], {
            **entry, "key": KEY, "upload_contract": "storage-rest-v1", "expiry": NOW + 3600,
        })
        self.assertEqual(self.upload(page, client), entry)
        self.assertEqual(len(page.requests), 2)
        self.assertEqual(len(self.httpx.puts), 1)
        self.assertEqual(page.reload_calls, 0)
        self.assertEqual(page.action_reads, 0)
        self.assertEqual(page._responses, [])

    def test_leading_slash_key_normalized_for_signing_attachment_and_cache(self):
        page = StoragePage([_allocated("/" + KEY), _signed()])
        entry = self.upload(page)
        self.assertEqual(entry["name"], KEY)
        self.assertEqual(json.loads(page.requests[1]["body"]), {"key": KEY})
        self.assertEqual(arena_direct.cache_scope(page)["uploads"][DIGEST]["key"], KEY)

    def test_http200_allocation_malformed_or_business_error_never_puts_or_caches(self):
        bodies = [
            "", "{broken", "<html>OK</html>", "null", "[]", "{}",
            _flight({"success": True, "data": {"uploadUrl": PUT_URL, "key": KEY}}),
        ] + [json.dumps(value) for value in (
            {"uploadUrl": PUT_URL}, {"key": KEY},
            {"uploadUrl": PUT_URL, "key": ""}, {"uploadUrl": PUT_URL, "key": "/"},
            {"uploadUrl": PUT_URL, "key": " "}, {"uploadUrl": PUT_URL, "key": 7},
            {"uploadUrl": PUT_URL, "key": "x" * 2049},
            {"uploadUrl": PUT_URL, "key": KEY, "success": False},
            {"uploadUrl": PUT_URL, "key": KEY, "error": "do-not-echo " + SIGNED_URL},
        )]
        for body in bodies:
            with self.subTest(body=body):
                page = StoragePage([{"status": 200, "headers": {}, "text": body}, _signed()])
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    self.upload(page)
                self.assertEqual(caught.exception.code, "upload_url_failed")
                self.assert_sanitized(caught.exception)
                self.assert_unconfirmed(page, requests=1, puts=0)
                self.assertEqual(len(page._responses), 1)

    def test_http200_signing_malformed_or_business_error_never_caches(self):
        bodies = ["", "{broken", "<html>OK</html>", "null", "[]", "{}",
                  _flight({"success": True, "data": {"url": SIGNED_URL}})]
        bodies += [json.dumps(value) for value in (
            {"url": None}, {"url": ""}, {"url": SIGNED_URL, "success": False},
            {"url": SIGNED_URL, "error": {"message": "do-not-echo " + PUT_URL}},
        )]
        for body in bodies:
            with self.subTest(body=body):
                self.httpx.puts.clear()
                page = StoragePage([_allocated(), {"status": 200, "headers": {}, "text": body}])
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    self.upload(page)
                self.assertEqual(caught.exception.code, "upload_signed_url_failed")
                self.assert_sanitized(caught.exception)
                self.assert_unconfirmed(page, requests=2, puts=1)

    def test_signed_urls_require_https_host_and_no_userinfo_at_both_steps(self):
        for url in (None, 7, "", "http://storage.example.invalid/a", "/relative",
                    "https:///missing-host", "https://user:do-not-echo@storage.example.invalid/a",
                    "https://[invalid/a", "https://storage.example.invalid/" + "x" * 16384):
            for signing in (False, True):
                with self.subTest(url=url, signing=signing):
                    self.httpx.puts.clear()
                    replies = ([_allocated(), _signed(url)] if signing
                               else [_reply({"uploadUrl": url, "key": KEY})])
                    page = StoragePage(replies)
                    with self.assertRaises(bridge_client.BridgeError) as caught:
                        self.upload(page)
                    self.assert_sanitized(caught.exception)
                    self.assert_unconfirmed(page, requests=2 if signing else 1, puts=int(signing))

    def test_non200_storage_responses_fail_closed_including_explicit_and_plain_409(self):
        failures = [_reply({"error": "do-not-echo " + SIGNED_URL}, status=status)
                    for status in (0, 201, 204, 302, 401, 403, 404, 409, 429, 500)]
        failures += [_reply({"error": "refresh_required"}, status=409),
                     _reply({}, status=409, headers={"x-arena-refresh-required": "1"})]
        for failure in failures:
            for signing in (False, True):
                with self.subTest(failure=failure, signing=signing):
                    self.httpx.puts.clear()
                    page = StoragePage(([_allocated(), failure] if signing else [failure]) + [_signed()])
                    with self.assertRaises(bridge_client.BridgeError) as caught:
                        self.upload(page)
                    self.assertEqual(caught.exception.status_code, failure["status"] or None)
                    expected = ("upload_deployment_refresh_required"
                                if arena_direct.deployment_refresh_required(failure)
                                else "upload_signed_url_failed" if signing else "upload_url_failed")
                    self.assertEqual(caught.exception.code, expected)
                    self.assert_sanitized(caught.exception)
                    self.assert_unconfirmed(page, requests=2 if signing else 1, puts=int(signing))
                    self.assertEqual(len(page._responses), 1)

    def test_put_success_is_only_confirmed_200_201_204_not_redirect_or_other_status(self):
        for status in (0, 199, 200, 201, 202, 204, 206, 299, 301, 302, 307, 308, 400, 409, 500):
            with self.subTest(status=status):
                self.httpx.status = status
                self.httpx.puts.clear()
                page = StoragePage([_allocated(), _signed()], scope=f"cdp:offline-put-{status}")
                if status in (200, 201, 204):
                    self.assertEqual(self.upload(page)["url"], SIGNED_URL)
                else:
                    with self.assertRaises(bridge_client.BridgeError) as caught:
                        self.upload(page)
                    self.assertEqual(caught.exception.code, "upload_failed")
                    self.assertEqual(caught.exception.status_code, status)
                    self.assert_unconfirmed(page, requests=1, puts=1)
                    self.assertEqual(len(page._responses), 1)

    def test_signed_put_timeout_is_sanitized_and_never_requests_signing_or_retries(self):
        self.httpx.error = httpx.ReadTimeout("do-not-echo " + PUT_URL + " " + SIGNED_URL)
        page = StoragePage([_allocated(), _signed()])
        with self.assertRaises(bridge_client.BridgeError) as caught:
            self.upload(page)
        self.assertEqual(caught.exception.code, "upload_failed")
        self.assert_sanitized(caught.exception)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assert_unconfirmed(page, requests=1, puts=1)

    def test_storage_fetch_timeouts_are_sanitized_and_not_replayed(self):
        for signing in (False, True):
            with self.subTest(signing=signing):
                self.httpx.puts.clear()
                page = StoragePage()
                failure = TimeoutError("do-not-echo " + PUT_URL + " " + SIGNED_URL)
                fetched = AsyncMock(side_effect=[_allocated(), failure] if signing else failure)
                self._patch(page, "fetch", fetched)
                with self.assertRaises(bridge_client.BridgeError) as caught:
                    self.upload(page)
                self.assert_sanitized(caught.exception)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(fetched.await_count, 2 if signing else 1)
                self.assertEqual(fetched.await_args.args, (SIGN if signing else ALLOCATE,))
                self.assertEqual(len(self.httpx.puts), int(signing))
                self.assertEqual(arena_direct.cache_scope(page)["uploads"], {})
                self.assertEqual(page.reload_calls, 0)

    def test_legacy_filename_cache_is_ignored_even_with_future_expiry(self):
        for marker in (None, "legacy-flight"):
            with self.subTest(marker=marker):
                page = StoragePage([_allocated(), _signed()])
                receipt = {"name": "old-filename.png", "contentType": "image/png",
                           "url": SIGNED_URL, "key": "old/key", "expiry": NOW + 7200}
                if marker is not None:
                    receipt["upload_contract"] = marker
                arena_direct.cache_scope(page)["uploads"][DIGEST] = receipt
                self.assertEqual(self.upload(page)["name"], KEY)
                self.assertEqual(len(page.requests), 2)
                self.assertEqual(arena_direct.cache_scope(page)["uploads"][DIGEST]["upload_contract"],
                                 "storage-rest-v1")

    def test_unknown_or_nearly_expired_signed_lifetime_is_not_reused(self):
        urls = ["https://storage.example.invalid/get?signature=offline",
                SIGNED_URL.replace("X-Amz-Expires=3600", "X-Amz-Expires=60"),
                SIGNED_URL.replace("X-Amz-Expires=3600", "X-Amz-Expires=0")]
        for index, url in enumerate(urls):
            with self.subTest(url=url):
                page = StoragePage([_allocated(), _signed(url), _allocated(), _signed()],
                                   scope=f"cdp:offline-expiry-{index}")
                client = self.uploader(page)
                self.assertEqual(self.upload(page, client)["url"], url)
                expiry = arena_direct.signed_url_expiry(url)
                self.assertEqual(arena_direct.cache_scope(page)["uploads"][DIGEST]["expiry"],
                                 expiry if expiry is not None else 0.0)
                self.assertEqual(self.upload(page, client)["url"], SIGNED_URL)
                self.assertEqual(len(page.requests), 4)

    def test_confirmed_cache_is_browser_scoped_not_shared_between_cdp_and_relay(self):
        pages = [StoragePage([_allocated(), _signed()], scope=scope)
                 for scope in ("cdp:offline-a", "cdp:offline-b", "relay:offline-a")]
        for page in pages:
            client = self.uploader(page)
            self.upload(page, client)
            self.upload(page, client)
            self.assertEqual(len(page.requests), 2)
        self.assertEqual(len(self.httpx.puts), 3)
        self.assertIsNot(arena_direct.cache_scope(pages[0])["uploads"],
                         arena_direct.cache_scope(pages[1])["uploads"])

    def test_real_cdp_fetch_storage_409_does_not_reload_or_replay_mutation(self):
        # Exercise the actual page adapter; a FakePage alone would hide retries.
        for path in (ALLOCATE, SIGN):
            with self.subTest(path=path):
                page = arena_direct.ArenaPage(None, "offline-session", "https://arena.ai/text/direct",
                                              cache_key="cdp:offline-adapter")
                conflict = _reply({"error": "refresh_required"}, status=409)
                page.evaluate = AsyncMock(side_effect=[conflict, _allocated()])
                page.reload = AsyncMock(return_value=True)
                result = self.run_async(page.fetch(path, headers=HEADERS, body="{}", timeout=60))
                self.assertEqual(result, conflict)
                page.evaluate.assert_awaited_once()
                page.reload.assert_not_awaited()

    def test_real_relay_fetch_storage_409_is_single_submit_without_reload(self):
        server = arena_relay.RelayServer(host="127.0.0.1", secret="offline-storage-test-secret",
                                         data_dir=self.data_dir)
        browser = arena_relay.RelayBrowser("offline-storage-browser")
        conflict = _reply({"error": "refresh_required"}, status=409)
        submitted = AsyncMock(return_value=conflict)
        self._patch(server, "submit", submitted)
        page = arena_relay.RelayPage(server, browser)
        for path in (ALLOCATE, SIGN):
            submitted.reset_mock()
            self.assertEqual(self.run_async(page.fetch(path, headers=HEADERS, body="{}", timeout=60)),
                             conflict)
            submitted.assert_awaited_once()
            self.assertEqual(submitted.await_args.args[:2], (browser, "fetch"))
            self.assertEqual(submitted.await_args.args[2]["path"], path)
        self.assertFalse(server.running)
