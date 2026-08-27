"""Ordering. Core orders by the timestamp we stamp; the dashboard renders it literally."""

from __future__ import annotations

import asyncio

from openbox_citadel.event_sequence import (
    EventSequencer,
    find_sequence_violations,
    is_monotonic,
)
from tests.conftest import Ctx, Decl
from tests.doubles import FakeClient


async def _tool(args, ctx):
    return "ok"


def _event(event_type: str, activity_id: str | None = None) -> dict:
    e = {"event_type": event_type}
    if activity_id:
        e["activity_id"] = activity_id
    return e


# ── monotonicity ────────────────────────────────────────────────────


def test_ties_are_broken_not_repeated() -> None:
    """Several ActivityStarted inside one millisecond is normal for a parallel
    tool round. Tied timestamps have no defined order, so the timeline can show
    a tool completing before the one before it started."""
    sequencer = EventSequencer()
    stamped = [sequencer.stamp(_event("WorkflowStarted")) for _ in range(50)]
    stamps = [e["timestamp"] for e in stamped]
    assert len(set(stamps)) == 50, "every event needs a distinct timestamp"
    assert stamps == sorted(stamps)


def test_sequence_is_gapless_and_one_based() -> None:
    sequencer = EventSequencer()
    stamped = [sequencer.stamp(_event("WorkflowStarted")) for _ in range(10)]
    assert [e["sequence"] for e in stamped] == list(range(1, 11))


def test_backwards_clock_does_not_regress(monkeypatch) -> None:
    """Wall-clock time is not monotonic; an NTP correction mid-run would
    otherwise reorder everything after it."""
    import openbox_citadel.event_sequence as es

    times = iter([1000.0, 1000.5, 999.0, 999.1, 1002.0])
    monkeypatch.setattr(es.time, "time", lambda: next(times))
    sequencer = EventSequencer()
    stamped = [sequencer.stamp(_event("WorkflowStarted")) for _ in range(5)]
    stamps = [e["timestamp"] for e in stamped]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == 5


async def test_emitted_run_is_monotonic(mw, client: FakeClient) -> None:
    await mw.before_turn(goal="g")
    await mw.govern(Decl("A"), _tool)({}, Ctx())
    await mw.govern(Decl("B"), _tool)({}, Ctx())
    await mw.after_turn()
    assert is_monotonic(client.events)


async def test_parallel_tools_still_produce_one_ordered_run(mw, client: FakeClient) -> None:
    """The SDK must not serialize sending — each POST costs ~840ms against a
    real Core, so chaining a three-tool round would triple its latency."""
    await mw.before_turn(goal="g")
    await asyncio.gather(
        *(mw.govern(Decl(f"T{i}"), _tool)({}, Ctx()) for i in range(4))
    )
    await mw.after_turn()
    assert is_monotonic(client.events)
    assert len(client.of_type("ActivityStarted")) == 4


# ── violations ──────────────────────────────────────────────────────


def test_completion_without_start_is_reported() -> None:
    violations = find_sequence_violations([
        _event("WorkflowStarted"),
        _event("ActivityCompleted", "a1"),
    ])
    assert [v.kind for v in violations] == ["completed_without_started"]


def test_duplicate_completion_is_reported() -> None:
    violations = find_sequence_violations([
        _event("WorkflowStarted"),
        _event("ActivityStarted", "a1"),
        _event("ActivityCompleted", "a1"),
        _event("ActivityCompleted", "a1"),
    ])
    assert [v.kind for v in violations] == ["duplicate_completion"]


def test_event_after_workflow_completed_is_reported() -> None:
    violations = find_sequence_violations([
        _event("WorkflowStarted"),
        _event("WorkflowCompleted"),
        _event("ActivityStarted", "a1"),
    ])
    assert [v.kind for v in violations] == ["event_after_workflow_completed"]


def test_activity_before_workflow_started_is_reported() -> None:
    violations = find_sequence_violations([_event("ActivityStarted", "a1")])
    assert [v.kind for v in violations] == ["activity_before_workflow_started"]


def test_a_well_formed_run_has_no_violations(mw) -> None:
    assert find_sequence_violations([
        _event("WorkflowStarted"),
        _event("ActivityStarted", "a1"),
        _event("ActivityCompleted", "a1"),
        _event("WorkflowCompleted"),
    ]) == []


# ── dangling ────────────────────────────────────────────────────────


async def test_dangling_activity_is_closed_at_run_end(mw, client: FakeClient) -> None:
    """Trust scoring never finalizes for a session with an open activity."""
    from openbox_citadel.events import build_event

    await mw.before_turn(goal="g")
    await mw._evaluate(build_event(mw, "ActivityStarted", "orphan-1", "Leaked"))
    await mw.after_turn()

    closures = [e for e in client.of_type("ActivityCompleted") if e["activity_id"] == "orphan-1"]
    assert len(closures) == 1
    assert closures[0]["status"] == "failed"
    assert client.types[-1] == "WorkflowCompleted"


# ── wire event names ────────────────────────────────────────────────


def test_sdk_event_names_map_to_the_canonical_six() -> None:
    """Core rejects anything else outright:
    `{"code": 400, "message": "invalid event_type: LLMStarted"}`."""
    from openbox_citadel.types import to_server_event_type

    assert to_server_event_type("LLMStarted") == "ActivityStarted"
    assert to_server_event_type("ToolStarted") == "ActivityStarted"
    assert to_server_event_type("LLMCompleted") == "ActivityCompleted"
    assert to_server_event_type("WorkflowStarted") == "WorkflowStarted"
    assert to_server_event_type("WorkflowFailed") == "WorkflowFailed"
    assert to_server_event_type("Nonsense") == "ActivityCompleted"


def test_sequencer_tracks_sdk_internal_names() -> None:
    """The sequencer sees the descriptive name, so its pairing must accept both."""
    assert find_sequence_violations([
        _event("WorkflowStarted"),
        _event("LLMStarted", "a1"),
        _event("LLMCompleted", "a1"),
        _event("WorkflowCompleted"),
    ]) == []
