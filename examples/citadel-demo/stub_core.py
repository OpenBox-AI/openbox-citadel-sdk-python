"""A scripted OpenBox Core, for exercising verdicts the live instance won't produce.

An agent with no policy attached gets `allow` for everything, which exercises
none of the enforcement path. This stub returns scripted verdicts per
`activity_type` instead, so the demo can be shown handling `block`, `halt` and
`require_approval` without a policy — on a plane, or before your own policies
exist.

It is not a real server and proves nothing about your policies. Use it to see the
shapes; use `run_demo.py` against your instance to see the decisions.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8099

VERDICTS: dict[str, str] = {
    "Send_Invoice": "require_approval",
    "Apollo": "monitor",
}
APPROVALS: dict[str, str] = {}
LOG: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    # The SDK's client pools connections and keeps them alive. A server that
    # speaks HTTP/1.0 closes after every response, which surfaces in the client
    # as "Server disconnected without sending a response."
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence per-request stderr noise
        return

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        event = json.loads(raw or b"{}")
        LOG.append(event)

        if self.path.endswith("/governance/approval"):
            activity = event.get("activity_id", "")
            # First poll pends, second approves — a human taking a moment.
            seen = APPROVALS.get(activity, 0)
            APPROVALS[activity] = seen + 1
            if seen >= 1:
                return self._json({"arm": "allow", "reason": "approved by demo reviewer"})
            # Pending: NO arm/verdict/action field at all. A client that
            # normalizes that absence to "allow" resolves on the first tick.
            return self._json({"reason": "awaiting review"})

        verdict = "allow"
        if event.get("event_type") == "ActivityStarted":
            verdict = VERDICTS.get(event.get("activity_type", ""), "allow")

        patch = (
            {"new_input": {"customer": "c77", "amount_usd": 9500}}
            if verdict == "block"
            else None
        )
        return self._json({
            "governance_event_id": "stub-event",
            "arm": verdict,
            "verdict": verdict,
            "action": verdict,
            "patch": patch,
            "risk_score": 0.8 if verdict != "allow" else 0.1,
            "trust_tier": 3,
            "policy_id": "stub-policy-demo",
            "reason": {
                "block": "Delete_Account is prohibited for this agent's trust tier.",
                "require_approval": "Payments above $10,000 require human approval.",
                "monitor": "Contact enrichment is recorded but not gated.",
            }.get(verdict, ""),
            "approval_id": "stub-approval" if verdict == "require_approval" else None,
        })


def serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
