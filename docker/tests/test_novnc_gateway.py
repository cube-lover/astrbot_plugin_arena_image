from __future__ import annotations

import hashlib
import hmac
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
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
    monkeypatch.setenv("NOVNC_STATIC_ROOT", str(tmp_path / "novnc"))
    (tmp_path / "novnc").mkdir()
    (tmp_path / "novnc" / "vnc.html").write_text(
        "<!doctype html><title>noVNC</title>", encoding="utf-8"
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
            assert location.startswith("/v/")
            assert "/vnc.html#" in location
            assert f"path=v%2F{quote(token, safe='')}%2Fwebsockify" in location
            assert "vnc-secret" in location
            assert "&password=" in location
            assert "?password=" not in location
        else:
            raise AssertionError("expected redirect")

        with urlopen(Request(f"{base}/vnc.html"), timeout=3) as response:
            assert response.read() == b"<!doctype html><title>noVNC</title>"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_gateway_proxies_websocket_upgrade(tmp_path, monkeypatch) -> None:
    secret_path = tmp_path / "secret"
    password_path = tmp_path / "password"
    secret_path.write_text("link-secret", encoding="utf-8")
    password_path.write_text("vnc-secret", encoding="utf-8")
    novnc_root = tmp_path / "novnc"
    novnc_root.mkdir()
    (novnc_root / "vnc.html").write_text("noVNC", encoding="utf-8")

    backend_ready = threading.Event()
    backend_received = threading.Event()

    def fake_websockify() -> None:
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            monkeypatch.setenv("NOVNC_INTERNAL_HOST", "127.0.0.1")
            monkeypatch.setenv("NOVNC_INTERNAL_PORT", str(server.getsockname()[1]))
            backend_ready.set()
            conn, _ = server.accept()
            with conn:
                request = conn.recv(4096)
                assert b"GET /websockify HTTP/1.1" in request
                assert b"Upgrade: websocket" in request
                backend_received.set()
                conn.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\n"
                    b"Connection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: test\r\n\r\n"
                )
                message = conn.recv(4096)
                conn.sendall(message)

    backend_thread = threading.Thread(target=fake_websockify, daemon=True)
    backend_thread.start()
    assert backend_ready.wait(3)

    monkeypatch.setenv("NOVNC_GATEWAY_SECRET_FILE", str(secret_path))
    monkeypatch.setenv("NOVNC_PASSWORD_FILE", str(password_path))
    monkeypatch.setenv("NOVNC_STATIC_ROOT", str(novnc_root))
    server = GatewayServer(("127.0.0.1", 0))
    gateway_thread = threading.Thread(target=server.serve_forever, daemon=True)
    gateway_thread.start()
    try:
        expires = int(time.time()) + 300
        payload = f"{expires}.proxy-test"
        signature = hmac.new(
            b"link-secret", payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
        token = f"{payload}.{signature}"
        with socket.create_connection(("127.0.0.1", server.server_address[1]), 5) as sock:
            request = (
                f"GET /v/{token}/websockify HTTP/1.1\r\n"
                "Host: example.com\r\n"
                "Connection: Upgrade\r\n"
                "Upgrade: websocket\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
            sock.sendall(request)
            response = bytearray()
            while b"\r\n\r\n" not in response:
                response.extend(sock.recv(4096))
            assert b" 101 " in response
            sock.sendall(b"echo-packet")
            assert sock.recv(4096) == b"echo-packet"
        assert backend_received.wait(3)
    finally:
        server.shutdown()
        gateway_thread.join(timeout=3)
        server.server_close()
        backend_thread.join(timeout=3)
