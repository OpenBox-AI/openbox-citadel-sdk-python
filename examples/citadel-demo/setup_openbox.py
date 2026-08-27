"""Create everything the demo needs, through the OpenBox platform API.

    python setup_openbox.py --dry-run     # show the exact calls, change nothing
    python setup_openbox.py               # create what is missing
    python setup_openbox.py --show        # what exists right now

Creates, and re-uses anything already there by name:

  * the agent                — the identity the demo runs as
  * a policy                 — REQUIRE_APPROVAL on Send_Invoice (scenario 4)
  * a guardrail              — PII on the way in (scenario 5)
  * a behavior rule          — an invoice with no prior contact lookup (scenario 6)

Policies are built the way the dashboard builds them — a `policy_builder` config
the backend compiles into Rego — not hand-written Rego. What you get here is what
you would get by clicking through Authorize → Policies, and it stays editable in
the UI afterwards.

Needs an organisation API key with the permissions listed in SETUP.md, in
OPENBOX_PLATFORM_API_KEY. That key is created once, in the dashboard.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

DEFAULT_API = "https://api.openbox.ai"
AGENT_NAME = "citadel demo"
POLICY_NAME = "citadel demo policy"
GUARDRAIL_NAME = "Inbound PII"
BEHAVIOR_RULE_NAME = "Invoice without a contact lookup"


# ── the controls, exactly as the dashboard would store them ────────────

POLICY_BUILDER = {
    "version": 2,
    "rules": [
        {
            "id": "rule-send-invoice",
            "name": "Invoices need a human",
            "decision": "REQUIRE_APPROVAL",
            "reason": "Invoices are issued by an agent and need a human reviewer",
            "matchMode": "all",
            "conditions": [
                {
                    "id": "cond-tool",
                    "left": {"kind": "field", "field": "input.activity_type",
                             "transform": "value", "valueType": "string"},
                    "operator": "equals",
                    "right": {"kind": "literal", "value": "Send_Invoice",
                              "valueType": "string"},
                },
                {
                    "id": "cond-stage",
                    # ActivityStarted only. Matching the completion too would ask
                    # a human to approve work that has already happened.
                    "left": {"kind": "field", "field": "input.event_type",
                             "transform": "value", "valueType": "string"},
                    "operator": "equals",
                    "right": {"kind": "literal", "value": "ActivityStarted",
                              "valueType": "string"},
                },
            ],
        },
        {
            "id": "rule-delete-account",
            "name": "Account deletion is never automatic",
            "decision": "BLOCK",
            "reason": "Deleting a customer account is not something an agent does unattended",
            "matchMode": "all",
            "conditions": [
                {
                    "id": "cond-delete",
                    "left": {"kind": "field", "field": "input.activity_type",
                             "transform": "value", "valueType": "string"},
                    "operator": "equals",
                    "right": {"kind": "literal", "value": "Delete_Account",
                              "valueType": "string"},
                },
            ],
        },
    ],
}

GUARDRAIL = {
    "guardrail_type": "1",      # PII
    "name": GUARDRAIL_NAME,
    "description": "Redact personal data in what the user sends the agent",
    "processing_stage": "0",    # input — evaluated on ActivityStarted
    "parameters": {},
}

BEHAVIOR_RULE = {
    "rule_name": BEHAVIOR_RULE_NAME,
    "description": (
        "An invoice may only go out after the contact at that company has been "
        "looked up. The lookup is the check that a real customer is being billed."
    ),
    "priority": 70,
    "trigger": "http_post",
    "trigger_match": [
        {"field": "http_url", "op": "contains", "value": "/invoices"},
    ],
    # Required prior state. Absent → the rule is violated.
    "states": [
        {
            "semantic_type": "http_post",
            "match": [{"field": "http_url", "op": "contains", "value": "/people/search"}],
        },
    ],
    "time_window": 300,
    "verdict": "BLOCK",
    "reject_message": "Invoice attempted with no prior contact lookup for this company",
}


# ── plumbing ───────────────────────────────────────────────────────────

USER_AGENT = "openbox-citadel-demo/0.1 (+https://openbox.ai)"
"""urllib's default (`Python-urllib/x.y`) is refused at the edge with a 403
and `error code: 1010`, before the request reaches the API. Any honest
identifier gets through; the point is simply not to send urllib's."""


class Api:
    def __init__(self, base: str, key: str, dry_run: bool) -> None:
        self.base = base.rstrip("/")
        self.key = key
        self.dry_run = dry_run

    def __call__(self, method: str, path: str, body: dict | None = None) -> object:
        url = f"{self.base}{path}"
        if self.dry_run and method != "GET":
            print(f"\n  {method} {url}")
            print("  " + json.dumps(body or {}, indent=2).replace("\n", "\n  "))
            return {"id": "<dry-run>", "dry_run": True}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"x-api-key": self.key, "Content-Type": "application/json",
                     "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:400]
            if exc.code == 403 and "error code: 1010" in detail:
                sys.exit(f"\n403 from {path} — blocked by the edge before the API "
                         f"saw the request, on the client signature.\n{detail}\n"
                         f"The API key was never evaluated. Send a User-Agent "
                         f"header (this script sets {USER_AGENT!r}).")
            if exc.code in (401, 403):
                sys.exit(f"\n{exc.code} from {path} — the API key is missing a "
                         f"permission, or is not an organisation key.\n{detail}\n"
                         f"See SETUP.md step 1 for the exact permission list.")
            raise SystemExit(f"\n{method} {path} failed: {exc.code}\n{detail}")


