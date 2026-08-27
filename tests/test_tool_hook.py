"""The tool flow: registration order, orphan closure, redaction, D5 composition."""

from __future__ import annotations

import asyncio

import pytest

from openbox_citadel.activity_registry import (
    activity_context,
    current_activity,
    is_activity_approved,
    set_activity_abort,
)
from tests.conftest import Ctx, Decl
from tests.doubles import Denied, FakeClient


async def _tool(args, ctx):
    return {"ok": True}


# ── registration order ──────────────────────────────────────────────


async def test_activity_registers_after_the_evaluate(mw, client: FakeClient) -> None:
    """Registering first would make this activity the attribution target while
    the evaluate's own HTTP request is in flight — the instrumentation would
    capture that request as a hook span and create a duplicate approval."""
    registered_during_evaluate: list[str | None] = []
    original = client.evaluate

    async def spy(event):
        if event["event_type"] == "ActivityStarted":
            registered_during_evaluate.append(activity_context(event["activity_id"]))
        return await original(event)

    client.evaluate = spy  # type: ignore[method-assign]
    await mw.before_turn(goal="g")
    await mw.govern(Decl("Apollo"), _tool)({}, Ctx())
    assert registered_during_evaluate == [None]


async def test_activity_is_scoped_during_execution(mw) -> None:
    seen: list[str | None] = []

    async def capture(args, ctx):
        seen.append(current_activity())
        return "ok"

    await mw.before_turn(goal="g")
    await mw.govern(Decl("Apollo"), capture)({}, Ctx())
    assert seen[0] is not None, "spans raised inside the tool must be attributable"
    assert current_activity() is None, "scope must not leak past the call"


async def test_scope_is_isolated_across_concurrent_tools(mw) -> None:
    """A plain global would not survive concurrent turns in one process."""
    seen: dict[str, str | None] = {}

    def make(name):
        async def impl(args, ctx):
            await asyncio.sleep(0.01)
            seen[name] = current_activity()
            return "ok"

        return impl

    await mw.before_turn(goal="g")
    await asyncio.gather(
        mw.govern(Decl("A"), make("A"))({}, Ctx()),
        mw.govern(Decl("B"), make("B"))({}, Ctx()),
    )
    assert seen["A"] != seen["B"] and all(v is not None for v in seen.values())


async def test_registry_is_cleaned_up(mw, client: FakeClient) -> None:
    """A context left registered attributes the NEXT activity's spans to this one."""
    await mw.before_turn(goal="g")
    await mw.govern(Decl("Apollo"), _tool)({}, Ctx())
    activity_id = client.of_type("ActivityStarted")[0]["activity_id"]
    assert activity_context(activity_id) is None
    assert not is_activity_approved(activity_id)


# ── orphan closure ──────────────────────────────────────────────────


async def test_block_at_start_closes_its_activity(mw, client: FakeClient) -> None:
    """A hard block at start otherwise leaves a true orphan on Core — and a
    blocked action is exactly what an auditor goes looking for."""
    client.verdicts = {"Apollo": "block"}
    await mw.before_turn(goal="g")
    with pytest.raises(Denied):
        await mw.govern(Decl("Apollo"), _tool)({}, Ctx())

    started = client.of_type("ActivityStarted")
    completed = client.of_type("ActivityCompleted")
    assert len(started) == len(completed) == 1
    assert started[0]["activity_id"] == completed[0]["activity_id"]
    assert completed[0]["status"] == "failed"
    assert "stack_trace" in completed[0]["error"]


async def test_failure_completes_with_structured_error(mw, client: FakeClient) -> None:
    async def boom(args, ctx):
        raise ValueError("tool blew up")

    await mw.before_turn(goal="g")
    with pytest.raises(ValueError):
        await mw.govern(Decl("Flaky"), boom)({}, Ctx())

    completed = client.of_type("ActivityCompleted")[0]
    assert completed["status"] == "failed"
    assert completed["error"]["type"] == "ValueError"


