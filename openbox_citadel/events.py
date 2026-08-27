"""Event construction — the one place ordering can be guaranteed for a run."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from openbox_citadel.event_sequence import sequencer_for
from openbox_citadel.types import error_info, rfc3339_now

if TYPE_CHECKING:
    from openbox_citadel.middleware import OpenBoxCitadelMiddleware

logger = logging.getLogger("openbox_citadel.events")


def base_event_fields(mw: OpenBoxCitadelMiddleware) -> dict[str, Any]:
    """The fields every event in a run carries."""
    return {
        "source": "workflow-telemetry",
        "workflow_id": mw._workflow_id,
        "run_id": mw._run_id,
        "workflow_type": mw._workflow_type,
        "task_queue": mw._config.task_queue,
        "timestamp": rfc3339_now(),
        "session_id": mw._config.session_id,
        "agent_name": mw._config.agent_name or mw._workflow_type,
    }


def build_event(
    mw: OpenBoxCitadelMiddleware,
    event_type: str,
    activity_id: str | None = None,
    activity_type: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Compose one event, check its ordering, and stamp its position.

    Every event in the SDK is built here, which is what makes a single
    monotonic sequence per run possible. See `event_sequence` for why
    wall-clock timestamps alone are not enough.
    """
    event: dict[str, Any] = {
        **base_event_fields(mw),
        "event_type": event_type,
    }
    if activity_id is not None:
        event["activity_id"] = activity_id
    if activity_type is not None:
        event["activity_type"] = activity_type
    event.update({k: v for k, v in fields.items() if v is not None})

    sequencer = sequencer_for(mw._run_id)
    violation = sequencer.check(event)
    if violation is not None:
        # Logged, never raised: an out-of-order event is a display problem, a
        # dropped one is a governance hole. Surfacing it makes the bug findable
        # instead of silently producing a nonsensical timeline.
        logger.warning(
            "event sequence violation (%s) for %s/%s",
            violation.kind,
            event_type,
            activity_type,
        )
    return sequencer.stamp(event)


async def send_orphan_closure(
    mw: OpenBoxCitadelMiddleware,
    completed_event_type: str,
    activity_id: str,
    activity_type: str,
    exc: BaseException,
) -> None:
    """Close an activity that was started and then refused before it ran.

    A hard block or halt at activity start otherwise leaves a true orphan on
    Core — and a blocked action is exactly what someone reading the audit trail
    goes looking for.
    """
    try:
        await mw._client.evaluate(
            build_event(
                mw,
                completed_event_type,
                activity_id,
                activity_type,
                status="failed",
                error=error_info(exc),
            )
        )
    except Exception:
        # Non-fatal: closure telemetry must not mask the original error.
        logger.debug("orphan closure failed", exc_info=True)


__all__ = ["base_event_fields", "build_event", "send_orphan_closure"]
