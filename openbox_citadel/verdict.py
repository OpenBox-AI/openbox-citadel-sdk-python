"""Verdict enforcement — the six arms, guardrails, and the remediation patch."""

from __future__ import annotations

from typing import Any

from openbox_citadel.types import VerdictArm, patch_from, with_patch_hint


class GovernanceHaltError(Exception):
    """Stop everything. Not the same as failing one action."""


class GovernanceBlockedError(Exception):
    """This action is refused.

    `patch` carries Core's remediation directive when it sent one. The whole
    point of a directive is that something downstream can act on it: an agent
    blocked with "retry with the capped amount" that never reads the patch tells
    its user to contact support instead.
    """

    def __init__(
        self, verdict: VerdictArm, message: str, patch: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.verdict = verdict
        self.patch = patch


class GuardrailsValidationError(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons) if reasons else "Guardrails validation failed")
        self.reasons = reasons


GovernanceError = (GovernanceHaltError, GovernanceBlockedError, GuardrailsValidationError)


def verdict_from_string(value: Any) -> VerdictArm:
    """Normalize whatever Core sent into one of the six arms."""
    if not isinstance(value, str):
        return "allow"
    normalized = value.lower().replace("-", "_")
    if normalized == "continue":
        return "allow"
    if normalized == "stop":
        return "halt"
    if normalized == "request_approval":
        return "require_approval"
    if normalized in ("allow", "monitor", "constrain", "block", "halt", "require_approval"):
        return normalized  # type: ignore[return-value]
    return "allow"


def _field(response: Any, *names: str) -> Any:
    for name in names:
        value = (
            response.get(name)
            if isinstance(response, dict)
            else getattr(response, name, None)
        )
        if value is not None:
            return value
    return None


class VerdictResult:
    """What the caller should do next."""

    __slots__ = ("approval_id", "requires_hitl")

    def __init__(self, *, requires_hitl: bool = False, approval_id: str | None = None) -> None:
        self.requires_hitl = requires_hitl
        self.approval_id = approval_id


def enforce_verdict(response: Any, phase: str) -> VerdictResult:
    """Map a verdict arm onto an exception, or a signal to poll.

    `arm` is read before `verdict`: Core sends both and `arm` is authoritative.
    Reading only `verdict` silently downgrades every non-allow decision.
    """
    arm = verdict_from_string(_field(response, "arm", "verdict"))
    reason = _field(response, "reason")

    if arm == "halt":
        raise GovernanceHaltError(
            f"OpenBox governance halt at {phase}: {reason or 'halted by policy'}"
        )

    if arm == "block":
        patch = patch_from(response)
        raise GovernanceBlockedError(
            "block",
            with_patch_hint(
                f"OpenBox governance block at {phase}: {reason or 'blocked by policy'}",
                patch,
            ),
            patch,
        )

    guardrails = _field(response, "guardrails_result", "guardrailsResult")
    if guardrails is not None and _field(guardrails, "validation_passed") is False:
        raw = _field(guardrails, "reasons") or []
        reasons = [
            r.get("reason") if isinstance(r, dict) else getattr(r, "reason", None) for r in raw
        ]
        raise GuardrailsValidationError([r for r in reasons if isinstance(r, str) and r])

    if arm == "require_approval":
        # Core's own approval id, not our activity id. Polling with the wrong
        # key asks about something Core is not tracking.
        approval_id = _field(response, "approval_id", "approvalId", "id")
        return VerdictResult(requires_hitl=True, approval_id=approval_id)

    # allow / monitor / constrain all proceed.
    return VerdictResult()


def unwrap_governance_error(exc: BaseException | None) -> BaseException | None:
    """Recover one of our errors from anywhere in a `__cause__` chain.

    A governance error raised inside an instrumented HTTP call surfaces through
    whatever client made that call, and most wrap any exception their transport
    raises in a generic transport error — burying the real reason. Without
    unwrapping, both the error the user sees and the closure telemetry sent to
    Core read "Connection error." instead of the actual refusal.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, GovernanceError):
            return current
        translated = _from_core_error(current)
        if translated is not None:
            return translated
        current = current.__cause__ or current.__context__
    return None


def _from_core_error(exc: BaseException) -> BaseException | None:
    """The base SDK's governance errors, as this SDK's own.

    A refusal decided on a span — a behavior rule, or a hook-level policy on an
    HTTP, DB or file call — is raised by the base runtime's instrumentation,
    with `openbox_core`'s classes, from inside the tool's own call. Left as
    they are, they miss every `GovernanceError` check, so the host gets an
    OpenBox type instead of its `deny_exc`, and a hook-level approval is never
    polled.
    """
    from openbox_core import errors as core

    if isinstance(exc, core.GovernanceHaltError):
        translated: BaseException = GovernanceHaltError(str(exc))
    elif isinstance(exc, core.GovernanceBlockedError):
        verdict = getattr(exc.verdict, "value", exc.verdict)
        arm = verdict_from_string(verdict)
        message = exc.reason or str(exc)
        translated = (
            GovernanceHaltError(message) if arm == "halt"
            else GovernanceBlockedError(arm if arm != "allow" else "block", message)
        )
    elif isinstance(exc, core.GuardrailsValidationError):
        translated = GuardrailsValidationError(list(exc.reasons))
    else:
        return None
    translated.__cause__ = exc
    return translated


def format_activity_rejected_message(reason: Any) -> str:
    """Avoids the "Activity rejected: Activity rejected" that inline formatting produced."""
    trimmed = reason.strip() if isinstance(reason, str) else ""
    return f"Activity rejected: {trimmed}" if trimmed else "Activity rejected (no reason provided)"


__all__ = [
    "GovernanceBlockedError",
    "GovernanceError",
    "GovernanceHaltError",
    "GuardrailsValidationError",
    "VerdictResult",
    "enforce_verdict",
    "format_activity_rejected_message",
    "unwrap_governance_error",
    "verdict_from_string",
]
