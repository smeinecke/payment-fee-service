from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse


class _CallbackHandler(BaseHTTPRequestHandler):
    expected_token: ClassVar[str] = ""
    state: ClassVar[dict[str, str]] = {}

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default logging to avoid leaking query strings.
        pass

    def _respond(self, code: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        token = None
        if parsed.query:
            params = parse_qs(parsed.query)
            token_list = params.get("token")
            if token_list:
                token = token_list[0]

        if parsed.path == "/paypal/return":
            if token and token == self.expected_token:
                self.state["status"] = "approved"
                self.state["token"] = token
                self._respond(200, b"Payment approved. You may close this window.")
                return
            self.state["status"] = "token_mismatch"
            self._respond(403, b"Invalid or missing token.")
            return

        if parsed.path == "/paypal/cancel":
            if token and token == self.expected_token:
                self.state["status"] = "cancelled"
                self._respond(200, b"Payment cancelled.")
                return
            self.state["status"] = "cancelled"
            self._respond(200, b"Payment cancelled.")
            return

        self._respond(404, b"Not found.")


class CallbackServer:
    def __init__(self, expected_token: str) -> None:
        self.expected_token = expected_token
        self.state: dict[str, str] = {"status": "pending"}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = self._find_port()

    def _find_port(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    @property
    def return_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/paypal/return"

    @property
    def cancel_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/paypal/cancel"

    def start(self) -> None:
        self._update_handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _CallbackHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def update_expected_token(self, token: str) -> None:
        self.expected_token = token
        self._update_handler()

    def _update_handler(self) -> None:
        _CallbackHandler.expected_token = self.expected_token
        _CallbackHandler.state = self.state

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def wait_for_state(self, timeout: float = 120.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.state.get("status") in ("approved", "cancelled", "token_mismatch"):
                return self.state["status"]
            time.sleep(0.2)
        return "timeout"
