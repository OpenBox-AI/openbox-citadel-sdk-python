# Citadel demo agent

A small B2B sales agent — it looks up a company, finds a contact, issues an
invoice — wired to OpenBox through
[`openbox-citadel-sdk-python`](../../README.md).

It exists to answer one question you cannot answer by reading an SDK: *what does
governance actually feel like in a running agent?* So the agent is real. It calls
a real model, makes real HTTP calls, and every decision you see on screen came
back from your OpenBox instance a few hundred milliseconds earlier.

Set-up is one API key and one command — see **[SETUP.md](SETUP.md)**. Then:

```bash
python serve_demo.py          # http://127.0.0.1:8010
```

---

## Six things to try

Paste each prompt into the page and watch what comes back. They build on each
other, so go in order.

### 1. Watch a tool get chosen

> A lead came in from acme-logistics.com. Analyse their website and tell me in
> one sentence what they do.

The chips along the top are the agent's tool catalogue. Three of them are
granted; `Delete_Account` is struck through, because it is declared but not
granted to this agent. The model picks from what it was given, the tool runs, and
every step arrives with a verdict beside it.

Nothing here is gated yet. This is what "governed and allowed" looks like — which
is most of what governance does, most of the time.

### 2. Ask for something the agent cannot do

> The customer at acme-logistics.com wants their account deleted permanently.
> Please delete it.

It will decline. That is not the interesting part — models decline things all
day, and a model that declines can be talked round.

The interesting part is that `Delete_Account` was never built for this agent, so
it never reached the tool list the model can see. There is nothing to call. Try
to argue it into deleting the account; you are arguing with a model about a
capability that does not exist in its context.

That distinction — *not offered* versus *asked not to* — is the one worth taking
away from this demo.

### 3. Move some money

> Find the operations contact at acme-logistics.com, then issue an invoice to
> customer cus_77 for $18,500.

`Apollo` runs and finds the contact. Then `Send_Invoice` stops, and the page
offers **Approve** and **Deny**.

Read the ledger at the bottom before you touch either button: `Apollo` fired,
`Send_Invoice` did not. The tool has not run. It is waiting on you.

Deny it. The turn ends with the reason your policy gave, and the ledger still
shows only `Apollo` — the refusal cost the agent an action, not a rollback. Run
it again and approve, and the invoice goes out.

The Approve and Deny buttons make the same API call the dashboard's approvals
view makes. Approving there works identically; the buttons just save you a tab.

### 4. Put personal data in the prompt

> Invoice Dana Reyes at dana.reyes@acme-logistics.com, phone 415-555-0142, for
> $4,200 — customer cus_77.

The PII guardrail evaluates the input side, before the agent acts on it. What
reaches the tool is redacted; the SDK swaps the redacted arguments in rather than
passing the originals through. Open the session afterwards and compare what you
typed against what the activity recorded.

### 5. Break a sequence

> Issue an invoice to customer cus_88 for $9,900.

Look at what is missing: no contact lookup. The behavior rule from SETUP.md
requires a `/people/search` within five minutes of any post to `/invoices` —
because the lookup is what establishes there is a real customer being billed.

Scenario 3 satisfied that rule. This one does not. This is the class of control
that reads a *sequence* of calls rather than a single one, which is where most
interesting agent misbehaviour actually lives.

### 6. Turn governance off

```bash
python run_demo.py --off
```

Same agent, same tools, SDK removed. It issues the $18,500 invoice without
stopping to ask. Worth one run, because it is the only way to see what the rest
of the demo is preventing.

---

## The same scenarios as tests

```bash
python run_demo.py            # against your OpenBox instance
python run_demo.py --stub     # scripted verdicts, no server needed
python run_demo.py --off      # governance disabled
```

Four scenarios, each asserting on a side-effect ledger rather than on whether an
exception surfaced:

| # | Scenario | What passing means |
|---|---|---|
| 1 | A granted tool runs | Authorized, executed, recorded |
| 2 | Ungranted tool is never bound | The model never saw it |
| 3 | A stale tool handle is used | Refused at call time, before any work |
| 4 | A money-moving tool | Held for approval, then approved or refused |

The ledger matters more than it sounds. A denial that raises *after* the tool
already fired would pass a test that only checks for an exception. These check
what ran.

`--stub` needs no OpenBox at all — it answers with scripted verdicts so you can
see `block`, `halt` and `require_approval` handling before you have policies of
your own.

---

## How it is put together

```
serve_demo.py       the browser UI
run_demo.py         the same scenarios, as assertions
setup_openbox.py    creates the agent and its controls via the API
demo_agent/
  registry.py       tool declarations and the grant set
  guard.py          the tool boundary: refuses at bind time and at call time
  loader.py         the one place every tool is built
  tools.py          real HTTP calls, so the telemetry is real
  turn.py           the model loop; only granted tools are offered
upstream.py         a local stand-in for the APIs the tools call
stub_core.py        scripted verdicts, for running without a server
```

The line worth reading is in `loader.py`, where every tool is assembled:

```python
call = impl                      # the tool itself
call = mw.govern(decl, call)     # OpenBox governance — inner
call = guarded(name, ctx)(call)  # the local grant check — OUTER, runs first
```

The local check goes outermost on purpose. It is a compensating control for a
boundary that is a module boundary rather than a network one, so it must never
depend on an external service being reachable. And a call the agent has no grant
for should never be sent for authorization at all — otherwise an approval request
pages a human about an action that could not have run either way.

That ordering was worth getting right: composed the other way round, a tool the
agent had no grant for still went out for authorization first, and a
`require_approval` policy turned a call that was going to be refused locally into
a ten-second wait on a human. Composed correctly it is refused in single-digit
milliseconds, without a round trip.

## Why the tools make real calls

`tools.py` posts to a local stand-in server rather than returning canned dicts.
Telemetry is captured from real outbound calls, so a tool that fakes its work
with a `sleep` produces nothing to inspect — no spans, nothing for a
sequence-based rule to match on, and a session that looks emptier than the agent
really was. If you adapt this demo, keep the calls real.

## What a turn costs

Every node in the execution tree is a blocking authorization round trip, so a
turn takes seconds, not milliseconds — a two-tool turn with a model call in front
of it lands around ten. Nothing is stuck; you are watching each step get
authorized. Worth knowing before you put a governed agent behind a request with a
timeout on it.
