"""Tool implementations. Each makes a real outbound HTTP call.

That matters for spans: OTel instrumentation captures actual requests, so a tool
that simulates its work produces nothing for a behavior rule to match on.

`SIDE_EFFECTS` is the demo's proof that a denial performs no work. Asserting only
that an exception surfaced would pass even if the request had already gone out.
"""

from __future__ import annotations

from typing import Any

import httpx

from upstream import BASE

SIDE_EFFECTS: list[dict[str, Any]] = []


async def _call(method: str, path: str, tool: str, args: dict[str, Any]) -> Any:
    SIDE_EFFECTS.append({"tool": tool, "args": args})
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.request(method, f"{BASE}{path}", json=args)
        return response.json()


async def web_analysis(args: dict[str, Any], ctx: Any) -> Any:
    return await _call("GET", "/analyse", "Web_Analysis", args)


async def apollo(args: dict[str, Any], ctx: Any) -> Any:
    return await _call("POST", "/people/search", "Apollo", args)


async def send_invoice(args: dict[str, Any], ctx: Any) -> Any:
    return await _call("POST", "/invoices", "Send_Invoice", args)


async def delete_account(args: dict[str, Any], ctx: Any) -> Any:
    return await _call("DELETE", "/accounts", "Delete_Account", args)


IMPLS = {
    "Web_Analysis": web_analysis,
    "Apollo": apollo,
    "Send_Invoice": send_invoice,
    "Delete_Account": delete_account,
}
