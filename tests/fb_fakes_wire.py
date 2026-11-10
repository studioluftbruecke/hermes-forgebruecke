"""A fake forge behind a real socket on 127.0.0.1, so a test runs the client's own urllib transport."""
from __future__ import annotations

import http.server
import socket
import threading

from fb_fakes import FakeForgejo, bot_account, make_token, sample_data

BOT = make_token("bot")


class LoopbackForge:
    """``fault(method, path)`` names what goes wrong with one request after the fake forge handled it:
    ``"hang up"`` (the connection closes without an answer), ``"cut"`` (the answer's body stops early),
    ``"stall"`` (the answer waits until the test is over) or None."""

    def __init__(self, fault=lambda method, path: None):
        self.fake = FakeForgejo({BOT: bot_account()}, data=sample_data())
        self._over = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def _serve(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
                status, headers, payload = outer.fake(self.command, f"http://127.0.0.1{self.path}",
                                                      {"Authorization": self.headers.get("Authorization", "")}, body, 5)
                kind = fault(self.command, self.path)
                if kind == "hang up":
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if kind == "stall":
                    outer._over.wait(30)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Total-Count", headers.get("x-total-count", ""))
                self.send_header("Content-Length", str(len(payload) + (40 if kind == "cut" else 0)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _serve

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._over.set()
        self.server.shutdown()
        self.server.server_close()


def closed_port() -> int:
    """A port on 127.0.0.1 that nothing listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
