#!/usr/bin/env python3
"""Read-only HTTP status service for pc-guard.

Binds only to the addresses in the config (default: 127.0.0.1 and the
current Tailscale IPv4 address). GET /status is always served and has
no authentication.

If PC_GUARD_WAKE_TOKEN is set, POST /mark-woken is also served and
requires Authorization: Bearer <token>. On success it writes the
woken-by-bot flag so the watcher can auto-suspend later.
"""

from __future__ import annotations

import hmac
import os
import socket
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pc_guard

BIND_RETRY_SEC = 5
WAKE_TOKEN_ENV = "PC_GUARD_WAKE_TOKEN"
MAX_BODY = 4096
TAILSCALE_RETRY_SEC = 10


def _port() -> int:
    return int(pc_guard.config().status_port)


def _wake_token() -> str | None:
    t = os.environ.get(WAKE_TOKEN_ENV, "").strip()
    return t or None


def _auth_ok(header: str | None) -> bool:
    token = _wake_token()
    if not token or not header:
        return False
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1].strip().encode(), token.encode())


def _touch_woken_flag() -> tuple[bool, str]:
    path = pc_guard.woken_flag()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(datetime.now().astimezone().isoformat(timespec="seconds") + "\n")
        return True, str(path)
    except OSError as exc:
        return False, type(exc).__name__


class StatusHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pc-guard-status/1.0"

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/status":
            try:
                body = pc_guard.status_json().encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                raw = b'{"error": "status failed"}\n'
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)
                sys.stderr.write(f"status error: {exc}\n")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        self._deny(404, "Not found")

    def do_HEAD(self):  # noqa: N802
        self._deny(405, "Method not allowed")

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path != "/mark-woken":
            self._deny(405, "Method not allowed")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if 0 < n <= MAX_BODY:
            try:
                self.rfile.read(n)
            except OSError:
                pass
        elif n > MAX_BODY:
            self.close_connection = True
            self._deny(413, "Body too large")
            return
        if not _wake_token():
            self._deny(404, "Not found")
            return
        if not _auth_ok(self.headers.get("Authorization")):
            body = b'{"ok": false, "error": "unauthorized"}\n'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("WWW-Authenticate", 'Bearer realm="pc-guard-mark-woken"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        ok, detail = _touch_woken_flag()
        if not ok:
            sys.stderr.write("mark-woken failed: %s\n" % detail)
            body = b'{"ok": false, "error": "could not write woken-by-bot flag"}\n'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'{"ok": true}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        sys.stderr.write("mark-woken accepted from %s\n" % self.address_string())

    def do_PUT(self):  # noqa: N802
        self._deny(405, "Method not allowed")

    def do_DELETE(self):  # noqa: N802
        self._deny(405, "Method not allowed")

    def do_PATCH(self):  # noqa: N802
        self._deny(405, "Method not allowed")

    def do_OPTIONS(self):  # noqa: N802
        self._deny(405, "Method not allowed")

    def _deny(self, code: int, msg: str) -> None:
        body = (msg + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        path = ""
        try:
            path = str(args[0]) if args else ""
        except Exception:
            path = ""
        if "/status" in path and "200" in (fmt % args if args else ""):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class ReuseHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


def _serve(host: str, port: int, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            httpd = ReuseHTTPServer((host, port), StatusHandler)
        except OSError as exc:
            sys.stderr.write(f"bind {host}:{port} failed ({exc}); retry in {BIND_RETRY_SEC}s\n")
            time.sleep(BIND_RETRY_SEC)
            continue
        sys.stderr.write(f"listening on {host}:{port}\n")
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        while not stop.is_set() and thread.is_alive():
            time.sleep(0.5)
        httpd.shutdown()
        httpd.server_close()
        break


def _serve_tailscale(port: int, stop: threading.Event, already: set[str]) -> None:
    """Bind to the Tailscale IPv4 address, retrying until it appears."""
    bound = None
    while not stop.is_set():
        ip = pc_guard.tailscale_ipv4()
        if ip and ip not in already:
            if bound != ip:
                sys.stderr.write(f"tailscale address {ip}; binding\n")
            _serve(ip, port, stop)
            return
        time.sleep(TAILSCALE_RETRY_SEC)


def main() -> int:
    port = _port()
    hosts = pc_guard.resolve_bind_hosts()
    want_tailscale = any(
        t.lower() in {"tailscale", "tailscale0"} for t in pc_guard.config().bind_hosts
    )
    sys.stderr.write(f"pc-guard status starting on {hosts} port {port}\n")
    if _wake_token():
        sys.stderr.write("POST /mark-woken is enabled (token is set)\n")
    else:
        sys.stderr.write("POST /mark-woken is disabled (PC_GUARD_WAKE_TOKEN unset)\n")
    stop = threading.Event()
    already = set(hosts)
    threads = []
    for host in hosts:
        t = threading.Thread(target=_serve, args=(host, port, stop), daemon=True)
        t.start()
        threads.append(t)
    if want_tailscale and pc_guard.tailscale_ipv4() is None:
        t = threading.Thread(
            target=_serve_tailscale, args=(port, stop, already), daemon=True
        )
        t.start()
        threads.append(t)
        sys.stderr.write("tailscale address not up yet; will bind when it appears\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
