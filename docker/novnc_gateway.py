"""Short-lived noVNC link gateway.

The bridge creates a signed, expiring URL.  This tiny service validates that
URL and redirects the browser's iframe to the existing noVNC page with the VNC
password in the URL fragment.  Fragments are handled only by the browser and
are not sent in HTTP requests, while the password never appears in the chat
message or in the gateway route.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


TOKEN_RE = re.compile(r"^(?P<expires>[0-9]+)\.(?P<nonce>[A-Za-z0-9_-]+)\.(?P<signature>[a-f0-9]{64})$")


def _read_value(path_env: str, fallback_env: str = "") -> str:
    path = os.environ.get(path_env, "").strip()
    if path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value:
            return value
    return os.environ.get(fallback_env, "").strip() if fallback_env else ""


def _verify_token(token: str, secret: str, now: int | None = None) -> bool:
    match = TOKEN_RE.fullmatch(str(token or "").strip())
    if not match or not secret:
        return False
    expires = int(match.group("expires"))
    if expires <= int(time.time() if now is None else now):
        return False
    payload = f"{expires}.{match.group('nonce')}"
    expected = hmac.new(
        secret.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, match.group("signature"))


def _vnc_url_with_password(vnc_url: str, password: str) -> str:
    """Add noVNC's password/autoconnect settings without changing its host."""
    parts = urlsplit(str(vnc_url or "").strip())
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("autoconnect", "true")
    fragment_values = [
        (key, value)
        for key, value in parse_qsl(parts.fragment, keep_blank_values=True)
        if key != "password"
    ]
    fragment_values.append(("password", password))
    fragment = urlencode(fragment_values, doseq=True, safe="")
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path or "/vnc.html",
            urlencode(query, doseq=True),
            fragment,
        )
    )


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ArenaAuthGateway/1.0"

    def log_message(self, format: str, *args: object) -> None:
        # Do not log signed URLs or any derived connection information.
        return None

    @property
    def gateway(self) -> "GatewayServer":
        return self.server  # type: ignore[return-value]

    def _send_bytes(
        self,
        payload: bytes,
        *,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_bytes(
            json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"),
            content_type="application/json; charset=utf-8",
            status=status,
        )

    def _token_parts(self) -> tuple[str, str] | None:
        path = urlsplit(self.path).path.rstrip("/")
        parts = path.split("/")
        if len(parts) == 3 and parts[1] == "v" and parts[2]:
            return parts[2], "page"
        if len(parts) == 4 and parts[1] == "v" and parts[3] == "connect":
            return parts[2], "connect"
        return None

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/health":
            self._send_bytes(
                b'{"status":"healthy"}',
                content_type="application/json",
            )
            return

        parsed = self._token_parts()
        if parsed is None:
            self._send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        token, action = parsed
        if not _verify_token(token, self.gateway.secret):
            self._send_error(HTTPStatus.GONE, "link expired")
            return

        if action == "connect":
            self._connect(token)
        else:
            self._page(token)

    def _page(self, token: str) -> None:
        frame_src = html.escape(self.gateway.vnc_origin, quote=True)
        connect_path = f"/v/{quote(token, safe='')}/connect"
        connect_path_html = html.escape(connect_path, quote=True)
        payload = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Arena 验证浏览器</title>
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-src 'self' {frame_src};">
  <style>
    :root {{ color-scheme: dark; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; min-height: 100vh; background: #111827; color: #f9fafb; }}
    #intro {{ display: grid; place-items: center; min-height: 100vh; text-align: center; }}
    .card {{ max-width: 34rem; padding: 2rem; }}
    button {{ border: 0; border-radius: .6rem; padding: .8rem 1.2rem;
              background: #2563eb; color: white; font-size: 1rem; cursor: pointer; }}
    button:hover {{ background: #1d4ed8; }}
    #frame {{ display: none; width: 100vw; height: 100vh; border: 0; }}
    .muted {{ color: #9ca3af; }}
  </style>
</head>
<body>
  <main id="intro">
    <div class="card">
      <h1>Arena 验证浏览器</h1>
      <p>点击下方按钮进入服务器浏览器，然后完成 CF/Turnstile 验证。</p>
      <button id="open" type="button">进入验证浏览器</button>
      <p class="muted">此链接为短时链接，验证完成后可关闭页面。</p>
    </div>
  </main>
  <iframe id="frame" title="服务器浏览器" referrerpolicy="no-referrer"></iframe>
  <script>
    document.getElementById("open").addEventListener("click", function () {{
      document.getElementById("intro").style.display = "none";
      const frame = document.getElementById("frame");
      frame.src = "{connect_path_html}";
      frame.style.display = "block";
    }});
  </script>
</body>
</html>
"""
        self._send_bytes(payload.encode("utf-8"), content_type="text/html; charset=utf-8")

    def _connect(self, token: str) -> None:
        password = self.gateway.password
        if not password:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "gateway is not configured")
            return
        target = _vnc_url_with_password(self.gateway.vnc_url, password)
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, GatewayHandler)
        self.secret = _read_value(
            "NOVNC_GATEWAY_SECRET_FILE",
            "NOVNC_GATEWAY_SECRET",
        )
        self.password = _read_value(
            "NOVNC_PASSWORD_FILE",
            "NOVNC_PASSWORD",
        )
        self.vnc_url = os.environ.get("NOVNC_VNC_URL", "").strip()
        vnc_parts = urlsplit(self.vnc_url)
        self.vnc_origin = (
            f"{vnc_parts.scheme}://{vnc_parts.netloc}"
            if vnc_parts.scheme and vnc_parts.netloc
            else "'self'"
        )


def main() -> None:
    host = os.environ.get("NOVNC_GATEWAY_HOST", "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = max(1, min(65535, int(os.environ.get("NOVNC_GATEWAY_PORT", "6081"))))
    except ValueError:
        port = 6081
    server = GatewayServer((host, port))
    server.serve_forever()


if __name__ == "__main__":
    main()