async def test_cancellation_still_completes(mw, client: FakeClient) -> None:
    """CancelledError is a BaseException — a bare `except Exception` misses it."""

    async def cancelled(args, ctx):
        raise asyncio.CancelledError()

    await mw.before_turn(goal="g")
    with pytest.raises(asyncio.CancelledError):
        await mw.govern(Decl("C"), cancelled)({}, Ctx())
    assert len(client.of_type("ActivityCompleted")) == 1


# ── mid-call abort ──────────────────────────────────────────────────


async def test_abort_flag_triggers_approval_even_without_an_exception(
    mw, client: FakeClient
) -> None:
    """Some tools catch transport errors internally and return them as values.
    The hook still set the abort flag, so the refusal must be discoverable by
    flag as well as by exception."""
    attempts: list[int] = []

    async def swallows(args, ctx):
        attempts.append(1)
        if len(attempts) == 1:
            set_activity_abort(current_activity() or "", "hook refused mid-call")
        return "looks fine"

    await mw.before_turn(goal="g")
    assert await mw.govern(Decl("Sneaky"), swallows)({}, Ctx()) == "looks fine"
    assert len(attempts) == 2, "must re-run after approval"
    assert len(client.polls) == 1


# ── redaction ───────────────────────────────────────────────────────


async def test_input_redaction_reaches_the_tool(mw, client: FakeClient) -> None:
    """Discarding redacted_input sends the unredacted value while the guardrail
    reports success — silent, and the whole point of the feature."""
    client.redact = [{"args": {"email": "[REDACTED]"}}]
    seen: list[dict] = []

    async def capture(args, ctx):
        seen.append(args)
        return "ok"

    await mw.before_turn(goal="g")
    await mw.govern(Decl("Apollo"), capture)({"email": "real@person.com"}, Ctx())
    assert seen == [{"email": "[REDACTED]"}]


# ── payload and composition ─────────────────────────────────────────


async def test_event_carries_session_and_agent_name(mw, client: FakeClient) -> None:
    mw._config.session_id = "sess-9"
    mw._config.agent_name = "Squidgy Sales"
    await mw.before_turn(goal="g")
    event = client.events[0]
    assert event["session_id"] == "sess-9"
    assert event["agent_name"] == "Squidgy Sales"
    assert event["task_queue"] == "citadel"


async def test_activity_input_is_an_array_with_caller_identity(mw, client: FakeClient) -> None:
    await mw.before_turn(goal="g")
    await mw.govern(Decl("T"), _tool)({"q": 1}, Ctx())
    payload = client.of_type("ActivityStarted")[0]["activity_input"]
    assert isinstance(payload, list)
    assert payload[0]["args"] == {"q": 1}
    assert payload[0]["tenant"] == "squidgy"
    assert "location_id" not in payload[0]


async def test_d5_runs_before_governance(mw, client: FakeClient) -> None:
    """Governance first would send OpenBox every call D5 was going to refuse,
    and a require_approval would page a human to approve an action that cannot run."""
    order: list[str] = []
    original = client.evaluate

    async def spy(event):
        if event["event_type"] == "ActivityStarted":
            order.append("openbox")
        return await original(event)

    client.evaluate = spy  # type: ignore[method-assign]

    async def impl(args, ctx):
        order.append("execute")
        return "ok"

    def guarded_stub(fn):
        async def inner(args, ctx):
            order.append("d5")
            return await fn(args, ctx)

        return inner

    await mw.before_turn(goal="g")
    await guarded_stub(mw.govern(Decl("T"), impl))({}, Ctx())
    assert order == ["d5", "openbox", "execute"]


async def test_d5_denial_never_reaches_openbox(mw, client: FakeClient) -> None:
    await mw.before_turn(goal="g")
    before = len(client.events)

    def guarded_stub(fn):
        async def inner(args, ctx):
            raise Denied("not granted")

        return inner

    with pytest.raises(Denied):
        await guarded_stub(mw.govern(Decl("T"), _tool))({}, Ctx())
    assert len(client.events) == before


async def test_skip_tool_types_is_a_full_passthrough(mw, client: FakeClient) -> None:
    mw._config.skip_tool_types = {"Noisy"}
    await mw.before_turn(goal="g")
    before = len(client.events)
    assert await mw.govern(Decl("Noisy"), _tool)({}, Ctx()) == {"ok": True}
    assert len(client.events) == before
