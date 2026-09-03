from __future__ import annotations

import hashlib
import hmac
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, build_opener
from urllib.request import Request, urlopen

from src.interactive_auth import _build_interactive_gateway_url
from novnc_gateway import GatewayServer, _verify_token, _vnc_url_with_password


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def test_signed_link_contains_no_vnc_password() -> None:
    url = _build_interactive_gateway_url(
        "http://HOST:6081",
        expires_at=time.time() + 300,
        secret="link-secret",
    )
    assert url.startswith("http://HOST:6081/v/")
    assert "password" not in url
    assert "link-secret" not in url

    token = url.rsplit("/", 1)[-1]
    assert _verify_token(token, "link-secret")
    assert not _verify_token(token, "wrong-secret")


def test_vnc_password_is_only_added_to_fragment() -> None:
    result = _vnc_url_with_password(
        "http://HOST:6080/vnc.html?resize=scale",
        "vnc&password",
    )
    assert result.startswith("http://HOST:6080/vnc.html?")
    assert "autoconnect=true" in result
    assert "password=vnc%26password" in result
    assert "#password=" in result
    assert "?password=" not in result


def test_gateway_page_hides_password_until_connect(tmp_path, monkeypatch) -> None:
    secret_path = tmp_path / "secret"
    password_path = tmp_path / "password"
    secret_path.write_text("link-secret", encoding="utf-8")
    password_path.write_text("vnc-secret", encoding="utf-8")
    monkeypatch.setenv("NOVNC_GATEWAY_SECRET_FILE", str(secret_path))
    monkeypatch.setenv("NOVNC_PASSWORD_FILE", str(password_path))
    monkeypatch.setenv(
        "NOVNC_VNC_URL",
        "http://HOST:6080/vnc.html?autoconnect=true&resize=scale",
    )

    server = GatewayServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        expires = int(time.time()) + 300
        nonce = "test-nonce"
        payload = f"{expires}.{nonce}"
        signature = hmac.new(
            b"link-secret",
            payload.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        token = f"{payload}.{signature}"
        base = f"http://127.0.0.1:{port}/v/{token}"

        with urlopen(Request(base), timeout=3) as response:
            page = response.read().decode("utf-8")
        assert "vnc-secret" not in page
        assert f"/v/{token}/connect" in page

        opener = build_opener(_NoRedirect)
        try:
            opener.open(Request(f"{base}/connect"), timeout=3)
        except HTTPError as exc:
            assert exc.code == 302
            location = exc.headers["Location"]
            assert "vnc-secret" in location
            assert "#password=" in location
        else:
            raise AssertionError("expected redirect")
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
