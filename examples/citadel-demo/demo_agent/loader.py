"""Bind an agent's granted tools. The single site where every tool is built.

The composition order here is the whole security argument:

    call = impl                            # the tool itself
    call = mw.govern(decl, call)           # OpenBox — inner
    call = guarded(name, ctx)(call)        # D5      — OUTER, runs first

D5 outermost, for two reasons. It is the compensating control for a tool
boundary that is a module boundary rather than a network one, so it must never
become contingent on an external service being reachable. And a call D5 will
refuse should never be sent to OpenBox at all — otherwise a `require_approval`
verdict pages a human to approve an action that cannot execute.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


from demo_agent.guard import ToolContext, guarded
from demo_agent.registry import ResolvedAgent, ToolDecl
from demo_agent.tools import IMPLS

logger = logging.getLogger("demo.loader")


def _govern(decl: ToolDecl, call: Any, mw: Any) -> Any:
    """Wrap in OpenBox governance, inside the D5 guard applied by the caller."""
    if mw is None:
        return call
    return mw.govern(decl, call)


@dataclass(slots=True)
class BoundTool:
    name: str
    description: str
    decl: ToolDecl
    call: Callable[..., Awaitable[Any]]


def bind(agent: ResolvedAgent, ctx: ToolContext, *, mw: Any = None) -> list[BoundTool]:
    """Build the granted tools. A declared-but-ungranted tool is simply not built."""
    bound: list[BoundTool] = []
    for decl in agent.tools:
        if not decl.enabled:
            continue
        if decl.name not in ctx.granted:
            # Bind-time gate. This tool never reaches the model at all.
            continue
        bound.append(
            BoundTool(
                name=decl.name,
                description=decl.description,
                decl=decl,
                call=guarded(decl.name, ctx)(_govern(decl, IMPLS[decl.name], mw)),
            )
        )
    logger.info("bound %d/%d tools for %s", len(bound), len(agent.tools), agent.agent_id)
    return bound
