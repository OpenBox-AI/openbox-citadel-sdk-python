"""Tool declarations and the agent that owns them.

`ToolBacking` carries `kind`, `method` and `url` — which is what lets the OpenBox
SDK derive a correctly gated span per tool with no configuration at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

BackingKind = Literal["python_function", "http", "mcp", "rpc"]


@dataclass(frozen=True, slots=True)
class ToolBacking:
    kind: BackingKind
    method: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class ToolParam:
    type: str
    description: str = ""
    required: bool = False


@dataclass(frozen=True, slots=True)
class ToolDecl:
    name: str
    description: str
    backing: ToolBacking
    params: dict[str, ToolParam] = field(default_factory=dict)
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ResolvedAgent:
    agent_id: str
    name: str
    source: str
    system_prompt: str
    tools: list[ToolDecl]


SALES_AGENT = ResolvedAgent(
    agent_id="demo-sales-01",
    name="Squidgy Sales Assistant",
    source="config",
    system_prompt="You qualify inbound leads and prepare outreach.",
    tools=[
        ToolDecl(
            name="Web_Analysis",
            description="Summarise a company website.",
            backing=ToolBacking("http", "GET", "https://api.demo.local/analyse"),
            params={"url": ToolParam("string", "Company website", required=True)},
        ),
        ToolDecl(
            name="Apollo",
            description="Look up contacts at a company.",
            backing=ToolBacking("http", "POST", "https://api.apollo.io/v1/people/search"),
            params={"domain": ToolParam("string", "Company domain", required=True)},
        ),
        ToolDecl(
            name="Send_Invoice",
            description="Issue an invoice to a customer. Moves money.",
            backing=ToolBacking("http", "POST", "https://api.billing.local/invoices"),
            params={
                "customer": ToolParam("string", "Customer id", required=True),
                "amount_usd": ToolParam("number", "Amount", required=True),
            },
        ),
        ToolDecl(
            name="Delete_Account",
            description="Permanently delete a customer account.",
            backing=ToolBacking("http", "DELETE", "https://api.billing.local/accounts"),
            params={"customer": ToolParam("string", "Customer id", required=True)},
        ),
    ],
)

# The grant set. `Delete_Account` is DECLARED but NOT granted — it exists in the
# catalogue and this agent may not call it. That gap is what scenario 2 exercises.
GRANTS: dict[str, frozenset[str]] = {
    "demo-sales-01": frozenset({"Web_Analysis", "Apollo", "Send_Invoice"}),
}


def grants_for(agent: ResolvedAgent) -> frozenset[str]:
    return GRANTS.get(agent.agent_id, frozenset())
