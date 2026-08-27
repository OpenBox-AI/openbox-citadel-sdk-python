"""Tool governance: ActivityStarted -> execute -> ActivityCompleted."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from openbox_citadel.activity_registry import (
    activity_abort_reason,
    clear_activity_abort,
    has_activity_abort,
    is_activity_approved,
    mark_activity_approved,
    run_with_activity,
)
from openbox_citadel.events import base_event_fields, build_event, send_orphan_closure
from openbox_citadel.hitl import poll_approval_or_halt
from openbox_citadel.types import error_info, hex_id, safe_serialize
from openbox_citadel.verdict import (
    GovernanceBlockedError,
    GovernanceHaltError,
    enforce_verdict,
    unwrap_governance_error,
)

if TYPE_CHECKING:
    from openbox_citadel.middleware import OpenBoxCitadelMiddleware

logger = logging.getLogger("openbox_citadel.tool")

START_EVENT = "ActivityStarted"
COMPLETED_EVENT = "ActivityCompleted"


async def handle_tool_call(
    mw: OpenBoxCitadelMiddleware,
    decl: Any,
    call: Any,
    args: Any,
    ctx: Any,
) -> Any:
    """Govern one tool invocation. `decl` is duck-typed against Citadel's ToolDecl."""
    tool_name = getattr(decl, "name", str(decl))
    if tool_name in mw._config.skip_tool_types:
        return await call(args, ctx)

    activity_id = hex_id(32)
    tool_type = mw._config.tool_type_map.get(tool_name)
    started_ms = time.monotonic() * 1000
    activity_input = [_describe(args, ctx)]

    # ── ActivityStarted ─────────────────────────────────────────────
    if mw._config.send_tool_start_event:
        response = await mw._evaluate(
            build_event(
                mw,
                START_EVENT,
                activity_id,
                tool_name,
                activity_input=activity_input,
                tool_name=tool_name,
                tool_type=tool_type,
            )
        )
        if response is not None:
            try:
                result = enforce_verdict(response, "tool_start")
                if result.requires_hitl:
                    await poll_approval_or_halt(mw, activity_id, tool_name, result.approval_id)
                    mark_activity_approved(activity_id)
                    clear_activity_abort(activity_id)
            except BaseException as exc:
                # A hard block or halt at start would otherwise leave a true
                # orphan on Core: started, never completed.
                if mw._config.send_tool_end_event:
                    await send_orphan_closure(mw, COMPLETED_EVENT, activity_id, tool_name, exc)
                raise mw._denial(exc, ctx) from exc
            args = _apply_redaction(response, activity_input, args)

    # Registered AFTER the evaluate, deliberately. Registering first would make
    # this activity the attribution target while the evaluate's own HTTP request
    # is in flight, so the instrumentation would capture that request as a hook
    # span for the tool — sending a second governance event and creating a
    # duplicate approval request for work nobody asked about.
    mw.register_activity(
        activity_id,
        {
            **base_event_fields(mw),
            "event_type": START_EVENT,
            "activity_id": activity_id,
            "activity_type": tool_name,
        },
    )

    # ── execute ─────────────────────────────────────────────────────
    was_approved = False
    try:
        while True:
            try:
                with run_with_activity(activity_id):
                    output = await call(args, ctx)
                # Some tools catch transport errors internally and return them
                # as values rather than raising. The hook still set the abort
                # flag, so check it — otherwise a mid-call refusal is silently
                # swallowed by the tool's own error handling.
                if has_activity_abort(activity_id):
                    await poll_approval_or_halt(mw, activity_id, tool_name)
                    mark_activity_approved(activity_id)
                    clear_activity_abort(activity_id)
                    continue
                break
            except BaseException as exc:  # noqa: BLE001 — re-raised or retried
                hook_error = unwrap_governance_error(exc)
                if (
                    isinstance(hook_error, GovernanceBlockedError)
                    and hook_error.verdict == "require_approval"
                ):
                    await poll_approval_or_halt(mw, activity_id, tool_name)
                    mark_activity_approved(activity_id)
                    clear_activity_abort(activity_id)
                    continue

                # Prefer the reason recorded at the moment of the abort: a
                # governance error raised inside an instrumented call surfaces
                # through whatever client made it, and most wrap it in a generic
                # transport error that buries the real cause.
                reason = activity_abort_reason(activity_id)
                failure: BaseException = (
                    GovernanceHaltError(reason) if reason is not None else (hook_error or exc)
                )
                if mw._config.send_tool_end_event and not is_activity_approved(activity_id):
                    await mw._evaluate(
                        build_event(
                            mw,
                            COMPLETED_EVENT,
                            activity_id,
                            tool_name,
                            activity_output=safe_serialize({"error": error_info(failure)}),
                            tool_name=tool_name,
                            tool_type=tool_type,
                            status="failed",
                            duration_ms=time.monotonic() * 1000 - started_ms,
                            error=error_info(failure),
                        )
                    )
                raise mw._denial(failure, ctx) if _is_governance(failure) else failure
        # Captured before unregister clears it.
        was_approved = is_activity_approved(activity_id)
    finally:
        mw.clear_activity(activity_id)

    # ── ActivityCompleted ───────────────────────────────────────────
    if mw._config.send_tool_end_event:
        serialized = (
            safe_serialize({"result": output})
            if isinstance(output, str)
            else safe_serialize(output)
        )
        # Same activity_id as the start: Core matches completions to starts by
        # activity_id and upserts one row. A different id produces an orphan.
        response = await mw._evaluate(
            build_event(
                mw,
                COMPLETED_EVENT,
                activity_id,
                tool_name,
                activity_output=serialized,
                tool_name=tool_name,
                tool_type=tool_type,
                status="completed",
                duration_ms=time.monotonic() * 1000 - started_ms,
            )
        )
        # When the start already required and received approval, send the event
        # but skip enforcement: a second evaluation creates a spurious approval
        # row for work a human has already signed off.
        if response is not None and not was_approved:
            try:
                result = enforce_verdict(response, "tool_end")
                if result.requires_hitl:
                    await poll_approval_or_halt(mw, activity_id, tool_name, result.approval_id)
            except BaseException as exc:
                raise mw._denial(exc) from exc

    return output


