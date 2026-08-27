"""Ordering guarantees for a run's governance events.

Core orders a session's events by the `timestamp` the SDK stamps on them, and
the dashboard renders that order literally. Two things break it:

1. **Millisecond ties.** A round of parallel tool calls emits several
   `ActivityStarted` events inside the same millisecond. Events sharing a
   timestamp have no defined order, so the timeline can show a tool completing
   before the tool that preceded it started.
2. **Clock movement.** Wall-clock time is not monotonic. An NTP correction or a
   sleep/wake mid-run reorders everything after it.

So ordering is not left to wall-clock luck: every event gets a strictly
increasing timestamp within its run, plus an explicit `sequence` number that is
unambiguous even if two events are later normalized to the same instant.

This deliberately does **not** serialize the *sending* of events. Tools in a
parallel round run concurrently by design and each governance POST costs ~840ms
against a real Core — chaining them would triple the latency of a three-tool
round to fix a display problem.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

SequenceViolationKind = Literal[
    "completed_without_started",
    "duplicate_completion",
    "event_after_workflow_completed",
    "workflow_started_twice",
    "activity_before_workflow_started",
]


@dataclass(frozen=True, slots=True)
class SequenceViolation:
    kind: SequenceViolationKind
    activity_id: str = ""
    activity_type: str | None = None
    event_type: str | None = None


def _iso_ms(epoch_ms: int) -> str:
    """`2026-08-26T04:41:00.533Z` — always exactly three fractional digits.

    Matching JavaScript's `toISOString()`, which the other SDKs use. A format
    that drops fractional seconds on a whole second sorts inconsistently as a
    string: `...:40Z` compares *before* `...:40.500Z` because `Z` < `.`.
    """
    moment = datetime.fromtimestamp(epoch_ms / 1000, UTC)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{epoch_ms % 1000:03d}Z"


_START_EVENTS = frozenset({"ActivityStarted", "LLMStarted", "ToolStarted"})
_COMPLETION_EVENTS = frozenset({"ActivityCompleted", "LLMCompleted", "ToolCompleted"})


def _is_start(event_type: str) -> bool:
    return event_type in _START_EVENTS


def _is_completion(event_type: str) -> bool:
    return event_type in _COMPLETION_EVENTS


class EventSequencer:
    """Per-run ordering state. One instance per `run_id`; see `sequencer_for`."""

    __slots__ = (
        "_completed_activities",
        "_last_ms",
        "_open_activities",
        "_seq",
        "_workflow_completed",
        "_workflow_started",
    )

    def __init__(self) -> None:
        self._last_ms = 0
        self._seq = 0
        self._workflow_started = False
        self._workflow_completed = False
        self._open_activities: set[str] = set()
        self._completed_activities: set[str] = set()

    def stamp(self, event: dict[str, Any]) -> dict[str, Any]:
        """Assign this event its position in the run. Returns a new dict."""
        now = int(time.time() * 1000)
        # Strictly increasing: a tie or a backwards clock step advances by 1ms
        # rather than repeating or regressing.
        self._last_ms = now if now > self._last_ms else self._last_ms + 1
        self._seq += 1
        self._track(event)

        stamped = dict(event)
        stamped["timestamp"] = _iso_ms(self._last_ms)
        stamped["sequence"] = self._seq
        return stamped

    def _track(self, event: dict[str, Any]) -> None:
        event_type = event.get("event_type", "")
        activity_id = event.get("activity_id")
        if event_type == "WorkflowStarted":
            self._workflow_started = True
        elif event_type in ("WorkflowCompleted", "WorkflowFailed"):
            self._workflow_completed = True
        elif activity_id is not None and _is_start(event_type):
            self._open_activities.add(activity_id)
        elif activity_id is not None and _is_completion(event_type):
            self._open_activities.discard(activity_id)
            self._completed_activities.add(activity_id)

    def check(self, event: dict[str, Any]) -> SequenceViolation | None:
        """What is wrong with emitting this event now, or None.

        Advisory. The SDK logs violations rather than raising, because dropping
        a governance event to protect a display invariant is the wrong trade —
        a missing event is worse than an out-of-order one.
        """
        event_type = event.get("event_type", "")
        activity_id = event.get("activity_id")
        activity_type = event.get("activity_type")

        if self._workflow_completed:
            return SequenceViolation(
                "event_after_workflow_completed",
                activity_id or "",
                activity_type,
                event_type,
            )
        if event_type == "WorkflowStarted" and self._workflow_started:
            return SequenceViolation("workflow_started_twice")
        if activity_id is not None and _is_completion(event_type):
            if activity_id in self._completed_activities:
                return SequenceViolation("duplicate_completion", activity_id, activity_type)
            if activity_id not in self._open_activities:
                return SequenceViolation("completed_without_started", activity_id, activity_type)
        if (
            not self._workflow_started
            and activity_id is not None
            and (_is_start(event_type) or _is_completion(event_type))
        ):
            return SequenceViolation(
                "activity_before_workflow_started", activity_id, activity_type, event_type
            )
        return None

    def dangling_activities(self) -> list[str]:
        """Started but never completed. Used when closing a run."""
        return sorted(self._open_activities)


# ── registry ────────────────────────────────────────────────────────
#
# Keyed by run_id so concurrent runs on one middleware keep independent
# sequences. A shared counter would interleave two runs' numbering and make each
# look full of holes — which matters because Citadel serves many tenants from
# one process.

_sequencers: dict[str, EventSequencer] = {}


def sequencer_for(run_id: str) -> EventSequencer:
    sequencer = _sequencers.get(run_id)
    if sequencer is None:
        sequencer = EventSequencer()
        _sequencers[run_id] = sequencer
    return sequencer


def release_sequencer(run_id: str) -> None:
    """Drop a finished run's state."""
    _sequencers.pop(run_id, None)


def reset_sequencers() -> None:
    """Test hook: forget every run."""
    _sequencers.clear()


# ── offline verification ────────────────────────────────────────────


def find_sequence_violations(events: list[dict[str, Any]]) -> list[SequenceViolation]:
    """Replay the rules over already-emitted events, e.g. rows read from Core.

    Point this at a session that "looks wrong": it answers whether the order is
    genuinely invalid or merely concurrent.
    """
    sequencer = EventSequencer()
    violations: list[SequenceViolation] = []
    for event in events:
        violation = sequencer.check(event)
        if violation is not None:
            violations.append(violation)
        sequencer.stamp(event)
    return violations


def is_monotonic(events: list[dict[str, Any]]) -> bool:
    """Timestamps strictly increasing and `sequence` a gapless 1..N run."""
    last_ms = -1.0
    for index, event in enumerate(events):
        raw = str(event.get("timestamp", ""))
        try:
            parsed = datetime.fromisoformat(raw).timestamp() * 1000
        except ValueError:
            return False
        if parsed <= last_ms:
            return False
        last_ms = parsed
        if event.get("sequence") != index + 1:
            return False
    return True


__all__ = [
    "EventSequencer",
    "SequenceViolation",
    "find_sequence_violations",
    "is_monotonic",
    "release_sequencer",
    "reset_sequencers",
    "sequencer_for",
]
