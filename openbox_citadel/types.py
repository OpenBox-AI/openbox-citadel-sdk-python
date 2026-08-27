"""Core types.

Field names are the wire contract, not a local style choice: the server
classifies an event by the names it arrives under. Renaming one here changes
what the server sees, so do not "tidy" a name without changing the protocol.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Any, Literal

VerdictArm = Literal["allow", "monitor", "constrain", "block", "halt", "require_approval"]
"""Six arms, not four.

`monitor` and `constrain` are live and non-blocking. An earlier version of this
SDK enumerated only four and treated anything unrecognised as `allow`, which
happens to be right for these two and wrong in principle.
"""


def rfc3339_now() -> str:
    """Millisecond precision, always — see `event_sequence._iso_ms`."""
    moment = datetime.now(UTC)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"


def hex_id(length: int = 32) -> str:
    """Activity ids: lowercase hex, the form the server expects."""
    return secrets.token_hex(length // 2)


def safe_serialize(value: Any) -> Any:
    """Best-effort JSON-safe projection. Never raises."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return {"repr": str(value)}
    return value


def error_info(exc: BaseException) -> dict[str, Any]:
    """Structured error shape Core requires.

    A bare string in an event's `error` field is rejected, so this is never
    optional. `stack_trace` is what makes a failed activity debuggable from the
    dashboard instead of just red.
    """
    import traceback

    info: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc) or type(exc).__name__,
    }
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if trace.strip():
        info["stack_trace"] = trace[:8000]
    return info


SERVER_EVENT_TYPES: dict[str, str] = {
    "WorkflowStarted": "WorkflowStarted",
    "WorkflowCompleted": "WorkflowCompleted",
    "WorkflowFailed": "WorkflowFailed",
    "SignalReceived": "SignalReceived",
    "ActivityStarted": "ActivityStarted",
    "ActivityCompleted": "ActivityCompleted",
    # SDK-internal names. Descriptive in logs and in the sequencer, but Core
    # accepts only the canonical six and 400s on anything else:
    #   {"code": 400, "message": "invalid event_type: LLMStarted"}
    "LLMStarted": "ActivityStarted",
    "ToolStarted": "ActivityStarted",
    "LLMCompleted": "ActivityCompleted",
    "ToolCompleted": "ActivityCompleted",
    "LLMFailed": "ActivityCompleted",
    "ToolFailed": "ActivityCompleted",
}


def to_server_event_type(event_type: str) -> str:
    """Map an SDK-internal event name onto one Core accepts.

    Unknown names fall through to `ActivityCompleted` rather than being sent
    as-is: Core rejects unrecognised types outright, and losing an event is
    worse than mislabelling one.
    """
    return SERVER_EVENT_TYPES.get(event_type, "ActivityCompleted")


def patch_from(response: Any) -> dict[str, Any] | None:
    """Read the remediation directive off a verdict, if it carries a usable one.

    Core validates the shape (exactly `new_input`, <=64 KiB, boolean rejected)
    and drops anything malformed, so this only guards against a body that is not
    an object at all.
    """
    patch = getattr(response, "patch", None)
    if patch is None and isinstance(response, dict):
        patch = response.get("patch")
    if patch is None or not isinstance(patch, dict):
        return None
    return patch


def with_patch_hint(message: str, patch: dict[str, Any] | None) -> str:
    """Append the policy's suggested input to a block message.

    This message is what reaches the model when a governed tool is blocked, so
    it is what lets an agent act on the directive: the model sees the
    suggestion and may retry — and that retry is governed again like any other
    call. The SDK never applies the patch itself. Silently re-running a
    money-moving tool with arguments the caller never wrote is not a decision an
    observability layer gets to make.
    """
    if patch is None or "new_input" not in patch:
        return message
    try:
        suggestion = json.dumps(patch["new_input"])
    except (TypeError, ValueError):
        return message
    return f"{message} — policy suggests retrying with: {suggestion}"


__all__ = [
    "VerdictArm",
    "error_info",
    "hex_id",
    "patch_from",
    "rfc3339_now",
    "safe_serialize",
    "to_server_event_type",
    "with_patch_hint",
]
