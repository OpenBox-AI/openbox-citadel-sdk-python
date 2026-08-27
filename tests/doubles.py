"""Test doubles. Kept out of conftest so pytest's double-import of `conftest`
cannot make `Denied` two distinct classes."""

from __future__ import annotations

from typing import Any


class Denied(Exception):
    """Stands in for Citadel's `ToolAccessDenied`."""


class FakeClient:
    """Records events; returns scripted verdicts and approval responses."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.verdicts: dict[str, Any] = {}
        self.fail = False
        self.polls: list[dict[str, Any]] = []
        # A list consumed one entry per poll — lets a test script "pending,
        # pending, approved" the way a real human decision arrives.
        self.approval_script: list[dict[str, Any]] = [{"arm": "allow"}]
        self.redact: Any = None

    async def evaluate(self, event: dict[str, Any]) -> Any:
        self.events.append(event)
        if self.fail:
            raise RuntimeError("network down")
        key = event.get("activity_type") or event.get("event_type")
        scripted = self.verdicts.get(key)
        response: dict[str, Any] = dict(scripted) if isinstance(scripted, dict) else {
            "arm": scripted or "allow"
        }
        response.setdefault("reason", "test")
        if self.redact is not None and event.get("event_type") == "ActivityStarted":
            response["guardrails_result"] = {
                "input_type": "activity_input",
                "redacted_input": self.redact,
                "validation_passed": True,
            }
        return response

    async def poll_approval(self, **kwargs: Any) -> Any:
        self.polls.append(kwargs)
        if not self.approval_script:
            return {}
        if len(self.approval_script) == 1:
            return self.approval_script[0]
        return self.approval_script.pop(0)

    async def close(self) -> None:
        return None

    @property
    def types(self) -> list[str]:
        return [e["event_type"] for e in self.events]

    @property
    def trace(self) -> list[tuple[str, str | None]]:
        return [(e["event_type"], e.get("activity_type")) for e in self.events]

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event_type"] == event_type]
