"""Model-call governance: LLMStarted -> call the model -> LLMCompleted.

`activity_type` is the literal string `llm_call` in every OpenBox SDK, so a
policy or behavior rule written against one agent's model calls matches every
other agent's too. Do not substitute the model name here — that belongs in
`llm_model`.

Citadel streams, which constrains what this can do. Authorization completes
before the first chunk is pulled, so a refusal costs no tokens. It cannot
retract text already streamed, which is why prompt-level policy belongs at the
input stage and not the output stage.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
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
from openbox_citadel.metadata import response_metadata
from openbox_citadel.types import hex_id, safe_serialize
from openbox_citadel.verdict import (
    GovernanceError,
    GovernanceHaltError,
    enforce_verdict,
    unwrap_governance_error,
)

if TYPE_CHECKING:
    from openbox_citadel.middleware import OpenBoxCitadelMiddleware

logger = logging.getLogger("openbox_citadel.llm")

ACTIVITY_TYPE = "llm_call"
START_EVENT = "LLMStarted"
COMPLETED_EVENT = "LLMCompleted"


async def _authorize(
    mw: OpenBoxCitadelMiddleware, activity_id: str, prompt: str | None, model: str | None
) -> Any:
    """LLMStarted plus enforcement. Closes its own row on a hard refusal."""
    if not mw._config.send_llm_start_event:
        return None

    response = await mw._evaluate(
        build_event(
            mw,
            START_EVENT,
            activity_id,
            ACTIVITY_TYPE,
            activity_input=[{"prompt": prompt}],
            prompt=prompt,
            llm_model=model,
        )
    )
    if response is None:
        return None

    try:
        result = enforce_verdict(response, "llm_start")
        if result.requires_hitl:
            await poll_approval_or_halt(mw, activity_id, ACTIVITY_TYPE, result.approval_id)
            mark_activity_approved(activity_id)
            clear_activity_abort(activity_id)
    except BaseException as exc:
        # Otherwise this is a started-never-completed llm_call row on Core forever.
        if mw._config.send_llm_end_event:
            await send_orphan_closure(mw, COMPLETED_EVENT, activity_id, ACTIVITY_TYPE, exc)
        raise mw._denial(exc) from exc
    return response


def _register(mw: OpenBoxCitadelMiddleware, activity_id: str) -> None:
    mw.register_activity(
        activity_id,
        {
            **base_event_fields(mw),
            "event_type": "ActivityStarted",
            "activity_id": activity_id,
            "activity_type": ACTIVITY_TYPE,
        },
    )


async def _complete(
    mw: OpenBoxCitadelMiddleware,
    activity_id: str,
    *,
    status: str,
    duration_ms: float,
    model: str | None = None,
    was_approved: bool = False,
    **meta: Any,
) -> None:
    """LLMCompleted. Skipped entirely when the start already got human approval,
    which would otherwise create a spurious approval row for the same activity."""
    if not mw._config.send_llm_end_event or was_approved:
        return
    response = await mw._evaluate(
        build_event(
            mw,
            COMPLETED_EVENT,
            activity_id,
            ACTIVITY_TYPE,
            status=status,
            duration_ms=duration_ms,
            llm_model=model,
            **meta,
        )
    )
    if response is not None and status == "completed":
        try:
            result = enforce_verdict(response, "llm_end")
            if result.requires_hitl:
                await poll_approval_or_halt(
                    mw, activity_id, ACTIVITY_TYPE, result.approval_id
                )
        except BaseException as exc:
            raise mw._denial(exc) from exc


async def govern_stream(
    mw: OpenBoxCitadelMiddleware,
    stream: AsyncIterator[Any],
    *,
    model: str | None = None,
    prompt: str | None = None,
) -> AsyncIterator[Any]:
    """Wrap a token stream. Chunks pass through untouched.

    Authorization completes before the first chunk is pulled, so a refusal costs
    no tokens.
    """
    activity_id = hex_id(32)
    started_ms = time.monotonic() * 1000
    await _authorize(mw, activity_id, prompt, model)
    _register(mw, activity_id)

    chunks = 0
    try:
        with run_with_activity(activity_id):
            async for chunk in stream:
                chunks += 1
                yield chunk
        was_approved = is_activity_approved(activity_id)
    except BaseException as exc:
        reason = activity_abort_reason(activity_id)
        failure: BaseException = (
            GovernanceHaltError(reason)
            if reason is not None
            else (unwrap_governance_error(exc) or exc)
        )
        mw.clear_activity(activity_id)
        if mw._config.send_llm_end_event:
            await send_orphan_closure(mw, COMPLETED_EVENT, activity_id, ACTIVITY_TYPE, failure)
        raise (mw._denial(failure) if isinstance(failure, GovernanceError) else failure) from exc
    else:
        mw.clear_activity(activity_id)

    # The text itself is not sent back: Citadel streams it to the user and
    # persists it to the transcript, so duplicating it here doubles the egress
    # of every turn for no policy that reads it.
    await _complete(
        mw,
        activity_id,
        status="completed",
        duration_ms=time.monotonic() * 1000 - started_ms,
        model=model,
        was_approved=was_approved,
        output_tokens=chunks or None,
        has_tool_calls=False,
    )


async def govern_call(
    mw: OpenBoxCitadelMiddleware,
    coro: Any,
    *,
    model: str | None = None,
    prompt: str | None = None,
) -> Any:
    """Non-streaming variant, for the router, title and memory nodes."""
    activity_id = hex_id(32)
    started_ms = time.monotonic() * 1000
    await _authorize(mw, activity_id, prompt, model)
    _register(mw, activity_id)

    try:
        with run_with_activity(activity_id):
            result = await coro
        if has_activity_abort(activity_id):
            await poll_approval_or_halt(mw, activity_id, ACTIVITY_TYPE)
            mark_activity_approved(activity_id)
            clear_activity_abort(activity_id)
        was_approved = is_activity_approved(activity_id)
    except BaseException as exc:
        reason = activity_abort_reason(activity_id)
        failure: BaseException = (
            GovernanceHaltError(reason)
            if reason is not None
            else (unwrap_governance_error(exc) or exc)
        )
        mw.clear_activity(activity_id)
        if mw._config.send_llm_end_event:
            await send_orphan_closure(mw, COMPLETED_EVENT, activity_id, ACTIVITY_TYPE, failure)
        raise (mw._denial(failure) if isinstance(failure, GovernanceError) else failure) from exc
    else:
        mw.clear_activity(activity_id)

    # Model, token counts, finish reason and the completion text. Without them a
    # session records that a model was called but not which one, at what cost,
    # or what it said.
    meta = response_metadata(result)
    completion = meta.get("completion")
    await _complete(
        mw,
        activity_id,
        status="completed",
        duration_ms=time.monotonic() * 1000 - started_ms,
        model=model,
        was_approved=was_approved,
        # Core persists `activity_output` into the event's `output` column; the
        # loose `completion` field has no column and is dropped. A turn that
        # only called tools produces no text and correctly carries no output.
        activity_output=safe_serialize({"result": completion}) if completion else None,
        **{k: v for k, v in meta.items() if k != "llm_model"},
    )
    return result


def last_user_message(messages: list[Any]) -> str | None:
    """The governed prompt is the LAST human message, not all of them.

    Joining every human message would concatenate the chat history loaded from
    memory into one blob, so a policy matching on prompt content would be
    reading prior turns as if the user had just said them.
    """
    for message in reversed(messages or []):
        role = (
            message.get("role")
            if isinstance(message, dict)
            else getattr(message, "type", None) or getattr(message, "role", None)
        )
        if role in ("user", "human"):
            content = (
                message.get("content")
                if isinstance(message, dict)
                else getattr(message, "content", None)
            )
            return content if isinstance(content, str) else str(content)
    return None


__all__ = ["ACTIVITY_TYPE", "govern_call", "govern_stream", "last_user_message"]
