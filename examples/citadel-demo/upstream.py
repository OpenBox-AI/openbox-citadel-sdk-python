"""A local stand-in for the APIs the demo's tools call.

Spans are not authored by the SDK — they are captured by OTel instrumentation
from real outbound calls. A tool that fakes its work with `asyncio.sleep` and
returns a dict produces no spans, however the SDK is configured. So the demo
needs something genuine to call.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8098

RESPONSES: dict[str, dict] = {
    "/analyse": {"summary": "Acme is a mid-market logistics SaaS.", "employees": 240},
    "/people/search": {"people": [{"name": "Dana Reyes", "title": "VP Ops"}]},
    "/invoices": {"invoice_id": "inv_9001", "status": "sent"},
    "/accounts": {"deleted": True},
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def _respond(self) -> None:
        path = self.path.split("?")[0]
        body = json.dumps(RESPONSES.get(path, {"ok": True})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        self._respond()

    def do_DELETE(self) -> None:
        self._respond()


def serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


BASE = f"http://127.0.0.1:{PORT}"
