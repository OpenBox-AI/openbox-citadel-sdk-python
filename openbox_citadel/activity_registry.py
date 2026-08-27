"""Per-activity state: span attribution, approval and abort flags.

Three separate jobs, all keyed on `activity_id`:

* **Attribution.** `current_activity()` is a `ContextVar`, so a span raised by
  any instrumented call made inside `run_with_activity()` knows which activity
  caused it — including across `await` points, which a plain global would not
  survive under concurrent turns.
* **Approval.** An activity whose *start* already required and received human
  approval must not be enforced again on completion; a second enforcement
  creates a spurious approval row on Core for work a human already signed off.
* **Abort.** The hook layer can refuse a call mid-flight. Some tools catch
  transport errors internally and return them as strings rather than raising, so
  the refusal has to be discoverable by flag as well as by exception.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_current_activity: ContextVar[str | None] = ContextVar("openbox_current_activity", default=None)

_contexts: dict[str, dict[str, Any]] = {}
_approved: set[str] = set()
_aborts: dict[str, str] = {}


def register_activity(activity_id: str, context: dict[str, Any]) -> None:
    """Make this activity the attribution target for spans raised inside it."""
    _contexts[activity_id] = context


def unregister_activity(activity_id: str) -> None:
    """Drop every trace of a finished activity.

    A context left registered attributes the *next* activity's spans to this
    one; a stale approval flag suppresses enforcement on an unrelated call.
    """
    _contexts.pop(activity_id, None)
    _approved.discard(activity_id)
    _aborts.pop(activity_id, None)


def activity_context(activity_id: str) -> dict[str, Any] | None:
    return _contexts.get(activity_id)


def current_activity() -> str | None:
    return _current_activity.get()


@contextmanager
def run_with_activity(activity_id: str):
    """Scope the current activity for the duration of a call."""
    token = _current_activity.set(activity_id)
    try:
        yield
    finally:
        _current_activity.reset(token)


# ── approval ────────────────────────────────────────────────────────


def mark_activity_approved(activity_id: str) -> None:
    _approved.add(activity_id)


def is_activity_approved(activity_id: str) -> bool:
    return activity_id in _approved


# ── abort ───────────────────────────────────────────────────────────


def set_activity_abort(activity_id: str, reason: str) -> None:
    _aborts[activity_id] = reason


def has_activity_abort(activity_id: str) -> bool:
    return activity_id in _aborts


def activity_abort_reason(activity_id: str) -> str | None:
    return _aborts.get(activity_id)


def clear_activity_abort(activity_id: str) -> None:
    _aborts.pop(activity_id, None)


def reset_registry() -> None:
    """Test hook."""
    _contexts.clear()
    _approved.clear()
    _aborts.clear()


__all__ = [
    "activity_abort_reason",
    "activity_context",
    "clear_activity_abort",
    "current_activity",
    "is_activity_approved",
    "mark_activity_approved",
    "register_activity",
    "reset_registry",
    "run_with_activity",
    "set_activity_abort",
    "unregister_activity",
]
