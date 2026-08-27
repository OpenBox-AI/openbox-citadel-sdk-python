# OpenBox Citadel SDK — Python

Governance and audit for [Citadel](https://github.com/Squidgy-AI/citadel), 4142's
agent engine. Wraps Citadel's tool boundary and model calls in the OpenBox
workflow envelope: every action is authorized before it runs and recorded after.

## Try it first

[`examples/citadel-demo`](examples/citadel-demo) is a working sales agent wired to
OpenBox — it looks up a company, finds a contact, issues an invoice, and asks a
human before the money moves. It runs in a browser, and it is the fastest way to
see what this SDK does before reading how it does it.

```bash
cd examples/citadel-demo
python setup_openbox.py       # creates the agent and its controls
python serve_demo.py          # http://127.0.0.1:8010
```

## Install

```bash
pip install openbox-citadel-sdk-python
```

## Use

```python
from openbox_citadel import create_openbox_citadel_middleware
from engine.tools.guard import ToolAccessDenied

mw = create_openbox_citadel_middleware(deny_exc=ToolAccessDenied)  # once, at startup

await mw.before_turn(workflow_type="chat", goal=user_message)
try:
    tools = loader.bind(agent, ctx, mw=mw)   # governance inside guarded()
    ...                                      # the turn
finally:
    await mw.after_turn()
```

`initialize()` reads `OPENBOX_API_URL`, `OPENBOX_API_KEY`, `OPENBOX_AGENT_DID`,
`OPENBOX_AGENT_PRIVATE_KEY` and `OPENBOX_ON_API_ERROR` from the environment —
which in Citadel means Doppler, project `squidgy`, never a `.env`.

Not calling `initialize()` *is* the off switch: `new_run()` returns `None` and
every wrapper degrades to a passthrough.

## Citadel's invariants, and how this SDK honours them

| Invariant | Where it comes from | What the SDK does |
|---|---|---|
| **D5 fails closed by raising** | `guard.py` — a denial returned as a string is one the model can rephrase around | `deny_exc=ToolAccessDenied` makes governance raise Citadel's own type. No OpenBox type enters an `engine/` signature |
| **D5 is not contingent on us** | in-process MCP has no network boundary behind it | `governed()` composes *outside* `guarded()`, so an OpenBox outage under `fail_open` cannot disable the grant check |
| **D9 streams first, persists after** | `respond` closes the body, then detaches writes | Authorize blocks; `ActivityCompleted` detaches, drained by `aclose()` with a bounded timeout |
| **Nothing dangles** | protocol: an unpaired activity orphans the session | Completion fires from `finally`, on success, exception, denial and cancellation alike |
| **Billing is not ours** | `User_token_Usage_Logs` is the source of truth for money | The SDK reports no cost and writes no token counts |

## Layout

| Module | Lines | Does |
|---|---|---|
| `event_sequence.py` | 229 | monotonic timestamps, `sequence`, violation detection, dangling activities |
| `tool_hook.py` | 240 | the tool flow: evaluate → register → execute → complete, with the approval retry loop |
| `middleware.py` | 218 | `OpenBoxCitadelMiddleware`, options, the envelope, OTel setup |
| `verdict.py` | 156 | six arms, guardrails, the remediation patch, error unwrapping |
| `client.py` | 136 | evaluate and approval polling (keyed by Core's approval id) |
| `activity_registry.py` | 114 | span attribution scope, approval and abort flags |
| `types.py` | 106 | wire types, `hex_id`, structured `ErrorInfo`, patch helpers |
| `events.py` | 98 | event construction, ordering check, orphan closure |
| `hitl.py` | 93 | the poll loop — an absent decision is *pending*, never `allow` |
| `middleware_factory.py` | 90 | `create_openbox_citadel_middleware` |
| `config.py` | 40 | `GovernanceConfig`, `HITLConfig` |

Ported from `openbox-openrouter-sdk`, which is the reference for the protocol
details Core actually enforces.

## Ordering

Core orders a session's events by the timestamp the SDK stamps, and the
dashboard renders that order literally. Two things break it, and both are
handled in `event_sequence.py`:

* **Millisecond ties.** A parallel tool round emits several `ActivityStarted`
  inside one millisecond; tied timestamps have no defined order, so the timeline
  can show a tool completing before the one before it started.
* **Non-monotonic clocks.** An NTP correction mid-run reorders everything after it.

Every event gets a strictly increasing timestamp within its run plus a gapless
`sequence` number. Sending is *not* serialized — each POST costs ~840ms against
a real Core, so chaining a three-tool round would triple its latency to fix a
display problem.

## HITL

An approval-poll response carrying no `arm`/`verdict`/`action` means Core has
not recorded a human decision — it is **pending**, not approved. Normalizing
that absence to `allow` (correct for an evaluate response, where unset means
"no restriction stated") resolves the loop on its first tick, before anyone
approved anything. Polls are keyed by **Core's** `approval_id`, not the local
activity id.

## Docs

* `docs/integration.md` — the three Citadel seams, with exact diffs
* `docs/protocol.md` — the wire contract and the traps in it