def rows_of(payload: object) -> list[dict]:
    """The rows out of any envelope the API wraps them in.

    Paginated endpoints answer `{status, data: {data: [...], total}}` — two
    `data` layers. Peeling one leaves the inner dict, which iterates as key
    strings, so every lookup silently finds nothing.
    """
    seen = 0
    while isinstance(payload, dict) and seen < 4:
        payload = payload.get("data") or payload.get("items") or []
        seen += 1
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def find_by(items: object, field: str, value: str) -> dict | None:
    for row in rows_of(items):
        if row.get(field) == value:
            return row
    return None


def load_env() -> None:
    env = pathlib.Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the calls that would be made, change nothing")
    parser.add_argument("--show", action="store_true",
                        help="report what already exists, create nothing")
    parser.add_argument("--api-url", default=None,
                        help=f"platform API base (default {DEFAULT_API})")
    args = parser.parse_args()

    load_env()
    base = args.api_url or os.environ.get("OPENBOX_PLATFORM_API_URL") or DEFAULT_API
    key = os.environ.get("OPENBOX_PLATFORM_API_KEY", "")
    if not key and not args.dry_run:
        sys.exit("OPENBOX_PLATFORM_API_KEY is not set — see SETUP.md step 1")

    api = Api(base, key, args.dry_run)
    read_only = args.show
    print(f"platform : {base}")

    # ── agent ──────────────────────────────────────────────────────────
    agents = api("GET", "/agent/list") if key else []
    agent = find_by(agents, "agent_name", AGENT_NAME) or find_by(agents, "name", AGENT_NAME)
    if agent:
        agent_id = agent.get("id")
        print(f"agent    : reusing {AGENT_NAME!r} ({agent_id})")
    elif read_only:
        print(f"agent    : MISSING — {AGENT_NAME!r} does not exist")
        return
    else:
        created = api("POST", "/agent/create", {
            "agent_name": AGENT_NAME,
            "agent_type": "temporal",
            "description": "Citadel-shaped demo agent, governed by OpenBox",
            "signing_required": True,
        })
        agent_id = created.get("id")
        print(f"agent    : created {AGENT_NAME!r} ({agent_id})")
        print("\n  Copy these into .env now — the private key is shown once:")
        for field, env_key in (("token", "OPENBOX_API_KEY"),
                               ("did", "OPENBOX_AGENT_DID"),
                               ("private_key", "OPENBOX_AGENT_PRIVATE_KEY")):
            if created.get(field):
                print(f"    {env_key}={created[field]}")

    if args.dry_run and not agent_id:
        agent_id = "<agent-id>"

    # ── policy ─────────────────────────────────────────────────────────
    policies = api("GET", f"/agent/{agent_id}/policies") if key else []
    policy = find_by(policies, "name", POLICY_NAME)
    if policy:
        print(f"policy   : reusing {POLICY_NAME!r} ({policy.get('id')})")
    elif not read_only:
        made = api("POST", f"/agent/{agent_id}/policies", {
            "name": POLICY_NAME,
            "description": "REQUIRE_APPROVAL on Send_Invoice, BLOCK on Delete_Account",
            "config": {"policy_builder": POLICY_BUILDER},
        })
        print(f"policy   : created {POLICY_NAME!r} ({made.get('id')})")
    else:
        print(f"policy   : MISSING")

    # ── guardrail ──────────────────────────────────────────────────────
    guardrails = api("GET", f"/agent/{agent_id}/guardrails") if key else []
    guardrail = find_by(guardrails, "name", GUARDRAIL_NAME)
    if guardrail:
        print(f"guardrail: reusing {GUARDRAIL_NAME!r} ({guardrail.get('id')})")
    elif not read_only:
        made = api("POST", f"/agent/{agent_id}/guardrails", GUARDRAIL)
        print(f"guardrail: created {GUARDRAIL_NAME!r} ({made.get('id')})")
    else:
        print(f"guardrail: MISSING")

    # ── behavior rule ──────────────────────────────────────────────────
    rules = api("GET", f"/agent/{agent_id}/behavior-rule") if key else []
    rule = find_by(rules, "rule_name", BEHAVIOR_RULE_NAME)
    if rule:
        print(f"behavior : reusing {BEHAVIOR_RULE_NAME!r} ({rule.get('id')})")
    elif not read_only:
        made = api("POST", f"/agent/{agent_id}/behavior-rule", BEHAVIOR_RULE)
        print(f"behavior : created {BEHAVIOR_RULE_NAME!r} ({made.get('id')})")
    else:
        print(f"behavior : MISSING")

    if not args.dry_run:
        print(f"\nReview them in the dashboard under the agent's Authorize tab.")
        print("Then: python serve_demo.py")


if __name__ == "__main__":
    main()