def _is_governance(exc: BaseException) -> bool:
    from openbox_citadel.verdict import GovernanceError

    return isinstance(exc, GovernanceError)


def _describe(args: Any, ctx: Any) -> dict[str, Any]:
    """The authorize payload: the arguments plus who is calling.

    A policy that cannot see the tenant and agent can only ever be a per-tool
    rule. `ToolContext` is frozen and slotted, so read it defensively.
    """
    payload: dict[str, Any] = {"args": safe_serialize(args)}
    if ctx is None:
        return payload
    for field in (
        "agent_id", "source", "user_id", "tenant", "session_id", "location_id", "campaign_id",
    ):
        value = getattr(ctx, field, None)
        if value is not None:
            payload[field] = value
    return payload


def _apply_redaction(response: Any, activity_input: list[Any], original: Any) -> Any:
    """Swap in Core's redacted arguments when guardrails rewrote them.

    Discarding this sends the unredacted value to the tool while the guardrail
    reports success — silent, and the whole point of the feature.
    """
    guardrails = (
        response.get("guardrails_result")
        if isinstance(response, dict)
        else getattr(response, "guardrails_result", None)
    )
    if not guardrails:
        return original
    get = guardrails.get if isinstance(guardrails, dict) else lambda k: getattr(guardrails, k, None)
    if get("input_type") != "activity_input":
        return original
    redacted = get("redacted_input")
    if redacted is None:
        return original
    first = redacted[0] if isinstance(redacted, list) and redacted else redacted
    if isinstance(first, dict) and "args" in first:
        return first["args"]
    return original


__all__ = ["handle_tool_call"]


async def handle_activity(mw: OpenBoxCitadelMiddleware, activity_type: str, coro: Any) -> Any:
    """Govern a bare awaitable as one activity.

    For work that is neither a tool call nor a model call. The caller names the
    activity type; the SDK has no opinion about what kinds of work exist.

    Same shape as `handle_tool_call`: authorize, anchor, run, complete. The
    anchor matters as much as the verdict — a DB or file span raised inside the
    call attaches to this activity instead of creating its own orphan node.
    """
    activity_id = hex_id(32)
    started_ms = time.monotonic() * 1000

    response = await mw._evaluate(
        build_event(mw, START_EVENT, activity_id, activity_type, activity_input=[{}])
    )
    if response is not None:
        try:
            result = enforce_verdict(response, "tool_start")
            if result.requires_hitl:
                await poll_approval_or_halt(mw, activity_id, activity_type, result.approval_id)
                mark_activity_approved(activity_id)
        except BaseException as exc:
            await send_orphan_closure(mw, COMPLETED_EVENT, activity_id, activity_type, exc)
            raise mw._denial(exc) from exc

    mw.register_activity(
        activity_id,
        {
            **base_event_fields(mw),
            "event_type": START_EVENT,
            "activity_id": activity_id,
            "activity_type": activity_type,
        },
    )

    try:
        with run_with_activity(activity_id):
            output = await coro
        was_approved = is_activity_approved(activity_id)
    except BaseException as exc:
        failure = unwrap_governance_error(exc) or exc
        mw.clear_activity(activity_id)
        await mw._evaluate(
            build_event(
                mw,
                COMPLETED_EVENT,
                activity_id,
                activity_type,
                status="failed",
                duration_ms=time.monotonic() * 1000 - started_ms,
                error=error_info(failure),
            )
        )
        raise (mw._denial(failure) if _is_governance(failure) else failure) from exc
    else:
        mw.clear_activity(activity_id)

    if not was_approved:
        await mw._evaluate(
            build_event(
                mw,
                COMPLETED_EVENT,
                activity_id,
                activity_type,
                status="completed",
                duration_ms=time.monotonic() * 1000 - started_ms,
                activity_output=safe_serialize(output) if output is not None else None,
            )
        )
    return output
