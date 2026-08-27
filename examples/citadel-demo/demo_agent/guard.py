"""D5 — the tool boundary. Citadel-shaped, written from scratch.

Reproduces the two properties the real engine's guard has, because they are what
the OpenBox integration has to compose with:

* **Bind-time and call-time, both.** Bind-time alone leaks a tool handle through
  state or a replayed checkpoint. Call-time alone pays tokens to show the model
  capabilities it cannot use.
* **A denial raises; it never returns a string.** A string goes back to the model
  as a tool result, which it may summarise, ignore or retry around. An access
  decision that reads to an LLM as a recoverable hint is not access control.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("demo.guard")


class ToolAccessDenied(PermissionError):
    """Raised when an agent invokes a tool it was not granted. Fails the turn."""


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Who is calling. `granted` is authoritative and never model-influenced."""

    agent_id: str
    source: str
    granted: frozenset[str]
    user_id: str | None = None
    tenant: str | None = None
    session_id: str | None = None

    def assert_granted(self, tool_name: str) -> None:
        if tool_name not in self.granted:
            logger.error("DENIED tool=%r agent=%r", tool_name, self.agent_id)
            raise ToolAccessDenied(
                f"agent {self.agent_id!r} (source={self.source}) is not granted "
                f"tool {tool_name!r}"
            )


def guarded(
    tool_name: str, ctx: ToolContext
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Re-check the grant on every invocation, before any work is done."""

    def decorate(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx.assert_granted(tool_name)
            return await fn(*args, **kwargs)

        return wrapper

    return decorate
