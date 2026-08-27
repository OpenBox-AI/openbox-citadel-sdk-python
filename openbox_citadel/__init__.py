"""OpenBox governance for Citadel — 4142's LangGraph + CrewAI agent engine.

Citadel has no `create_agent(middleware=[...])` to hand a middleware to: direct
chat runs a hand-rolled tool loop and campaigns run CrewAI's kickoff. Both take
their tools from one loader, so the middleware attaches explicitly.

    from openbox_citadel import create_openbox_citadel_middleware
    from engine.tools.guard import ToolAccessDenied

    mw = create_openbox_citadel_middleware(deny_exc=ToolAccessDenied)

    await mw.before_turn(workflow_type="chat", goal=user_message)
    try:
        ...                                   # the turn
    finally:
        await mw.after_turn()

See `docs/integration.md` for the Citadel seams and the exact diffs.
"""

from importlib.metadata import PackageNotFoundError, version

from openbox_citadel.activity_registry import (
    current_activity,
    register_activity,
    run_with_activity,
    unregister_activity,
)
from openbox_citadel.client import GovernanceClient, OpenBoxAuthError
from openbox_citadel.config import (
    DEFAULT_APPROVAL_MAX_WAIT_SECONDS,
    GovernanceConfig,
    HITLConfig,
)
from openbox_citadel.event_sequence import (
    EventSequencer,
    SequenceViolation,
    find_sequence_violations,
    is_monotonic,
    release_sequencer,
    reset_sequencers,
    sequencer_for,
)
from openbox_citadel.events import base_event_fields, build_event, send_orphan_closure
from openbox_citadel.hitl import poll_approval_or_halt
from openbox_citadel.llm_hook import ACTIVITY_TYPE as LLM_ACTIVITY_TYPE
from openbox_citadel.llm_hook import last_user_message
from openbox_citadel.metadata import has_human_turn, response_metadata
from openbox_citadel.middleware import (
    TASK_QUEUE,
    OpenBoxCitadelMiddleware,
    OpenBoxCitadelMiddlewareOptions,
)
from openbox_citadel.middleware_factory import create_openbox_citadel_middleware
from openbox_citadel.tool_hook import handle_activity, handle_tool_call
from openbox_citadel.types import (
    VerdictArm,
    error_info,
    hex_id,
    patch_from,
    rfc3339_now,
    safe_serialize,
    with_patch_hint,
)
from openbox_citadel.verdict import (
    GovernanceBlockedError,
    GovernanceHaltError,
    GuardrailsValidationError,
    VerdictResult,
    enforce_verdict,
    format_activity_rejected_message,
    unwrap_governance_error,
    verdict_from_string,
)

try:
    __version__ = version("openbox-citadel-sdk-python")
except PackageNotFoundError:  # editable install without metadata
    __version__ = "0.0.0.dev0"

__all__ = [
    "DEFAULT_APPROVAL_MAX_WAIT_SECONDS",
    "LLM_ACTIVITY_TYPE",
    "TASK_QUEUE",
    "EventSequencer",
    "GovernanceBlockedError",
    "GovernanceClient",
    "GovernanceConfig",
    "GovernanceHaltError",
    "GuardrailsValidationError",
    "HITLConfig",
    "OpenBoxAuthError",
    "OpenBoxCitadelMiddleware",
    "OpenBoxCitadelMiddlewareOptions",
    "SequenceViolation",
    "VerdictArm",
    "VerdictResult",
    "__version__",
    "base_event_fields",
    "build_event",
    "create_openbox_citadel_middleware",
    "current_activity",
    "enforce_verdict",
    "error_info",
    "find_sequence_violations",
    "format_activity_rejected_message",
    "handle_activity",
    "handle_tool_call",
    "has_human_turn",
    "hex_id",
    "is_monotonic",
    "last_user_message",
    "patch_from",
    "poll_approval_or_halt",
    "register_activity",
    "release_sequencer",
    "reset_sequencers",
    "response_metadata",
    "rfc3339_now",
    "run_with_activity",
    "safe_serialize",
    "send_orphan_closure",
    "sequencer_for",
    "unregister_activity",
    "unwrap_governance_error",
    "verdict_from_string",
    "with_patch_hint",
]
