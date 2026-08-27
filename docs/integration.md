# Attaching to Citadel

Three seams. Diffs are written against `Squidgy-AI/citadel@main`; line numbers
are where the anchors sit today, not where the edit lands.

Every change is removable by deleting it. No OpenBox type appears in an
`engine/` signature, no `engine/` module imports `openbox_citadel` at module
scope, and `tests/security/test_tool_boundary.py` is untouched.

---

## Seam 0 — startup

`engine/api/app.py`, once per process.

```python
_MIDDLEWARE = None


def _init_governance() -> None:
    """OpenBox governance. Not initializing IS the off switch — no second flag."""
    global _MIDDLEWARE
    import os

    if os.environ.get("OPENBOX_ENABLED", "").lower() not in ("1", "true"):
        return

    from openbox_citadel import create_openbox_citadel_middleware

    from engine.crew.cancellation import FlowCancelled
    from engine.tools.guard import ToolAccessDenied

    _MIDDLEWARE = create_openbox_citadel_middleware(
        deny_exc=ToolAccessDenied,
        # A halt is not a denial. Ships together with the `_run` branch in
        # seam 1c — wiring one without the other is worse than neither.
        halt_exc=lambda reason, ctx: FlowCancelled(
            getattr(ctx, "run_id", None) or "unknown-run"
        ),
        # fail_open in dev, fail_closed in prod. An unavailable policy decision
        # is not an allow, but it must not take dev down either.
        on_api_error=os.environ.get("OPENBOX_ON_API_ERROR", "fail_open"),
        # Layer 2: real OTel spans from httpx/asyncpg, mapped to each activity.
        instrument_http=True,
        instrument_databases=True,
    )
```

The factory installs OTel instrumentation once per process. That is what makes
spans exist at all — they are captured from real HTTP and DB calls, never
authored by hand.

`deny_exc=ToolAccessDenied` is the load-bearing argument. Without it a `block`
verdict raises `GovernanceBlockedError`, and `engine/` would need an OpenBox
`except` clause — at which point the integration is no longer removable by
deleting the wrap.

---

## Seam 1 — tools

Three edits, in two files. 1a and 1b are the tool boundary; 1c is what a halt
means on the campaign path.

### 1a. The loader

**`engine/tools/loader.py`, `bind()` (line 153).**

This is the site where every bound tool in the system is built. `run_turn`,
`engine/crew/sources.py` and `engine/tools/impl/flows.py` all reach it through
`routes_chat.bind_tools()`, and none constructs a `BoundTool` itself.

```diff
     def bind(
         self,
         agent: ResolvedAgent,
         ctx: ToolContext,
         *,
         include_disabled: bool = False,
+        mw: Any = None,
     ) -> list[BoundTool]:
@@
             call = self._make_callable(decl)
+            if mw is not None:
+                # INSIDE the D5 guard applied below: guarded() stays outermost.
+                call = mw.govern(decl, call)
             bound.append(
                 BoundTool(
                     name=decl.name,
                     description=decl.description,
                     decl=decl,
                     call=guarded(decl.name, ctx)(call),
                 )
             )
```

and thread `run` through the funnel in `engine/api/routes_chat.py` (~line 85):

```diff
 def bind_tools(
     agent: Any,
     state: EngineState,
     services: Any,
     *,
     campaign_id: str | None = None,
+    mw: Any = None,
 ) -> tuple[list[Any], Any]:
@@
-        tools = services.tools.bind(agent, ctx)
+        tools = services.tools.bind(agent, ctx, mw=mw)
```

### 1b. Why D5 must stay outermost

Two reasons, and the second is the one that actually bit.

**Independence.** `engine/tools/guard.py` says it plainly: the in-process MCP
server has no network boundary behind it, and the grant check is *"the ONLY
thing standing between a hallucinated tool name and execution."* It must never
become contingent on an external service being reachable.

**No wasted decisions.** If governance ran first, every call D5 was always going
to refuse would still be evaluated by OpenBox — and a `require_approval` verdict
would page a human to approve an action that cannot execute. This is not
hypothetical. The first version of this SDK wrapped governance *outside* the
guard, and the demo caught it immediately: an ungranted `Send_Invoice` call sat
in a **ten-second approval poll** before D5 refused it. Composed correctly, the
same call is refused in **7 ms** with no governance round trip at all.

`tests/test_tools.py::test_d5_runs_before_governance` pins the order as
`["d5", "openbox", "execute"]`, and
`test_d5_denial_never_reaches_openbox` asserts the client sends nothing on a D5
refusal.

### 1c. The campaign path: a halt must cancel the run

**`engine/crew/executor.py`, `CrewToolAdapter.adapt()._run` (~line 381).**

Seam 1 covers campaigns without a second wrapper — `run_crew_step` takes its
tools from the loader like everything else, and the sync `_run` adapter marshals
the same governed `BoundTool.call` onto the owning loop. Verified, including that
span attribution survives the thread hop.

One thing does **not** carry over: a `halt`.

`_run` catches `ToolAccessDenied` and records it on `adapter.denied`, which makes
`run_crew_step` fail the **step**. That is right for a `block` — one action
refused, the run continues. It is wrong for a `halt`, which means stop
everything. Without a branch of its own, a halt either collapses into a step
denial (the run proceeds to the next step) or, once `halt_exc` is wired, escapes
`_run` entirely and gets stringified for the model — the failure `guard.py`
forbids:

> "the raise alone is not enough (CrewAI stringifies tool exceptions for the
> model), so the record on the adapter is what makes `run_crew_step` fail the
> whole step closed"

