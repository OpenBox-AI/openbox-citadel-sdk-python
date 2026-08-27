# Setting up the demo

The agent needs an identity in OpenBox and three controls attached to it — a
policy, a guardrail, and a behavior rule. One scenario exercises each. Until they
exist the demo runs, but every decision comes back `allow` and you are watching
plumbing rather than governance.

There is a script for it, and there is the click-through. The script is faster;
the click-through shows you where these live, which is worth seeing once before
you demo this to someone else.

---

## 1. An API key

The one thing you create by hand, because it is what authorizes everything else.

**Dashboard → Organization → API keys → create**, with these permissions:

| Permission | Used for |
|---|---|
| `read:agent`, `create:agent` | finding or creating the demo agent |
| `read:agent_policy`, `create:agent_policy` | the approval policy |
| `read:agent_guardrail`, `create:agent_guardrail` | the PII guardrail |
| `read:agent_behavior_rule`, `create:agent_behavior_rule` | the behavior rule |
| `read:agent_session` | linking each run to its session in the dashboard |
| `manage:agent_session` | the Approve / Deny buttons on the demo page |

The last two are optional. Without them the demo still runs — you just get a
workflow id instead of a session link, and you approve in the dashboard instead
of on the page.

Copy the key, then create `.env` next to `run_demo.py`:

```bash
cp .env.example .env
chmod 600 .env
```

and fill it in:

```bash
OPENBOX_PLATFORM_API_URL=https://api.openbox.ai
OPENBOX_PLATFORM_API_KEY=<the key you just created>
OPENBOX_DASHBOARD_URL=https://app.openbox.ai
OPENBOX_API_URL=https://api.openbox.ai

OPENROUTER_API_KEY=<your OpenRouter key>
OPENROUTER_MODEL=openai/gpt-4o-mini
```

Point the URLs at your own deployment if it is not the hosted one.

The OpenRouter key is not optional — the demo makes real model calls. If you want
a different provider, `demo_agent/turn.py` is forty lines and the request is a
plain chat completion.

---

## 2a. With the script

```bash
python setup_openbox.py --dry-run    # every call it will make, nothing changed
python setup_openbox.py              # create them
```

It creates the agent and all three controls, reusing anything already there by
name, so it is safe to run twice. The policy goes in as a builder config — the
same structure the dashboard's policy builder produces — so you can open it in the
UI afterwards and edit it like any other policy.

Creating the agent prints three credentials **once**. Put them in `.env`:

```
OPENBOX_API_KEY=…
OPENBOX_AGENT_DID=did:aip:…
OPENBOX_AGENT_PRIVATE_KEY=…
```

`python setup_openbox.py --show` tells you what exists without changing anything.

Skip to [step 3](#3-run-it).

---

## 2b. By hand in the dashboard

### The agent

**Agents → Create agent.** Name it `citadel demo`, type `temporal`, leave signing
on. Copy the API key, DID and private key into `.env` — the private key is shown
once.

### The policy — approval on money movement

**Your agent → Authorize → Policies → create.** Add two rules.

**"Invoices need a human"**

| Field | Value |
|---|---|
| Decision | `REQUIRE_APPROVAL` |
| Reason | `Invoices are issued by an agent and need a human reviewer` |
| Match | **all** conditions |
| Condition 1 | `activity_type` **equals** `Send_Invoice` |
| Condition 2 | `event_type` **equals** `ActivityStarted` |

Do not skip the second condition. Without it the rule matches the completion
event too, and you end up asking a reviewer to approve work that has already
happened.

**"Account deletion is never automatic"**

| Field | Value |
|---|---|
| Decision | `BLOCK` |
| Reason | `Deleting a customer account is not something an agent does unattended` |
| Match | **all** |
| Condition | `activity_type` **equals** `Delete_Account` |

### The guardrail — PII on the way in

**Authorize → Guardrails → create.**

| Field | Value |
|---|---|
| Type | `PII` |
| Name | `Inbound PII` |
| Processing stage | `Input` |

Input stage means it runs on the way in, before the agent acts on what it was
given.

### The behavior rule — a required sequence

**Authorize → Behavior rules → create.** This is the one that reads a sequence of
calls rather than a single one.

| Field | Value |
|---|---|
| Rule name | `Invoice without a contact lookup` |
| Priority | `70` |
| Trigger | `http_post` |
| Trigger match | `http_url` **contains** `/invoices` |
| Required prior state | `http_post` where `http_url` **contains** `/people/search` |
| Time window | `300` seconds |
| Verdict | `BLOCK` |
| Reject message | `Invoice attempted with no prior contact lookup` |

In plain terms: an invoice may only go out after someone at that company has been
looked up, because the lookup is what establishes there is a real customer being
billed. The prior state is required — if it is missing, the rule is violated.

---

## 3. Run it

```bash
pip install openbox-citadel-sdk-python    # or: pip install -e ../..
pip install httpx starlette uvicorn

python serve_demo.py                      # http://127.0.0.1:8010
```

The [README](README.md) has six scenarios to work through. Start with the first
prompt as it comes.

### Check the controls actually attached

Run one turn, open the session link the page gives you, and find `Send_Invoice`.
It should be sitting on `require_approval`, not `allow`. If it says `allow`, the
policy is attached to a different agent than the one the demo is running as —
check that `OPENBOX_AGENT_DID` in `.env` matches the agent you configured.

---

## When something is off

**`403` from the setup script.** The API key is missing a permission. The error
names the call that failed; match it against the table in step 1.

**Everything comes back `allow` and the invoice goes out unchallenged.** The
policy is not reaching this agent. Check `OPENBOX_AGENT_DID` against the agent
the policy is attached to.

**`OPENROUTER_API_KEY unset — no model call, and so no span`.** The demo will not
fake a turn. Set the key.

**The page shows a workflow id instead of a session link.** The API key is missing
`read:agent_session`, or `OPENBOX_PLATFORM_API_KEY` is unset. The run itself is
unaffected.

**Approve / Deny returns an error.** The API key is missing `manage:agent_session`.
Approve from the dashboard's approvals view instead.

**Runs end with "Approval timed out".** Nobody decided within the window — 20
seconds in `run_demo.py`, 180 in the UI. Both are constructor arguments in the
source.

**`Address already in use` on port 8098.** `serve_demo.py` is already running and
holding the stand-in upstream. Stop it before running `run_demo.py`; each starts
its own.
