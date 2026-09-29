"""Verdict arms, the remediation patch, guardrails and error unwrapping."""

from __future__ import annotations

import pytest

from openbox_citadel.types import error_info, with_patch_hint
from openbox_citadel.verdict import (
    GovernanceBlockedError,
    GovernanceHaltError,
    GuardrailsValidationError,
    enforce_verdict,
    format_activity_rejected_message,
    unwrap_governance_error,
    verdict_from_string,
)
from tests.conftest import Ctx, Decl
from tests.doubles import Denied, FakeClient


async def _tool(args, ctx):
    return "ok"


# ── arms ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("allow", "allow"), ("monitor", "monitor"), ("constrain", "constrain"),
        ("block", "block"), ("halt", "halt"), ("require_approval", "require_approval"),
        ("continue", "allow"), ("stop", "halt"), ("request_approval", "require_approval"),
        ("REQUIRE-APPROVAL", "require_approval"), (None, "allow"), ("nonsense", "allow"),
    ],
)
def test_verdict_normalization(raw, expected) -> None:
    assert verdict_from_string(raw) == expected


def test_arm_wins_over_verdict() -> None:
    """Core sends both; `arm` is authoritative. Reading only `verdict` silently
    downgrades every non-allow decision."""
    with pytest.raises(GovernanceHaltError):
        enforce_verdict({"arm": "halt", "verdict": "allow"}, "tool_start")


def test_monitor_and_constrain_proceed() -> None:
    """Both are live arms and neither blocks."""
    for arm in ("monitor", "constrain"):
        assert enforce_verdict({"arm": arm}, "tool_start").requires_hitl is False


def test_approval_id_is_carried_off_the_verdict() -> None:
    result = enforce_verdict({"arm": "require_approval", "approval_id": "apr_9"}, "tool_start")
    assert result.requires_hitl and result.approval_id == "apr_9"


# ── patch ───────────────────────────────────────────────────────────


def test_block_carries_the_remediation_patch() -> None:
    with pytest.raises(GovernanceBlockedError) as caught:
        enforce_verdict(
            {"arm": "block", "reason": "over limit", "patch": {"new_input": {"amount": 500}}},
            "tool_start",
        )
    assert caught.value.patch == {"new_input": {"amount": 500}}
    assert "policy suggests retrying with" in str(caught.value)


def test_patch_hint_survives_to_the_model(mw, client: FakeClient) -> None:
    """An agent blocked with "retry with the capped amount" that never reads the
    patch tells its user to contact support instead."""
    assert "500" in with_patch_hint("blocked", {"new_input": {"amount": 500}})


def test_malformed_patch_degrades_to_a_plain_block() -> None:
    with pytest.raises(GovernanceBlockedError) as caught:
        enforce_verdict({"arm": "block", "patch": "not-an-object"}, "tool_start")
    assert caught.value.patch is None


def test_absent_new_input_adds_no_hint() -> None:
    assert with_patch_hint("blocked", {}) == "blocked"


# ── guardrails ──────────────────────────────────────────────────────


def test_failed_guardrails_raise_with_reasons() -> None:
    with pytest.raises(GuardrailsValidationError, match="contains PII"):
        enforce_verdict(
            {
                "arm": "allow",
                "guardrails_result": {
                    "validation_passed": False,
                    "reasons": [{"reason": "contains PII"}],
                },
            },
            "tool_start",
        )


# ── error unwrapping ────────────────────────────────────────────────


def test_governance_error_is_recovered_from_a_cause_chain() -> None:
    """Most HTTP clients wrap anything their transport raises in a generic
    error, burying the real refusal in `__cause__`."""
    root = GovernanceHaltError("Activity rejected: over limit")
    try:
        try:
            raise root
        except GovernanceHaltError as inner:
            raise ConnectionError("Connection error.") from inner
    except ConnectionError as outer:
        recovered = unwrap_governance_error(outer)
    assert recovered is root


def test_unwrapping_a_plain_error_returns_none() -> None:
    assert unwrap_governance_error(ValueError("boom")) is None


def test_unwrapping_survives_a_cycle() -> None:
    a = ValueError("a")
    b = ValueError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert unwrap_governance_error(a) is None


# ── messages and error shape ────────────────────────────────────────


def test_rejected_message_is_not_doubled() -> None:
    assert format_activity_rejected_message("Activity rejected") == (
        "Activity rejected: Activity rejected"
    )
    assert format_activity_rejected_message(None) == "Activity rejected (no reason provided)"


def test_error_info_is_structured_with_a_stack_trace() -> None:
    """Core rejects a bare string in an event's `error` field."""
    try:
        raise ValueError("boom")
    except ValueError as exc:
        info = error_info(exc)
    assert info["type"] == "ValueError"
    assert info["message"] == "boom"
    assert "ValueError: boom" in info["stack_trace"]


# ── denial translation ──────────────────────────────────────────────


async def test_only_governance_errors_become_the_hosts_denial(mw, client: FakeClient) -> None:
    """A tool's own TimeoutError must reach Citadel unchanged — translating it
    would read to the engine as an access-control decision."""

    async def times_out(args, ctx):
        raise TimeoutError("upstream slow")

    await mw.before_turn(goal="g")
    with pytest.raises(TimeoutError):
        await mw.govern(Decl("Slow"), times_out)({}, Ctx())


async def test_block_becomes_the_hosts_denial(mw, client: FakeClient) -> None:
    client.verdicts = {"Apollo": "block"}
    await mw.before_turn(goal="g")
    with pytest.raises(Denied):
        await mw.govern(Decl("Apollo"), _tool)({}, Ctx())


def test_core_block_raised_inside_a_tool_call_is_translated() -> None:
    """A behavior rule decided on a span is raised by the base runtime with its
    own class, wrapped by the HTTP client. It must come back as ours."""
    from openbox_core import errors as core

    from openbox_citadel.verdict import GovernanceBlockedError, unwrap_governance_error

    try:
        try:
            raise core.GovernanceBlockedError("block", "no prior contact lookup")
        except core.GovernanceBlockedError as inner:
            raise ConnectionError("transport failed") from inner
    except ConnectionError as outer:
        found = unwrap_governance_error(outer)
    assert isinstance(found, GovernanceBlockedError)
    assert found.verdict == "block"
    assert "no prior contact lookup" in str(found)


def test_core_halt_and_hook_approval_are_translated() -> None:
    from openbox_core import errors as core

    from openbox_citadel.verdict import (
        GovernanceBlockedError,
        GovernanceHaltError,
        GuardrailsValidationError,
        unwrap_governance_error,
    )

    assert isinstance(unwrap_governance_error(core.GovernanceHaltError("stop")), GovernanceHaltError)
    approval = unwrap_governance_error(core.GovernanceBlockedError("require_approval", "human"))
    assert isinstance(approval, GovernanceBlockedError) and approval.verdict == "require_approval"
    rails = unwrap_governance_error(core.GuardrailsValidationError(["PII"]))
    assert isinstance(rails, GuardrailsValidationError) and rails.reasons == ["PII"]
