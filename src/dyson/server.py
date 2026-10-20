"""Loopback-only authenticated local dashboard and JSON API."""
from __future__ import annotations

import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlsplit

from .backend import BackendError
from .config import ConfigError, ConflictError

MAX_BODY = 2 * 1024 * 1024


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, runtime, token=None):
        if address[0] != "127.0.0.1":
            raise ConfigError("dashboard must bind to 127.0.0.1")
        self.runtime = runtime
        self.token = token or secrets.token_urlsafe(32)
        self.capacity = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.capacity.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.capacity.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.capacity.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "DYSON/0.2"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Access paths can contain secrets in user requests; keep logs in runtime telemetry.
        return

    def send_data(self, status, payload, content_type="application/json", nonce=None):
        data = payload if isinstance(payload, bytes) else json.dumps(
            payload, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Connection", "close")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; connect-src 'self'; "
                         + (f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; " if nonce else "")
                         + "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def check_host(self):
        port = self.server.server_address[1]
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.send_data(403, {"error": "invalid Host"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {
                f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            self.send_data(403, {"error": "cross-origin request rejected"})
            return False
        return True

    def authenticated(self):
        raw = self.headers.get("Authorization", "")
        supplied = raw[7:] if raw.startswith("Bearer ") else self.headers.get("X-Dyson-Token", "")
        if not hmac.compare_digest(supplied.encode(), self.server.token.encode()):
            self.send_data(401, {"error": "DYSON API token required"})
            return False
        return True

    def do_GET(self):
        if not self.check_host():
            return
        path = urlsplit(self.path).path
        if path == "/":
            nonce = secrets.token_hex(16)
            html = files("dyson").joinpath("dashboard.html").read_text(encoding="utf-8")
            html = html.replace("__DYSON_TOKEN__", self.server.token).replace("__NONCE__", nonce)
            self.send_data(200, html.encode(), "text/html; charset=utf-8", nonce)
            return
        if not self.authenticated():
            return
        try:
            if path == "/api/status":
                self.send_data(200, self.server.runtime.status())
            elif path == "/api/health":
                self.send_data(200, {"backend": self.server.runtime.health()})
            else:
                self.send_data(404, {"error": "unknown route"})
        except BackendError as exc:
            self.send_data(502, {"error": str(exc)})

    def do_POST(self):
        if not self.check_host() or not self.authenticated():
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ConfigError("chunked requests are unsupported")
            if self.headers.get_content_type() != "application/json":
                raise ConfigError("Content-Type must be application/json")
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 < length <= MAX_BODY:
                raise ConfigError("request body must be between 1 byte and 2 MiB")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ConfigError("incomplete request body")
            def invalid_constant(value):
                raise ConfigError(f"nonfinite JSON constant: {value}")
            obj = json.loads(raw, parse_constant=invalid_constant)
            if not isinstance(obj, dict):
                raise ConfigError("request body must be an object")
            path = urlsplit(self.path).path
            allowed = {
                "/api/config": {"values", "revision"},
                "/api/chat": {"messages", "session_id", "tools"},
                "/api/feedback": {"request_id", "changed", "completed", "level"},
            }
            if path not in allowed:
                self.send_data(404, {"error": "unknown route"})
                return
            if set(obj) - allowed[path]:
                raise ConfigError("request contains unsupported fields")
            runtime = self.server.runtime
            if path == "/api/config":
                result = runtime.configure(obj.get("values"), obj.get("revision"))
            elif path == "/api/chat":
                result = runtime.chat(obj.get("messages"), obj.get("session_id", "default"),
                                      obj.get("tools"))
            else:
                result = runtime.feedback(obj.get("request_id"), obj.get("changed"),
                                          obj.get("completed", False), obj.get("level"))
            self.send_data(200, result)
        except ConflictError as exc:
            self.send_data(409, {"error": str(exc)})
        except (ConfigError, ValueError, UnicodeError) as exc:
            self.send_data(400, {"error": str(exc)})
        except BackendError as exc:
            self.send_data(502, {"error": str(exc)})
        except OSError:
            self.send_data(500, {"error": "local I/O failed; check state directory and disk space"})
