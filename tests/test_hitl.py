"""HITL. The bug here was silent: approvals appeared to work and gated nothing."""

from __future__ import annotations

import pytest

from tests.conftest import Ctx, Decl
from tests.doubles import Denied, FakeClient


async def _tool(args, ctx):
    return "sent"


async def test_pending_poll_is_not_an_approval(mw, client: FakeClient) -> None:
    """A response with no arm/verdict/action means Core has not recorded a human
    decision yet. Normalizing that absence to `allow` resolves the loop on its
    very first tick, before anyone approved anything."""
    client.verdicts = {"Wire": "require_approval"}
    client.approval_script = [{}, {}, {"arm": "allow"}]  # pending, pending, approved

    await mw.before_turn(goal="g")
    assert await mw.govern(Decl("Wire"), _tool)({}, Ctx()) == "sent"
    assert len(client.polls) == 3, "must keep polling until a decision arrives"


async def test_poll_uses_the_core_approval_id(mw, client: FakeClient) -> None:
    """Core keys the approval by its own id, not our activity id."""
    client.verdicts = {"Wire": {"arm": "require_approval", "approval_id": "apr_123"}}
    await mw.before_turn(goal="g")
    await mw.govern(Decl("Wire"), _tool)({}, Ctx())

    assert client.polls[0]["approval_id"] == "apr_123"


async def test_rejection_is_terminal(mw, client: FakeClient) -> None:
    client.verdicts = {"Wire": "require_approval"}
    client.approval_script = [{"arm": "block", "reason": "over limit"}]

    async def must_not_run(args, ctx):
        raise AssertionError("must not execute after a human said no")

    await mw.before_turn(goal="g")
    with pytest.raises(Denied, match="over limit"):
        await mw.govern(Decl("Wire"), must_not_run)({}, Ctx())


async def test_rejection_message_is_not_doubled(mw, client: FakeClient) -> None:
    """Inline formatting produced "Activity rejected: Activity rejected"."""
    client.verdicts = {"Wire": "require_approval"}
    client.approval_script = [{"arm": "block"}]

    await mw.before_turn(goal="g")
    with pytest.raises(Denied) as caught:
        await mw.govern(Decl("Wire"), _tool)({}, Ctx())
    assert "no reason provided" in str(caught.value)


async def test_expired_approval_halts(mw, client: FakeClient) -> None:
    client.verdicts = {"Wire": "require_approval"}
    client.approval_script = [{"expired": True}]

    await mw.before_turn(goal="g")
    with pytest.raises(Denied, match="expired"):
        await mw.govern(Decl("Wire"), _tool)({}, Ctx())


async def test_approval_wait_is_bounded(mw, client: FakeClient) -> None:
    """Citadel's SPA aborts a silent stream at 90s."""
    client.verdicts = {"Slow": "require_approval"}
    client.approval_script = [{}]  # never decides
    mw._options.approval_max_wait_seconds = 0.05

    await mw.before_turn(goal="g")
    with pytest.raises(Denied, match="timed out"):
        await mw.govern(Decl("Slow"), _tool)({}, Ctx())


async def test_approved_start_does_not_enforce_again_on_completion(
    mw, client: FakeClient
) -> None:
    """A second enforcement creates a spurious approval row on Core for work a
    human already signed off."""
    client.verdicts = {"Wire": "require_approval"}
    await mw.before_turn(goal="g")
    await mw.govern(Decl("Wire"), _tool)({}, Ctx())

    assert len(client.polls) == 1, "one approval per action, not one per stage"


async def test_hitl_disabled_halts_rather_than_proceeding(mw, client: FakeClient) -> None:
    client.verdicts = {"Wire": "require_approval"}
    mw._config.hitl.enabled = False
    await mw.before_turn(goal="g")
    with pytest.raises(Denied):
        await mw.govern(Decl("Wire"), _tool)({}, Ctx())
