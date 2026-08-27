"""OpenBoxCitadelMiddleware — governance for Citadel turns and campaign steps.

Citadel is a LangChain + CrewAI hybrid with a hand-rolled tool loop, so there is
no `create_agent(middleware=[...])` to hand this to. It attaches explicitly: one
call around the turn, one around each bound tool.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openbox_citadel.activity_registry import (
    register_activity as register_activity_ctx,
)
from openbox_citadel.activity_registry import unregister_activity
from openbox_citadel.client import GovernanceClient
from openbox_citadel.config import GovernanceConfig
from openbox_citadel.event_sequence import release_sequencer, sequencer_for
from openbox_citadel.events import build_event
from openbox_citadel.types import error_info, safe_serialize
from openbox_citadel.verdict import (
    GovernanceBlockedError,
    GovernanceError,
    GovernanceHaltError,
    enforce_verdict,
)

logger = logging.getLogger("openbox_citadel")

_INSTRUMENTATION_READY = False
"""Whether this PROCESS has already installed OTel instrumentation."""

_SHARED_RUNTIME: Any = None
"""The one armed `openbox_core` runtime, shared by every middleware built
afterwards. Attribution is per-activity in its `ContextStore`, so a single
runtime serves any number of concurrent runs."""

_INSTALLED_TARGETS: list[str] = []
"""What the first setup actually patched, so a later middleware asking for a
target that is not there can say so instead of silently governing nothing."""

CORE_ENV_PREFIX = "OPENBOX_CITADEL"
"""SDK env namespace. Resolution order is explicit > OPENBOX_CITADEL_* > OPENBOX_*."""

SDK_ENGINE = "citadel"
SDK_LANGUAGE = "python"
SDK_VERSION = "0.1.0"

TASK_QUEUE = "citadel"
"""Framework identifier. The live server accepts any string and new frameworks
are expected to invent their own; Citadel is neither plain LangGraph nor plain
CrewAI, and filtering a dashboard by framework only helps if the value is honest."""


@dataclass
class OpenBoxCitadelMiddlewareOptions:
    """Construction options."""

    api_url: str
    api_key: str
    agent_did: str | None = None
    agent_private_key: str | None = None
    governance_timeout: float = 30.0
    on_api_error: str = "fail_open"
    config: GovernanceConfig = field(default_factory=GovernanceConfig)

    deny_exc: Callable[[str], Exception] | None = None
    """Builds the exception raised on a refusal.

    Citadel passes `ToolAccessDenied`, so no OpenBox type enters an `engine/`
    signature and the integration stays removable by deleting the wrap.
    """

    halt_exc: Callable[[str, Any], Exception] | None = None
    """Builds the exception raised on a `halt` verdict, given `(reason, ctx)`.

    A halt is not a denial. `block` refuses one action and the run carries on;
    `halt` means stop everything. Collapsing both into `deny_exc` is survivable
    on the chat path — the turn ends either way — but wrong on the campaign
    path, where a denial fails the current *step* and the run continues to the
    next one.

    Citadel supplies `FlowCancelled`, which is the type its runner already
    treats as "stop at the boundary, terminal state cancelled, never done".
    `ctx` is the `ToolContext`, so the factory can read `run_id` off it.

    Left unset, a halt falls back to `deny_exc` — the previous behaviour, and
    still correct for a single-turn chat.
    """

    approval_max_wait_seconds: float | None = 75.0
    """Ceiling on a human approval wait; `None` waits as long as Core allows.

    Citadel's SPA aborts a silent stream at 90s, so an approval outliving that
    budget produces a dead tab rather than a pending decision. Campaign steps
    are checkpointed and resumable — raise or disable it there.
    """

    terminal_timeout_seconds: float = 45.0
    """Timeout for the terminal event only.

    Core enforces its own server-side timeout on session close — measured at a
    flat 30s on a session that carries a SignalReceived plus an LLM span, where
    the alignment step waits on a dependency and then gives up. A client timeout
    shorter than Core's means we abandon the request *before* Core answers, and
    the session is left with no terminal event at all: it shows as failed or
    forever in-progress on the dashboard even though the run finished cleanly.
    That is what "failed workflow" with no WorkflowCompleted was.

    This fires after the reply is already delivered, so waiting costs the user
    nothing. It must stay above Core's 30s.
    """

    instrument_http: bool = True
    instrument_databases: bool = True
    instrument_file_io: bool = False
    sqlalchemy_engine: Any = None
    on_verdict: Callable[[str, Any], None] | None = None


class OpenBoxCitadelMiddleware:
    """Governs one Citadel turn or campaign step."""

    def __init__(self, options: OpenBoxCitadelMiddlewareOptions) -> None:
        self._options = options
        self._config = options.config
        self._client = GovernanceClient(
            api_url=options.api_url,
            api_key=options.api_key,
            timeout=options.governance_timeout,
            on_api_error=options.on_api_error,
            fail_hard_on_auth_error=options.config.fail_hard_on_auth_error,
            agent_did=options.agent_did,
            agent_private_key=options.agent_private_key,
        )
        self._otel_ready = False
        self._runtime: Any = None
        self._context_store: Any = None
        self._ctx_tokens: dict[str, Any] = {}
        """ContextVar reset tokens, keyed by activity_id. A token must be reset
        in the context that bound it, so they are held per activity rather than
        as one running stack."""
        self._workflow_id = ""
        self._run_id = ""
        self._workflow_type = "chat"

    # ── layer 2 ─────────────────────────────────────────────────────

    def setup_instrumentation(self) -> None:
        """Install OTel instrumentation. Once per PROCESS, not once per instance.

        The guard used to be `self._otel_ready`, which is per-middleware, so a
        host that builds one middleware per turn re-ran the whole setup every
        turn. The OTel instrumentors tolerate that — they refuse politely and
        log "Attempting to instrument while already instrumented" — but the hook
        layer's `setup_httpx_body_capture` has no such guard and re-wraps
        `httpx.Client.send` / `AsyncClient.send` each time. The wrappers stack.

        Every stacked wrapper reports the same request: the innermost one takes
        the OTel span out of the `_httpx_http_span` ContextVar and sends a proper
        `completed` entry, and each one outside it finds that slot already
        emptied, falls back to `trace.get_current_span()` — which is
        INVALID_SPAN out there — and sends a SECOND completion with an all-zero
        span id under the fallback name `HTTP {method}`. That is the extra
        `llm_completion` completed span, carrying a duplicate of the same prompt
        and response body, unjoinable to its start, and one more of them per
        additional turn.

        So the setup runs once and every later middleware shares the span
        processor that was actually wired into the tracer provider. Sharing is
        safe by construction: the processor keys everything it holds by
        workflow_id, which is exactly how it already keeps concurrent runs
        apart.
        """
        global _INSTRUMENTATION_READY, _SHARED_RUNTIME, _INSTALLED_TARGETS

        if self._otel_ready or not self._options.instrument_http:
            return

        if _INSTRUMENTATION_READY:
            # Adopt the live runtime. Building a second one would install a
            # second set of hooks over the same interpreter and attribute spans
            # into a ContextStore no exporter consults.
            self._runtime = _SHARED_RUNTIME
            self._context_store = _SHARED_RUNTIME.context_store
            self._warn_absent_targets()
            self._otel_ready = True
            return

        try:
            self._runtime = self._build_core_runtime()
            self._context_store = self._runtime.context_store
            # Mirror the flat HTTP wire fields into attributes under the legacy
            # keys core and the dashboard actually read. Without this a span
            # arrives with url.full only: no request link, no status badge, and
            # no identity for core's fingerprint.
            from openbox_citadel.span_aliases import install_span_attribute_aliases

            install_span_attribute_aliases()
            if self._options.instrument_databases:
                # The manager patches the DB-API governance seam but instruments
                # no driver, so without this a SQL statement raises no span at
                # all and `dbapi` still shows as an installed target.
                from openbox_citadel.db_drivers import install_db_driver_instrumentation

                install_db_driver_instrumentation()
            if self._options.instrument_file_io:
                # Correct the file spans the hook layer emits: canonical names,
                # so Core does not file a write as a read, and per-operation
                # timing, so a started/completed pair reads as one sequence.
                from openbox_citadel.file_spans import install_file_span_corrections

                install_file_span_corrections()
            _SHARED_RUNTIME = self._runtime
            _INSTALLED_TARGETS = list(
                getattr(self._runtime._instrumentation_manager, "installed_targets", [])
            )
            _INSTRUMENTATION_READY = True
            self._otel_ready = True
            if not _INSTALLED_TARGETS:
                # Every installer declining is not a success. The usual cause is
                # a base SDK installed without its [http] extra: the OTel
                # instrumentation packages are missing, each installer returns
                # False, and the runtime arms over nothing. Silence here is how
                # a governed-looking run records no spans at all.
                logger.warning(
                    "instrumentation armed but patched NOTHING — no spans will be "
                    "recorded. The base SDK is most likely installed without its "
                    "HTTP extra; install openbox-sdk-python[http]."
                )
            else:
                logger.info("instrumentation installed: %s", ", ".join(_INSTALLED_TARGETS))
            self._warn_absent_targets()
        except Exception:
            # Instrumentation is best-effort. Losing spans degrades behavior
            # rules; failing to start the turn loses the whole run.
            logger.warning("OTel instrumentation unavailable; spans disabled", exc_info=True)

    def _build_core_runtime(self) -> Any:
        """An armed `openbox_core` runtime — the only hook runtime there is.

        This used to call `openbox_langgraph.otel_setup`, which no longer
        installs anything: hook governance moved wholesale into the base SDK's
        `InstrumentationManager`, and the old entry point was left as a shim
        that raises. Nothing here is LangGraph-shaped, so it builds on
        `openbox_core` directly rather than reaching through another
        framework's SDK to get at it.

        The runtime owns a PRIVATE `ContextStore`. The base default is a
        process-global store and `runtime.close()` clears whatever store it
        holds, so sharing one would let a teardown blast a concurrent turn.
        """
        from openbox_core.config import InstrumentationConfig, OpenBoxConfig
        from openbox_core.context import ContextStore
        from openbox_core.instrumentation.manager import InstrumentationManager
        from openbox_core.runtime import OpenBoxRuntime

        core_config = OpenBoxConfig.resolve(
            env_prefix=CORE_ENV_PREFIX,
            api_url=self._options.api_url,
            api_key=self._options.api_key,
            timeout_seconds=self._options.governance_timeout,
            on_api_error=self._options.on_api_error,
            agent_name=self._config.agent_name,
            # Passed through so the runtime's own client signs its requests.
            # Dropping them here would downgrade this SDK to bare-Bearer at the
            # trust boundary while the middleware's client still signs.
            agent_did=self._options.agent_did,
            agent_private_key=self._options.agent_private_key,
            sdk_version=SDK_VERSION,
            sdk_engine=SDK_ENGINE,
            sdk_language=SDK_LANGUAGE,
            instrumentation=InstrumentationConfig(
                http_enabled=self._options.instrument_http,
                db_enabled=self._options.instrument_databases,
                file_enabled=self._options.instrument_file_io,
            ),
            validate=True,
        )

        store = ContextStore()
        runtime = OpenBoxRuntime(core_config, context_store=store)
        # extra_ignored_urls has to reach the manager at construction, so build
        # it directly rather than through runtime.install_instrumentation(),
        # which takes no arguments. runtime.close() still finds the manager here
        # and calls uninstall on it. Same pattern the base SDK's conformance kit
        # uses. Without the ignore, the runtime governs its own evaluate calls.
        manager = InstrumentationManager(
            runtime, extra_ignored_urls={self._options.api_url}
        )
        runtime._instrumentation_manager = manager
        manager.install()
        return runtime

    def _warn_absent_targets(self) -> None:
        """Say so when this middleware wants a target the live runtime lacks.

        Instrumentation is installed once per process, by whichever middleware
        got there first, and its config decided the targets. A later middleware
        asking for more cannot have it retrofitted — installing a second manager
        would re-patch httpx and stack the wrappers this guard exists to
        prevent — so the honest move is to report it rather than let the caller
        believe DB or file calls are being governed.
        """
        wanted = {
            "HTTP": (self._options.instrument_http,
                     ("httpx", "requests", "urllib", "urllib3")),
            # The manager's own names, not the driver packages': one "dbapi"
            # target covers sqlite3/psycopg2/pymysql together.
            "databases": (self._options.instrument_databases, ("sqlalchemy", "dbapi",
                          "asyncpg", "redis", "pymongo")),
            "file io": (self._options.instrument_file_io, ("file",)),
        }
        for label, (asked, targets) in wanted.items():
            if asked and not any(t in _INSTALLED_TARGETS for t in targets):
                logger.warning(
                    "%s instrumentation was requested but the live runtime was "
                    "installed without it; those calls are not governed", label
                )

    # ── layer 1 ─────────────────────────────────────────────────────

    def register_activity(self, activity_id: str, context: dict[str, Any]) -> None:
        """Make this activity the attribution target for layer-2 spans.

        Two registries, deliberately kept in step. The local one is a
        `ContextVar`, which is what survives `await` under concurrent turns; the
        span processor's is what `WorkflowSpanProcessor.on_end` consults when an
        instrumented call finishes. Registering only the first means every
        captured span is dropped for want of an owning activity — which is
        exactly how this SDK shipped a version with no spans at all.
        """
        register_activity_ctx(activity_id, context)
        if self._context_store is not None:
            self._ctx_tokens[activity_id] = self._context_store.bind(
                self._activity_context(activity_id, context)
            )

    def clear_activity(self, activity_id: str) -> None:
        """Stop attributing spans to a finished activity, in both registries."""
        unregister_activity(activity_id)
        token = self._ctx_tokens.pop(activity_id, None)
        if token is not None and self._context_store is not None:
            try:
                self._context_store.reset(token)
            except ValueError:
                # A ContextVar token can only be reset in the context that set
                # it. The hook paths clear in a `finally` beside their register,
                # so this is the odd path out — and leaving the binding is
                # better than raising over telemetry. The next bind shadows it.
                logger.debug("activity context token out of scope", exc_info=True)

    def _activity_context(self, activity_id: str, context: dict[str, Any]) -> Any:
        """Citadel's event dict as the base SDK's `ActivityContext`.

        The hook layer resolves a span's owner from this — the ContextVar tier
        first, which is what survives `await` under concurrent turns. Fields it
        has no home for ride along in `metadata` rather than being dropped.
        """
        from openbox_core.contracts.context import ActivityContext

        first_class = {
            "workflow_id", "run_id", "workflow_type", "task_queue", "activity_id",
            "activity_type", "agent_name", "session_id",
        }
        return ActivityContext(
            workflow_id=context.get("workflow_id") or self._workflow_id,
            run_id=context.get("run_id") or self._run_id,
            workflow_type=context.get("workflow_type") or self._workflow_type,
            task_queue=context.get("task_queue") or self._config.task_queue,
            activity_id=activity_id,
            activity_type=context.get("activity_type"),
            agent_name=context.get("agent_name") or self._config.agent_name,
            session_id=context.get("session_id") or self._config.session_id,
            metadata={k: v for k, v in context.items() if k not in first_class},
        )

    async def before_turn(
        self,
        *,
        workflow_type: str = "chat",
        goal: str | None = None,
        workflow_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """`WorkflowStarted`. `goal` is what drift detection compares against."""
        self.setup_instrumentation()
        # Generated once and reused across every event in the run. Regenerating
        # either mid-run is the commonest way to orphan a workflow.
        self._workflow_id = workflow_id or str(uuid.uuid4())
        self._run_id = run_id or str(uuid.uuid4())
        self._workflow_type = workflow_type

        # No per-workflow registration step here any more. run_id and
        # workflow_type used to live in a WorkflowSpanBuffer keyed by
        # workflow_id; in the core model every span resolves an ActivityContext
        # that already carries all three, so there is nothing to pre-declare.

        # WorkflowStarted carries an activity id of its own, so the workflow has
        # an anchor node on the timeline rather than a bare marker.
        await self._evaluate(
            build_event(
                self,
                "WorkflowStarted",
                f"{self._run_id}-wf",
                workflow_type,
                activity_input=[{"goal": goal}],
            )
        )

    async def signal(
        self,
        signal_name: str,
        signal_args: list[Any] | None = None,
        *,
        activity_type: str | None = None,
    ) -> None:
        """`SignalReceived` — an external trigger, governed like any action.

        Citadel's trigger is the user's message, so this is where a prompt-level
        policy gets its say *before* any model call is made. Enforced, not
        merely recorded: a blocked prompt should never reach the model.
        """
        # Payload shape matters here, and both halves are load-bearing.
        #
        # The activity anchor IS sent: it is what gives the signal a node of its
        # own, and every other OpenBox SDK sends it.
        #
        # `activity_input` is NOT sent. A SignalReceived carries `signal_name`
        # and `signal_args`; adding `activity_input` makes Core run input-stage
        # processing on a workflow-level event, and the session then never
        # closes — WorkflowCompleted hangs until the client times out, which is
        # why sessions were showing as failed or in-progress with no terminal
        # event at all. Verified by diffing against a working openrouter
        # session on the same Core.
        if not self._config.send_signal_events:
            return

        activity_id = f"{self._run_id}-sig"
        response = await self._evaluate(
            build_event(
                self,
                "SignalReceived",
                activity_id,
                activity_type or signal_name,
                signal_name=signal_name,
                signal_args=signal_args or [],
            )
        )
        if response is None:
            return
        try:
            result = enforce_verdict(response, "signal_received")
            if result.requires_hitl:
                from openbox_citadel.hitl import poll_approval_or_halt

                await poll_approval_or_halt(
                    self, activity_id, activity_type or signal_name, result.approval_id
                )
        except BaseException as exc:
            from openbox_citadel.events import send_orphan_closure

            await send_orphan_closure(
                self, "ActivityCompleted", activity_id, activity_type or signal_name, exc
            )
            raise self._denial(exc) from exc

    async def govern_activity(self, activity_type: str, coro: Any) -> Any:
        """Govern one arbitrary awaitable as a single activity.

        The generic escape hatch, for work that is neither a tool call nor a
        model call. `activity_type` is the caller's own vocabulary — the SDK
        neither supplies nor validates it, and whatever string is passed must
        match the guardrail or policy configured against it, byte for byte.

        Citadel's uses are its history read and its memory write, but nothing
        about that is encoded here: an SDK that knows what a "memory op" is has
        a domain concept in a layer that should only understand activities.

            await mw.govern_activity("load_memory", load_history(session_id))

        The activity also anchors any span the call raises — a DB query inside
        it attaches here instead of creating its own orphan node.
        """
        from openbox_citadel.tool_hook import handle_activity

        return await handle_activity(self, activity_type, coro)

    async def after_turn(
        self,
        status: str = "completed",
        error: Exception | None = None,
        *,
        output: Any = None,
    ) -> None:
        """The terminal event. Must fire from a finally block, not the happy path.

        `output` is the turn's final answer. It is sent as `activity_output`
        because that is the field Core actually binds — its payload struct has
        no `workflow_output`, so an SDK sending only that has its final answer
        dropped at unmarshal and every WorkflowCompleted row lands with an empty
        output. `workflow_output` goes too: unknown keys are ignored, and it is
        the conventional name for anything reading the raw event stream.
        """
        try:
            await self._close_dangling()
            serialized = safe_serialize({"result": output}) if output is not None else None
            if error is not None or status != "completed":
                event = build_event(
                    self,
                    "WorkflowFailed",
                    f"{self._run_id}-wf",
                    self._workflow_type,
                    status=status,
                    error=error_info(error) if error is not None else None,
                    activity_output=serialized,
                    workflow_output=serialized,
                )
            else:
                event = build_event(
                    self,
                    "WorkflowCompleted",
                    f"{self._run_id}-wf",
                    self._workflow_type,
                    status="completed",
                    activity_output=serialized,
                    workflow_output=serialized,
                )
            # The terminal event never propagates a transport failure, whatever
            # `on_api_error` says. By the time it fires the turn is over and the
            # reply is already delivered, so failing here would convert a
            # telemetry problem into a user-visible one — and `fail_closed`
            # exists to stop actions before they happen, not to punish a run for
            # a slow write after it finished.
            try:
                # The terminal event gets its own, longer budget — see
                # `terminal_timeout_seconds`.
                await asyncio.wait_for(
                    self._evaluate(event), self._options.terminal_timeout_seconds
                )
            except Exception:
                logger.warning(
                    "terminal %s could not be recorded; session may show as "
                    "in-progress on the dashboard",
                    event.get("event_type"),
                    exc_info=True,
                )
        finally:
            # Any activity that never reached clear_activity would otherwise
            # keep its binding and attribute the next turn's spans to a closed
            # activity.
            for activity_id in list(self._ctx_tokens):
                self.clear_activity(activity_id)
            release_sequencer(self._run_id)

    async def _close_dangling(self) -> None:
        """Complete any activity that was started and never closed.

        Trust scoring never finalizes for a session with an open activity, and
        output-stage guardrails never run. Closing them explicitly is better
        than leaving Core to time them out.
        """
        for activity_id in sequencer_for(self._run_id).dangling_activities():
            logger.warning("closing dangling activity %s", activity_id)
            unregister_activity(activity_id)
            await self._evaluate(
                build_event(
                    self,
                    "ActivityCompleted",
                    activity_id,
                    None,
                    status="failed",
                    error={"type": "DanglingActivity", "message": "closed at workflow end"},
                )
            )

    def govern(self, decl: Any, call: Any) -> Any:
        """Wrap one bound tool. Compose INSIDE Citadel's `guarded()`."""
        import functools

        from openbox_citadel.tool_hook import handle_tool_call

        @functools.wraps(call)
        async def wrapper(args: Any, ctx: Any = None) -> Any:
            return await handle_tool_call(self, decl, call, args, ctx)

        return wrapper

    def govern_stream(self, stream: Any, *, model: str | None = None, prompt: str | None = None) -> Any:
        """Wrap a token stream in an `llm_call` activity."""
        from openbox_citadel.llm_hook import govern_stream as _govern_stream

        return _govern_stream(self, stream, model=model, prompt=prompt)

    async def govern_call(self, coro: Any, *, model: str | None = None, prompt: str | None = None) -> Any:
        """Wrap a non-streaming model call in an `llm_call` activity."""
        from openbox_citadel.llm_hook import govern_call as _govern_call

        return await _govern_call(self, coro, model=model, prompt=prompt)

    async def aclose(self) -> None:
        await self._client.close()

    # ── plumbing ────────────────────────────────────────────────────

    async def _evaluate(self, event: dict[str, Any]) -> Any:
        response = await self._client.evaluate(event)
        if response is not None and self._options.on_verdict is not None:
            try:
                self._options.on_verdict(
                    event.get("activity_type") or event.get("event_type", ""), response
                )
            except Exception:  # a tracer must never fail the action it observes
                logger.warning("on_verdict hook raised; ignoring", exc_info=True)
        return response

    def _denial(self, cause: BaseException, ctx: Any = None) -> BaseException:
        """Translate a governance refusal into the host's own type.

        Only governance refusals are translated. A tool's own `TimeoutError`
        must reach Citadel unchanged, or a transport failure would read to the
        engine as an access-control decision.

        A **halt** is translated separately when `halt_exc` is supplied: it is a
        different instruction from a denial, and on the campaign path a denial
        only fails the current step.
        """
        if not isinstance(cause, GovernanceError):
            return cause
        if isinstance(cause, GovernanceHaltError) and self._options.halt_exc is not None:
            return self._options.halt_exc(str(cause), ctx)
        if self._options.deny_exc is not None:
            return self._options.deny_exc(str(cause))
        if isinstance(cause, Exception):
            return cause
        return GovernanceBlockedError("block", str(cause))


__all__ = ["TASK_QUEUE", "OpenBoxCitadelMiddleware", "OpenBoxCitadelMiddlewareOptions"]
