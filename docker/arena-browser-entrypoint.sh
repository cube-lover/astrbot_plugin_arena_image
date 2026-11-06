#!/usr/bin/env bash
set -euo pipefail
export DISPLAY=:99
export LANG="${LANG:-zh_CN.UTF-8}"
export LC_ALL="${LC_ALL:-zh_CN.UTF-8}"
STATE_DIR=/data/browser
PROFILE="$STATE_DIR/chrome-profile"
mkdir -p "$PROFILE"
if [ -z "${NOVNC_PASSWORD:-}" ] && [ -r "${NOVNC_PASSWORD_FILE:-}" ]; then
    NOVNC_PASSWORD="$(tr -d '\r\n' < "$NOVNC_PASSWORD_FILE")"
fi
if [ -z "${NOVNC_PASSWORD:-}" ]; then
    echo "NOVNC_PASSWORD or NOVNC_PASSWORD_FILE is required" >&2
    exit 1
fi
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonSocket" "$PROFILE/SingletonCookie"
Xvfb "$DISPLAY" -screen 0 1280x900x24 -ac +extension GLX +render -noreset >/tmp/arena-xvfb.log 2>&1 &
sleep 1
fluxbox >/tmp/arena-fluxbox.log 2>&1 &
x11vnc -storepasswd "${NOVNC_PASSWORD}" "$STATE_DIR/vnc.pass" >/dev/null
x11vnc -display "$DISPLAY" -forever -shared -rfbauth "$STATE_DIR/vnc.pass" -rfbport 5900 -localhost -noxdamage >/tmp/arena-x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/arena-novnc.log 2>&1 &
python3 /app/novnc_gateway.py >/tmp/arena-auth-gateway.log 2>&1 &
CHROME="${CHROME_BIN:-}"
if [ -z "$CHROME" ]; then
  CHROME="$(command -v google-chrome-stable || command -v google-chrome || true)"
fi
if [ -z "$CHROME" ]; then
  CHROME="$(find "${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}" /root/.cache/ms-playwright -type f \( -path "*/chrome-linux64/chrome" -o -path "*/chrome-linux/chrome" \) -print -quit 2>/dev/null || true)"
fi
if [ -z "$CHROME" ]; then
  echo "chromium executable not found" >&2
  exit 1
fi
PROXY_URL="${LM_BRIDGE_PROXY_URL:-${HTTP_PROXY:-}}"
proxy_args=()
CHROME_PROXY_URL="$PROXY_URL"
if [ -n "$PROXY_URL" ]; then
  # Chromium does not accept credentials embedded in --proxy-server URLs.
  # Start a local relay that adds Proxy-Authorization to the upstream proxy.
  case "$PROXY_URL" in
    http://*:*@*|https://*:*@*)
      # Pass the resolved fallback too; the relay must see the same proxy as
      # Chromium even when only HTTP_PROXY was configured.
      export LM_BRIDGE_PROXY_URL="$PROXY_URL"
      python3 /app/proxy_relay.py >/tmp/arena-proxy-relay.log 2>&1 &
      relay_pid=$!
      CHROME_PROXY_URL="http://127.0.0.1:18080"
      sleep 0.2
      if ! kill -0 "$relay_pid" 2>/dev/null; then
        echo "arena proxy relay failed; see /tmp/arena-proxy-relay.log" >&2
        exit 1
      fi
      ;;
  esac
  proxy_args=(
    --proxy-server="$CHROME_PROXY_URL"
    --proxy-bypass-list="localhost;127.0.0.1;arena-browser;arena-bridge;172.16.0.0/12;192.168.0.0/16;10.0.0.0/8"
  )
  echo "arena browser proxy configured" >&2
fi
run_chrome() {
  while true; do
    rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonSocket" "$PROFILE/SingletonCookie"
    "$CHROME" \
      --no-sandbox \
      --no-first-run \
      --no-default-browser-check \
      --lang=zh-CN \
      --remote-allow-origins=http://127.0.0.1:9222,http://localhost:9222 \
      --remote-debugging-port=9222 \
      --remote-debugging-address=127.0.0.1 \
      --user-data-dir="$PROFILE" \
      "${proxy_args[@]}" \
      --window-position=0,0 \
      --window-size=1280,900 \
      --restore-last-session \
      "https://arena.ai/?mode=direct" \
      >>/tmp/arena-chromium.log 2>&1 || true
    echo "arena browser exited; restarting" >>/tmp/arena-browser-watch.log
    sleep 3
  done
}
run_chrome &
python3 - <<'PY' >/tmp/arena-cdp-proxy.log 2>&1 &
import asyncio
async def pipe(r, w):
    try:
        while d := await r.read(65536):
            w.write(d)
            await w.drain()
    except Exception:
        pass
async def handler(cr, cw):
    sw = None
    try:
        sr, sw = await asyncio.open_connection("127.0.0.1", 9222)
        hd = b""
        while b"\r\n\r\n" not in hd:
            hd += await asyncio.wait_for(cr.read(4096), 10)
        hd, rem = hd.split(b"\r\n\r\n", 1)
        lines = hd.split(b"\r\n")
        ws = any(x.lower().startswith(b"upgrade: websocket") for x in lines)
        out = []
        for x in lines:
            lx = x.lower()
            if lx.startswith(b"host:"):
                out.append(b"Host: 127.0.0.1:9222")
            elif lx.startswith(b"origin:"):
                out.append(b"Origin: http://127.0.0.1:9222")
            else:
                out.append(x)
        sw.write(b"\r\n".join(out) + b"\r\n\r\n" + rem)
        await sw.drain()
        if ws:
            cw.write(await sr.readuntil(b"\r\n\r\n"))
            await cw.drain()
            await asyncio.gather(pipe(cr, sw), pipe(sr, cw), return_exceptions=True)
        else:
            rh = await sr.readuntil(b"\r\n\r\n")
            hl = rh[:-4].split(b"\r\n")
            n = 0
            for x in hl:
                if x.lower().startswith(b"content-length:"):
                    n = int(x.split(b":", 1)[1].strip())
            body = await sr.readexactly(n) if n else b""
            body = body.replace(b"127.0.0.1:9222", b"arena-browser:9223")
            body = body.replace(b"localhost:9222", b"arena-browser:9223")
            hl = [b"Content-Length: " + str(len(body)).encode() if x.lower().startswith(b"content-length:") else x for x in hl]
            cw.write(b"\r\n".join(hl) + b"\r\n\r\n" + body)
            await cw.drain()
    except Exception as e:
        print(f"proxy 9223: {e}", flush=True)
    finally:
        cw.close()
        if sw:
            sw.close()
async def main():
    server = await asyncio.start_server(handler, "0.0.0.0", 9223)
    async with server:
        await server.serve_forever()
asyncio.run(main())
PY
echo "arena dedicated browser ready" >&2
wait
