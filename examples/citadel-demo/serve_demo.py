"""A browser front end for the Citadel-shaped demo agent.

    python serve_demo.py          # then open http://127.0.0.1:8010

Same agent, same loader, same D5 guard as `run_demo.py` — this only replaces the
terminal with a page. One turn at a time: type a prompt, pick a mode, and watch
the governed turn arrive event by event, including the approval a money-moving
tool has to wait for.

What it streams is what the SDK reported, not a reconstruction. Every verdict
line is an `on_verdict` callback, every tool line is `run_turn`'s `on_tool`, and
the side-effect ledger is `demo_agent.tools.SIDE_EFFECTS` — the same assertion
surface the scenarios use, so a denial that performed work is visible here too.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from demo_agent.guard import ToolAccessDenied, ToolContext
from demo_agent.loader import bind
from demo_agent.registry import SALES_AGENT, grants_for
from demo_agent.tools import SIDE_EFFECTS
from demo_agent.turn import Turn, run_turn
from openbox_citadel import GovernanceConfig, create_openbox_citadel_middleware, verdict_from_string
from openbox_citadel.config import HITLConfig

PORT = 8010
WORKFLOW_TYPE = "citadel-demo"

DASHBOARD = ""
"""Where the 'review this session' link points. From OPENBOX_DASHBOARD_URL."""

PLATFORM_API = ""
PLATFORM_KEY = ""
"""Read-only use: turning a workflow_id into the session link. Without it the run
still works — the page just shows the workflow_id instead of a link.

All three are resolved in `main()`, AFTER .env is loaded. Reading them at import
time made the file's defaults win over the .env every time."""

USER_AGENT = "openbox-citadel-demo/0.1 (+https://openbox.ai)"
"""urllib's default (`Python-urllib/x.y`) is refused at the edge with a 403
and `error code: 1010`, before the request reaches the API."""

RUN_LOCK = asyncio.Lock()
"""One turn at a time. `SIDE_EFFECTS` is module-global and the approval controls
address a single pending activity, so concurrent turns would report each other's
work as their own."""


def load_env() -> None:
    env = pathlib.Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key, value)


