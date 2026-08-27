# The wire contract, and the traps in it

Notes recorded while building this SDK. Everything here is a place where the
OpenAPI spec, the older generated types, or ordinary intuition disagrees with
the live server — always trust the implementation.

## Verdicts are four

`allow`, `require_approval`, `block`, `halt`. Lowercase on the wire.

`constrain` is in the spec and is never emitted; `Constraints[]` is never
populated. Do not write a `case "constrain"` branch. Same for
`drift_detection_action: 'constrain'` — only `alert_only` and `terminate` are
implemented.

Citadel mapping:

| Verdict | Citadel behaviour |
|---|---|
| `allow` | run the action |
| `require_approval` | poll, bounded — see HITL below |
| `block` | raise `ToolAccessDenied`; takes the turn down |
| `halt` | also raises today |

**`halt` is the open question.** It means *stop everything, end the session
immediately, no cleanup* — which is not the same as one turn erroring. This SDK
currently maps it onto the same denial path as `block`, which is correct-ish for
a chat turn and wrong for a campaign, where a halt should cancel the whole run
rather than fail one step. Decide before stage 3.

## `activity_input` must be an array

Spec says `oneOf: [array, object]`. The live server validates a list. An object
returns 422, or 500 when the rejection bubbles through unmapped. `_describe()`
always wraps.

## Spans are what make behavior rules fire

A span without the right gate attribute matches zero rules — the guardrail is
configured, never fires, and nothing in the response says so.

| Tool does | Attribute | Name must contain |
|---|---|---|
| HTTP | `http.method` | the verb |
| DB | `db.system` | the SQL verb |
| File | `file.path` | `file.read` / `file.write` |
| LLM | `http.method` + LLM-domain `http.url` | COMPLETION / TOOL / EMBED |

Citadel gets most of this free: `ToolDecl.backing` already carries `kind`,
`method` and `url`. Only `python_function` backings are opaque, and those need
an explicit `tool_type_map` entry. `spans.py` is the whole story.

The LLM case is the subtle one. The matcher classifies by the **domain of
`http.url`**, not the span name. Citadel routes through OpenRouter, so the span
must carry `https://openrouter.ai/api/v1`. Point it at an internal hostname and
every model call quietly demotes to a plain HTTP request.

## Stage gating

| `processing_stage` | Fires on | Prefix |
|---|---|---|
| `"0"` | `ActivityStarted` | `input.` |
| `"1"` | `ActivityCompleted` | `output.` |
| anything else, `"both"` included | nothing, silently | — |

This is why the envelope's completion half cannot be dropped, D9
notwithstanding: a stage-1 guardrail on tool output never runs if only
`ActivityStarted` is emitted. The SDK's answer is to send it *detached* rather
than not at all.

## HITL

`require_approval` is a poll loop against `POST /api/v1/governance/approval`,
not a synchronous return. Never auto-accept.

The base SDK polls indefinitely and lets the server own expiry. Citadel cannot
afford that on the chat path: the SPA aborts a silent stream at 90s, so an
approval outliving that budget produces a dead tab rather than a pending
decision. Hence `approval_max_wait_seconds`, default 75s, and a timeout is
treated as a **denial** — timing out into execution would make the gate
advisory.

Campaign steps are the opposite case. They are checkpointed and resumable, not
streamed, so they should raise or disable the bound.

**Still open:** Citadel already has an approval gate that posts into a group
chat and reads the user's reply (`engine/crew/chat_control.py`). Two approval
surfaces is one too many. The intended shape is OpenBox's risk tier *driving*
that existing gate — `require_approval` resolving through `interrupt()` and
`interpret_reply` instead of through this poll loop. Not built.

## Approval caching

A repeated span fingerprint after a prior approval short-circuits to `allow`
and skips downstream evaluators. Worth knowing before describing OpenBox as an
independent second check: sometimes it is a cache hit, not an evaluation. To
force a fresh decision, vary a span field inside the fingerprint.

This does not weaken Citadel's position — D5 runs on every call regardless — but
it should not be oversold either.

## `trust_tier` is an integer

1–4, or null. Some older generated types say string. The live server sends an
int.

There is no `alignment_score` at root; per-span alignment lives at
`age_result.span_results[].alignment_result.score`, and goal drift is the
boolean `age_result.goal_drifted`.

## `task_queue` is not an enum

Spec says `[langgraph, temporal, mastra]`; the live server accepts any string
with no validation, and new frameworks are expected to invent their own.
Citadel sends `citadel` — it is neither plain LangGraph nor plain CrewAI, and
filtering the dashboard by framework is only useful if the value is honest.

## `activity_type` matching is exact

Free-form `varchar(255)`, no whitelist, no wildcards. A guardrail configured for
`LLMCompleted` will not match a client sending `LLMCompletion`.

Citadel sends the **tool's declared name** as `activity_type` — `Apollo`,
`Web_Analysis`, `Serach_in_knowledge_base`. That last one carries a live typo
and `'Listing agents '` really does end in a space, because those strings are
also the pricing keys. Guardrail config must reproduce them byte for byte,
typo and trailing space included.

Domain types are fully supported on the wire; what you give up is the SDK's
typed helpers and generic-fallback trust classification.
