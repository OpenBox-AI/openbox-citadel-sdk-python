"""Corrections to the file spans the hook layer emits.

Two things reach Core wrong, and both are visible on a session's `export_report`
activity.

**A write is recorded as a read.** Core classifies a file span from the `name`
it arrives under. It knows `file.read`, `file.write`, `file.open` and
`file.delete`; anything else falls to a default of `file_read`. The hook layer
also emits `file.writelines`, `file.readline` and `file.readlines`, so writing
lines to disk is stored as a read — backwards for anything reasoning about what
an agent sent out. We send the canonical name Core understands and keep the
precise operation in `file.operation`, which is preserved: file spans are the
ones whose attributes Core actually stores.

**The stages do not read as a sequence.** Every operation sends a `started` and
a `completed` entry, but no duration is ever passed for file work, and
`_build_file_span_data` derives `start_time` from one: with no duration it
stamps `now` on both entries. So a pair arrives as two same-instant, zero-length
events instead of one operation with a start and an end, and nothing downstream
can order the operations inside an activity or say how long a write took. We
time each operation and pass a real `duration_ms` on completion, which is also
what makes the hook layer backdate `start_time` to when the operation actually
began.

This is a shim over `openbox_langgraph.file_governance_hooks`, applied from our
layer because that is the layer we own. It wraps one function, changes no
control flow, and is best-effort: if the hook layer moves, file spans go back to
being merely wrong rather than governance breaking. Delete it once the hook
package emits canonical names and durations itself.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("openbox_citadel.file_spans")

_CANONICAL_NAME = {
    "read": "file.read",
    "readline": "file.read",
    "readlines": "file.read",
    "write": "file.write",
    "writelines": "file.write",
    "open": "file.open",
    "close": "file.open",  # the close entry completes the open span
    "delete": "file.delete",
}

_STARTED_AT: dict[tuple[str, str], float] = {}
"""(span_id, operation) -> perf_counter at the `started` entry.

Keyed by span id as well as operation because one file object can run the same
operation many times, and two files can be open at once. Bounded below.
"""

_MAX_TRACKED = 4096

_installed = False


def _canonical(operation: str, fallback: str) -> str:
    if operation in _CANONICAL_NAME:
        return _CANONICAL_NAME[operation]
    # An operation we have not seen. Classify by the word it carries rather than
    # letting a write default to a read.
    lowered = operation.lower()
    if "write" in lowered or "trunc" in lowered:
        return "file.write"
    if "read" in lowered:
        return "file.read"
    return fallback


def install_file_span_corrections() -> bool:
    """Wrap the hook layer's file span builder. Idempotent; safe to call often.

    Mostly historical now. File hooks moved into `openbox_core`, whose builder
    derives the span name from the open mode (`_file_span_name`) and so already
    files a write as `file.write` — the misclassification this module existed to
    fix is gone upstream. What did NOT come across is per-operation timing: core
    sends no duration, so a started/completed pair still lands as two
    same-instant events.

    When the legacy module is absent this reports it and installs nothing,
    rather than returning a success that corrects a builder nothing calls.
    """
    global _installed
    if _installed:
        return True
    try:
        from openbox_langgraph import file_governance_hooks as hooks
    except Exception:  # noqa: BLE001 — hook layer absent or moved
        logger.info(
            "file span corrections not installed: the legacy hook builder is gone. "
            "Canonical naming is handled by openbox_core; per-operation duration "
            "is not sent by it, so file start/completion pairs share a timestamp."
        )
        return False

    original = getattr(hooks, "_build_file_span_data", None)
    if original is None or getattr(original, "_openbox_corrected", False):
        _installed = original is not None
        return _installed

    def corrected(
        span: Any,
        file_path: str,
        file_mode: str,
        operation: str,
        stage: str,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        # Turn the recorded start into a duration, which is what makes the
        # builder backdate start_time so the pair lands as one ordered
        # operation. The start itself is recorded in the evaluate wrapper below.
        if stage == "completed" and kwargs.get("duration_ms") is None:
            span_id = _span_id_of(span)
            # `close` completes the span `open` started, so it reads that key:
            # the pair's duration is how long the file was held open.
            began = _STARTED_AT.pop(
                (span_id, "open" if operation == "close" else operation), None
            )
            if began is not None:
                kwargs["duration_ms"] = (time.perf_counter() - began) * 1000

        data = original(span, file_path, file_mode, operation, stage, *args, **kwargs)

        try:
            fallback = data.get("name") or f"file.{operation}"
            data["name"] = _canonical(operation, fallback)
            attrs = data.get("attributes")
            if not isinstance(attrs, dict):
                attrs = {}
                data["attributes"] = attrs
            # The granular operation is the thing a reviewer wants; it just must
            # not be the field Core classifies from.
            attrs.setdefault("file.operation", operation)
            attrs.setdefault("file.path", file_path)
            if file_mode:
                attrs.setdefault("file.mode", file_mode)
        except Exception:  # noqa: BLE001 — never fail the file operation
            logger.debug("file span correction failed; sending as built", exc_info=True)
        return data

    corrected._openbox_corrected = True  # type: ignore[attr-defined]
    hooks._build_file_span_data = corrected
    _install_start_clock(hooks)
    _installed = True
    logger.info("file span corrections installed (canonical names + operation timing)")
    return True


def _install_start_clock(hooks: Any) -> None:
    """Start the per-operation clock when a `started` evaluation returns.

    The evaluation is a blocking HTTP call to Core. Timing from the moment the
    payload is *built* therefore measures the authorize round trip plus the file
    work, and the round trip dominates by three orders of magnitude. Timing from
    the moment it returns measures the operation.

    Wraps the shared hook entry point, so it filters to file operations and
    leaves HTTP and DB spans — which pass their own durations — untouched.
    """
    gov = getattr(hooks, "_hook_gov", None)
    original = getattr(gov, "evaluate_sync", None) if gov is not None else None
    if original is None or getattr(original, "_openbox_clocked", False):
        return

    def timed(span: Any, identifier: Any = None, span_data: Any = None, *args: Any, **kwargs: Any):
        result = original(span, identifier, span_data, *args, **kwargs)
        try:
            if (
                isinstance(span_data, dict)
                and span_data.get("hook_type") == "file_operation"
                and span_data.get("stage") == "started"
                and len(_STARTED_AT) < _MAX_TRACKED
            ):
                operation = span_data.get("file_operation")
                if operation:
                    _STARTED_AT[(_span_id_of(span), operation)] = time.perf_counter()
        except Exception:  # noqa: BLE001 — timing must never break a file call
            logger.debug("file span clock failed", exc_info=True)
        return result

    timed._openbox_clocked = True  # type: ignore[attr-defined]
    gov.evaluate_sync = timed


def _span_id_of(span: Any) -> str:
    """A stable key for one span, without assuming the OTel API is reachable."""
    try:
        ctx = span.get_span_context()
        return format(ctx.span_id, "016x")
    except Exception:  # noqa: BLE001 — a mock, a NonRecordingSpan, or None
        return str(id(span))


__all__ = ["install_file_span_corrections"]