def platform(path: str, method: str = "GET", body: dict | None = None) -> Any:
    """One call against the OpenBox API. `None` when it is not configured."""
    if not PLATFORM_KEY:
        return None
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{PLATFORM_API}{path}", data=data, method=method,
        headers={"x-api-key": PLATFORM_KEY, "Content-Type": "application/json",
                 "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except Exception as exc:  # noqa: BLE001 — never fail the turn over this
        logger_warn(f"{method} {path} failed: {exc}")
        return None


def logger_warn(message: str) -> None:
    print(f"  [warn] {message}", flush=True)


def rows_of(payload: Any) -> list[dict]:
    """The rows out of any envelope the API wraps them in.

    Paginated endpoints answer `{status, data: {data: [...], total}}` — two
    `data` layers, not one. Peeling a single layer yields the inner *dict*,
    whose iteration gives key strings and no rows at all, so the caller sees
    an empty list and reports the thing it asked for as missing.
    """
    seen = 0
    while isinstance(payload, dict) and seen < 4:
        payload = payload.get("data") or payload.get("items") or []
        seen += 1
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def resolve_agent_id() -> str:
    """The agent's id, which the dashboard link needs.

    Looked up by the DID the demo runs as, so nothing is pinned to one
    installation. Empty means "show the workflow_id instead of a link".
    """
    did = os.environ.get("OPENBOX_AGENT_DID", "")
    if not did:
        return ""
    for row in rows_of(platform("/agent/list")):
        if row.get("did") == did:
            return str(row.get("id") or "")
    return ""


def session_id_for(agent_id: str, workflow_id: str) -> str:
    """Turn a workflow_id into the session id the dashboard link uses."""
    if not agent_id:
        return ""
    found = platform(f"/agent/{agent_id}/sessions?search={workflow_id}&limit=5")
    for row in rows_of(found):
        if row.get("workflow_id") == workflow_id:
            return str(row.get("id") or "")
    return ""


# ── the turn, as a stream of events ─────────────────────────────────────

async def run_stream(prompt: str, mode: str, gate_invoice: bool):
    """Drive one governed turn, yielding SSE frames as things happen."""
    queue: asyncio.Queue = asyncio.Queue()

    def emit(kind: str, **fields: Any) -> None:
        queue.put_nowait({"type": kind, **fields})

    async def drive() -> None:
        governed = mode != "off"
        mw = None
        try:
            if gate_invoice and mode == "live":
                # The approval branch only exists if a policy asks for it.
                from verify import policy
                policy.set_arm("Send_Invoice", "REQUIRE_APPROVAL",
                               reason="Invoices require a human reviewer.")
                await asyncio.sleep(0.4)
                emit("info", text="OPA policy: Send_Invoice → require_approval")

            api_url = None
            if mode == "stub":
                import stub_core
                stub_core.serve()
                api_url = f"http://127.0.0.1:{stub_core.PORT}"
                emit("info", text="scripted verdicts — NOT the live policy engine")

            if governed:
                def on_verdict(activity_type: str, response: dict) -> None:
                    arm = verdict_from_string(response.get("arm") or response.get("verdict"))
                    reason = response.get("reason")
                    emit("verdict", activity=activity_type, arm=arm,
                         risk=response.get("risk_score"),
                         reason=reason if isinstance(reason, str) else "",
                         policy=str(response.get("policy_id") or "")[:18])
                    if arm == "require_approval":
                        emit("pending", activity=activity_type,
                             workflow_id=getattr(mw, "_workflow_id", ""))

                options = dict(
                    deny_exc=ToolAccessDenied, on_verdict=on_verdict, validate=False,
                    agent_name="Squidgy Sales Assistant", session_id="frontend",
                    instrument_http=True, instrument_databases=False,
                    instrument_file_io=False, governance_timeout=15.0,
                    # The prompt belongs in the tree as its own SignalReceived
                    # node, but emitting it makes Core run goal alignment, which
                    # needs Guardrails (:9000). With Guardrails down that costs
                    # ~31s at after_turn and loses the terminal event. See the
                    # note in run_demo.py.
                    config=GovernanceConfig(send_signal_events=False,
                                            hitl=HITLConfig(poll_interval_ms=1500)),
                    approval_max_wait_seconds=180.0,
                )
                if api_url:
                    options["api_url"] = api_url
                mw = create_openbox_citadel_middleware(**options)
                await mw.before_turn(workflow_type=WORKFLOW_TYPE, goal=prompt)
                emit("workflow", workflow_id=mw._workflow_id)
                await mw.signal("user_prompt", [prompt])
                await mw.govern_activity("load_memory", asyncio.sleep(0.01))

            ctx = ToolContext(
                agent_id=SALES_AGENT.agent_id, source=SALES_AGENT.source,
                granted=grants_for(SALES_AGENT), user_id="user-42",
                tenant="squidgy", session_id="frontend",
            )
            tools = bind(SALES_AGENT, ctx, mw=mw)
            emit("bound", tools=[t.name for t in tools],
                 declared=[d.name for d in SALES_AGENT.tools])

            before = len(SIDE_EFFECTS)
            started = time.monotonic()
            outcome, reply = "completed", None
            try:
                reply = await run_turn(
                    Turn(prompt), tools, ctx, gov=mw,
                    on_text=lambda chunk: emit("text", text=chunk),
                    on_tool=lambda name, status, result: emit(
                        "tool", name=name, status=status, result=str(result)[:200]),
                )
            except ToolAccessDenied as exc:
                outcome = "ToolAccessDenied"
                emit("denied", text=str(exc))
            except Exception as exc:  # noqa: BLE001 — reported, never swallowed
                outcome = type(exc).__name__
                emit("denied", text=f"{outcome}: {exc}")

            if mw is not None:
                if outcome == "completed":
                    await mw.govern_activity("save_context", asyncio.sleep(0.01))
                await mw.after_turn(
                    status="completed" if outcome == "completed" else "failed",
                    output=reply,
                )

            fired = [e["tool"] for e in SIDE_EFFECTS[before:]]
            link, session_id = "", ""
            if mw is not None:
                session_id = session_id_for(AGENT_ID, mw._workflow_id)
                if session_id and AGENT_ID:
                    link = (f"{DASHBOARD}/agents/{AGENT_ID}"
                            f"?tab=verify&sessionId={session_id}")
            emit("done", outcome=outcome, effects=fired,
                 elapsed_ms=round((time.monotonic() - started) * 1000),
                 session_id=session_id, link=link)
        except Exception as exc:  # noqa: BLE001 — surface setup failures too
            emit("error", text=f"{type(exc).__name__}: {exc}")
        finally:
            if mw is not None:
                await mw.aclose()
            if gate_invoice and mode == "live":
                try:
                    from verify import policy
                    policy.clear()
                except Exception:  # noqa: BLE001
                    pass
            queue.put_nowait(None)

    task = asyncio.create_task(drive())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"
    finally:
        if not task.done():
            task.cancel()


async def stream(request: Request) -> StreamingResponse:
    prompt = request.query_params.get("prompt", "").strip()
    mode = request.query_params.get("mode", "live")
    gate = request.query_params.get("gate") == "1"
    if not prompt:
        return JSONResponse({"error": "prompt required"}, status_code=400)
    if RUN_LOCK.locked():
        return JSONResponse({"error": "a turn is already running"}, status_code=409)

    async def guarded():
        async with RUN_LOCK:
            async for frame in run_stream(prompt, mode, gate):
                yield frame

    return StreamingResponse(guarded(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


async def decide(request: Request) -> JSONResponse:
    """Approve or reject the pending approval, the same way a reviewer would.

    The dashboard's approvals view does exactly this call. Doing it from here
    just saves you a tab while you are trying the scenarios out.
    """
    body = await request.json()
    workflow_id = body.get("workflow_id", "")
    action = "approve" if body.get("decision") == "approve" else "reject"
    if not AGENT_ID:
        return JSONResponse(
            {"error": "no agent id — set OPENBOX_PLATFORM_API_KEY, or decide it "
                      "in the dashboard instead"}, status_code=409)

    pending = rows_of(platform(f"/agent/{AGENT_ID}/approvals/pending?limit=25"))
    match = next((row for row in pending if row.get("workflow_id") == workflow_id),
                 pending[0] if pending else None)
    if not match:
        return JSONResponse({"error": "nothing pending to decide"}, status_code=404)

    event_id = match.get("id") or match.get("event_id") or match.get("governance_event_id")
    result = platform(f"/agent/{AGENT_ID}/approvals/{event_id}/decide",
                      method="PUT", body={"action": action})
    if result is None:
        return JSONResponse(
            {"error": "the decision call failed — the API key may be missing "
                      "manage:agent_session"}, status_code=502)
    return JSONResponse({"ok": True, "decision": action})


async def index(request: Request) -> HTMLResponse:
    return HTMLResponse(PAGE)


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Citadel demo — governed by OpenBox</title>
<style>
 :root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;
       --ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}
 header{padding:18px 24px;border-bottom:1px solid var(--line)}
 h1{margin:0;font-size:15px;font-weight:600;letter-spacing:.01em}
 h1 span{color:var(--dim);font-weight:400}
 main{max-width:1000px;margin:0 auto;padding:22px 24px 60px}
 form{display:grid;gap:12px;background:var(--panel);border:1px solid var(--line);
      border-radius:10px;padding:16px}
 .row{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 label{color:var(--dim)}
 input[type=text]{flex:1;min-width:280px;background:#0d1117;color:var(--fg);
   border:1px solid var(--line);border-radius:7px;padding:9px 11px;font:inherit}
 button{background:var(--acc);color:#04121f;border:0;border-radius:7px;
   padding:9px 18px;font:inherit;font-weight:600;cursor:pointer}
 button:disabled{opacity:.45;cursor:default}
 button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line);font-weight:500}
 .chips{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 0}
 .chip{font:12px var(--mono);border:1px solid var(--line);border-radius:999px;padding:3px 10px;color:var(--dim)}
 .chip.on{color:var(--ok);border-color:#1f6f36}
 .chip.off{color:var(--dim);text-decoration:line-through}
 #log{margin-top:16px;display:flex;flex-direction:column;gap:1px}
 .ev{display:grid;grid-template-columns:132px 1fr;gap:12px;padding:7px 10px;
     border-left:2px solid transparent;font:12.5px var(--mono)}
 .ev .k{color:var(--dim)}
 .ev.verdict{border-left-color:#21262d}
 .ev.enforce{border-left-color:var(--warn);background:#1d1a0e}
 .ev.tool{border-left-color:var(--ok);background:#0f1a12}
 .ev.text{border-left-color:var(--acc);background:#0d1620;font-family:inherit;font-size:13.5px}
 .ev.bad{border-left-color:var(--bad);background:#1d1113;color:#ffb4ae}
 .ev.done{border-left-color:var(--acc);background:#101720}
 .pending{margin-top:14px;background:#1d1a0e;border:1px solid #6b5310;border-radius:9px;
   padding:13px 15px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
 .pending b{font:12.5px var(--mono);color:var(--warn)}
 a{color:var(--acc)}
 .muted{color:var(--dim);font-size:12.5px}
 .eff{font:12.5px var(--mono)}
</style></head><body>
<header><h1>Citadel-shaped demo agent <span>— governed by OpenBox</span></h1></header>
<main>
 <form id="f">
  <div class="row">
   <label>mode</label>
   <label><input type="radio" name="mode" value="live" checked> live</label>
   <label><input type="radio" name="mode" value="stub"> stub core</label>
   <label><input type="radio" name="mode" value="off"> governance off</label>
   <label style="margin-left:auto"><input type="checkbox" id="gate" checked>
     gate Send_Invoice with require_approval</label>
  </div>
  <div class="row">
   <input type="text" id="p" value="Find the operations contact at acme-logistics.com, then issue an invoice to customer cus_77 for $18,500.">
   <button id="go">Run turn</button>
  </div>
  <div class="muted">The grant set is fixed: Web_Analysis, Apollo and Send_Invoice are
   granted; Delete_Account is declared but not granted, so ask for a deletion and
   watch it never reach the model.</div>
 </form>
 <div class="chips" id="chips"></div>
 <div id="pending"></div>
 <div id="log"></div>
</main>
<script>
const log=document.getElementById('log'), chips=document.getElementById('chips'),
      pend=document.getElementById('pending'), go=document.getElementById('go');
let es=null, wf='';
function add(cls,k,v){const d=document.createElement('div');d.className='ev '+cls;
  d.innerHTML='<div class="k"></div><div class="v"></div>';
  d.children[0].textContent=k; d.children[1].textContent=v; log.appendChild(d);
  window.scrollTo(0,document.body.scrollHeight);}
document.getElementById('f').addEventListener('submit',e=>{
  e.preventDefault(); if(es) es.close();
  log.innerHTML=''; chips.innerHTML=''; pend.innerHTML=''; wf='';
  go.disabled=true; go.textContent='Running…';
  const mode=document.querySelector('input[name=mode]:checked').value;
  const gate=document.getElementById('gate').checked?'1':'0';
  const q=new URLSearchParams({prompt:document.getElementById('p').value,mode,gate});
  es=new EventSource('/stream?'+q);
  es.onmessage=m=>{
    const e=JSON.parse(m.data);
    if(e.type==='info') add('','note',e.text);
    else if(e.type==='workflow'){wf=e.workflow_id; add('','workflow',e.workflow_id);}
    else if(e.type==='bound'){
      e.declared.forEach(t=>{const on=e.tools.includes(t);
        const s=document.createElement('span'); s.className='chip '+(on?'on':'off');
        s.textContent=t+(on?'':' — not granted'); chips.appendChild(s);});
      add('','bound',e.tools.join(', '));
    }
    else if(e.type==='verdict') add(e.arm==='allow'||e.arm==='monitor'?'verdict':'enforce',
      e.activity, 'arm='+e.arm+(e.risk!=null?'  risk='+e.risk:'')+(e.reason?'  '+e.reason:''));
    else if(e.type==='pending'){
      pend.innerHTML='<b>'+e.activity+' is waiting on a human.</b>'+
        '<button class="ghost" data-d="approve">Approve</button>'+
        '<button class="ghost" data-d="deny">Deny</button>'+
        '<span class="muted">same call the dashboard\'s approvals view makes</span>';
      pend.querySelectorAll('button').forEach(b=>b.onclick=async()=>{
        pend.querySelectorAll('button').forEach(x=>x.disabled=true);
        await fetch('/decide',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({workflow_id:e.workflow_id||wf,activity:e.activity,decision:b.dataset.d})});
        pend.innerHTML='<span class="muted">'+b.dataset.d+'d — the poll picks it up on its next tick</span>';
      });
    }
    else if(e.type==='tool') add('tool',e.name,(e.status==='ok'?'→ ':'! ')+e.result);
    else if(e.type==='text') add('text','model',e.text);
    else if(e.type==='denied') add('bad','refused',e.text);
    else if(e.type==='error') add('bad','error',e.text);
    else if(e.type==='done'){
      pend.innerHTML='';
      add('done','side effects',(e.effects.length?e.effects.join(', '):'NONE')+
        '   ('+e.elapsed_ms+' ms, outcome '+e.outcome+')');
      if(e.link){const d=document.createElement('div');d.className='ev done';
        d.innerHTML='<div class="k">session</div><div><a target="_blank" href="'+e.link+'">'+e.session_id+' — review on the dashboard →</a></div>';
        log.appendChild(d);}
      es.close(); go.disabled=false; go.textContent='Run turn';
    }
  };
  es.onerror=()=>{add('bad','stream','connection closed'); es.close();
    go.disabled=false; go.textContent='Run turn';};
});
</script></body></html>"""

app = Starlette(routes=[
    Route("/", index),
    Route("/stream", stream),
    Route("/decide", decide, methods=["POST"]),
])


AGENT_ID = ""
"""Resolved once at startup from OPENBOX_AGENT_DID."""


def main() -> None:
    global AGENT_ID, DASHBOARD, PLATFORM_API, PLATFORM_KEY
    load_env()
    DASHBOARD = os.environ.get(
        "OPENBOX_DASHBOARD_URL", "https://app.openbox.ai").rstrip("/")
    PLATFORM_API = os.environ.get(
        "OPENBOX_PLATFORM_API_URL", "https://api.openbox.ai").rstrip("/")
    PLATFORM_KEY = os.environ.get("OPENBOX_PLATFORM_API_KEY", "")
    import upstream
    upstream.serve()          # the tools need something genuine to call

    AGENT_ID = resolve_agent_id()
    if not AGENT_ID:
        reason = ("OPENBOX_PLATFORM_API_KEY is not set (see SETUP.md)"
                  if not PLATFORM_KEY else
                  "the key is set, so check the warning above for the real cause "
                  "— a 403 with 'error code: 1010' is an edge block on the client "
                  "signature, not a permission problem")
        print("  [warn] could not resolve the agent's id — runs will show a "
              "workflow_id instead of a dashboard link.\n"
              f"         {reason}.")
    import uvicorn
    print(f"Citadel demo front end → http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
