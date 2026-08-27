"""Human-in-the-loop polling.

The subtle bug this exists to avoid: a poll response with **no** arm, verdict or
action field means Core has not recorded a human decision yet — it is *pending*,
not approved. Normalizing an absent field to `allow` (correct for an evaluate
response, where unset means "no restriction stated") resolves the loop on its
very first tick, before anyone approved anything. Approvals then appear to work
while gating nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from openbox_citadel.types import patch_from, with_patch_hint
from openbox_citadel.verdict import (
    GovernanceHaltError,
    format_activity_rejected_message,
    verdict_from_string,
)

if TYPE_CHECKING:
    from openbox_citadel.middleware import OpenBoxCitadelMiddleware

logger = logging.getLogger("openbox_citadel.hitl")


async def poll_approval_or_halt(
    mw: OpenBoxCitadelMiddleware,
    activity_id: str,
    activity_type: str,
    approval_id: str | None = None,
) -> None:
    """Block until a human decides. Never auto-accept, never wait forever."""
    hitl = mw._config.hitl
    if not hitl.enabled:
        raise GovernanceHaltError(f"Approval required for activity {activity_type}")

    timeout_ms = mw._options.approval_max_wait_seconds
    timeout_ms = None if timeout_ms is None else timeout_ms * 1000
    started_at = time.monotonic() * 1000
    interval = hitl.poll_interval_ms / 1000

    while timeout_ms is None or (time.monotonic() * 1000 - started_at) <= timeout_ms:
        response: Any = await mw._client.poll_approval(
            workflow_id=mw._workflow_id,
            run_id=mw._run_id,
            activity_id=activity_id,
            approval_id=approval_id,
        )
        if response is None:
            await asyncio.sleep(interval)
            continue

        if response.get("expired"):
            raise GovernanceHaltError(
                f"Approval expired for activity {activity_type} "
                f"(workflow_id={mw._workflow_id}, activity_id={activity_id})"
            )

        raw = response.get("arm") or response.get("verdict") or response.get("action")
        if not isinstance(raw, str) or not raw.strip():
            # Still pending. This is the guard: an absent decision is not an allow.
            await asyncio.sleep(interval)
            continue

        arm = verdict_from_string(raw)
        if arm == "allow":
            return
        if arm in ("block", "halt"):
            # Terminal. A human said no, and that is not something to retry
            # around. The directive still rides along, because "denied, but this
            # would have been allowed" is the useful half for whoever reads the
            # trail.
            raise GovernanceHaltError(
                with_patch_hint(
                    format_activity_rejected_message(response.get("reason")),
                    patch_from(response),
                )
            )

        await asyncio.sleep(interval)

    raise GovernanceHaltError(
        f"Approval timed out for activity {activity_type} "
        f"(workflow_id={mw._workflow_id}, activity_id={activity_id})"
    )


__all__ = ["poll_approval_or_halt"]