```diff
                 except ToolAccessDenied as exc:
                     # Recorded so run_crew_step can fail the STEP. Re-raised
                     # too — if CrewAI stringifies it, the record still wins.
                     adapter.denied.append(DeniedCall(tool.name, exc))
                     raise
+                except FlowCancelled as exc:
+                    # A governance HALT, not a denial: stop the run, not the
+                    # step. Recorded the way an out-of-band cancel is, so
+                    # run_crew_step's `adapter.cancelled` check ends the run
+                    # with a terminal state of `cancelled`, never `done`.
+                    adapter.cancelled = exc
+                    raise
```

No new machinery: `run_crew_step` already checks `adapter.cancelled` **before**
`adapter.denied` and raises it, and `CancelWatch` already treats that state as
sticky and terminal. This only routes a policy halt into the path an operator
cancel already takes.

**`halt_exc` and this branch ship together.** Measured, all three ways:

| | raised | `adapter.cancelled` | `adapter.denied` | outcome |
|---|---|---|---|---|
| Neither | `ToolAccessDenied` | — | 1 | step fails, **run continues** |
| Both | `FlowCancelled` | **set** | 0 | **run cancelled** ✅ |
| `halt_exc` only | `FlowCancelled` | — | 0 | **escapes unrecorded** ✗ |

The third row is the trap: wiring `halt_exc` on its own is worse than leaving it
unset, because the halt no longer lands in `denied` and nothing else records it.

`verify/crew_halt.py` covers all three rows against the real `FlowCancelled`,
`ToolContext` and `ToolLoader`.

### 1d. If the loader cannot be edited

`mw.govern_tools(tools)` wraps an already-bound list at the
`routes_chat.bind_tools()` return instead. It is one line rather than two edits,
but it necessarily places governance outside D5 and pays both costs above. The
tool boundary still holds — D5 is inner, so it always runs — but prefer the
loader seam.

## Seam 2 — the envelope

**`engine/api/routes_chat.py`, `run_turn()` (~line 128).**

```diff
     agent: Any = None
     history: list[Any] = []
+
+    mw = _MIDDLEWARE
+    if mw is not None:
+        await mw.before_turn(workflow_type="chat", goal=state.get("user_mssg"))
```

and in the same `finally` that already dispatches persistence:

```diff
     finally:
+        if mw is not None:
+            await mw.after_turn()
```

`goal` is what drift detection compares the run against; omit it and
`goal_drifted` is never evaluated.

`after_turn()` unregisters the span buffer from a `finally`, so a failing
terminal send cannot leak it for the life of the process.

### The sequence is not negotiable

```
ActivityStarted
  -> register_activity     (maps layer-2 spans onto this activity)
  -> enforce verdict       (block / halt / require_approval)
  -> apply input redaction (guardrails may have rewritten the arguments)
  -> execute
ActivityCompleted          (awaited, never detached)
  -> enforce verdict       (output-stage guardrails, behavior rules)
  -> clear_activity
```

An earlier version of this SDK detached `ActivityCompleted` to keep it off the
streaming hot path (D9). That is wrong, in three ways it does not announce:

* Completions interleave — the second half of one pair lands after the next
  pair opens, and the event stream no longer reads as nested activities.
* Output-stage enforcement is skipped, making every stage-1 guardrail advisory.
* The terminal event can overtake a completion, so it arrives against a session
  the server has already closed.

`workflow_id` and `run_id` are generated once in `before_turn()`. `activity_id`
is generated once per action and **reused** by both halves of the pair — the
server upserts a single activity row, with `created_at` from the start and
`updated_at` from the completion. Suffixing the completion id creates a spurious
second row.

## Seam 3 — model calls

Optional for a pilot. Tool calls are the side-effecting actions; model calls are
telemetry plus prompt-stage guardrails.

Citadel routes every model call through OpenRouter over `httpx`, so once
`instrument_http=True` is set at seam 0, those calls already produce real OTel
spans carrying `http.method` and an LLM-domain `http.url`. They are attributed
to whichever activity is registered when they fire. No `turn.py` edit is needed
for spans.

An explicit model-call activity — one that can `block` a completion before any
tokens are spent — is not in this version. Campaigns need no seam-3 edit either:
`run_crew_step` takes its tools from the loader like everything else, so seam 1
already covers CrewAI's kickoff, including the sync `_run` adapter.

## Rollout

Matches the doc's four stages, and each is a config change rather than a code
change after seam 0 lands.

| Stage | Config | Proves |
|---|---|---|
| 1. Monitor | `OPENBOX_ENABLED=true`, `fail_open`, no guardrails configured | The wrapper does not change replies |
| 2. Enforce on tools | attach stage-0 guardrails to one agent | Denials raise as `ToolAccessDenied`; `test_tool_boundary.py` still passes untouched |
| 3. Campaigns | apply seam 1c, raise `approval_max_wait_seconds` for flow runs | A halt cancels the run; checkpointed steps can wait longer than a streamed turn |
| 4. Widen | per agent, the way `agents.webhook_url` widened (D13) | No flag day |

## What this does not touch

* **Billing.** `User_token_Usage_Logs` stays the source of truth for money. The
  SDK reports no cost and writes no token counts.
* **Migrations.** No new column, no Bytebase PR. `workflow_id` and `run_id` are
  generated per turn and live only in the event stream. D2 holds.
* **The approval UI.** Citadel's `chat_control.interpret_reply` gate is
  untouched. Wiring OpenBox's risk tier to *drive* that gate rather than
  standing up a second approval surface is the open design question — see
  `docs/protocol.md` § HITL.
