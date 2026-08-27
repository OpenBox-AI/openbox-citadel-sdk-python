"""The turn: a real model call, a real tool loop.

The model chooses. Each iteration is one `llm_call` activity carrying a genuine
`llm_completion` span, and every tool the model picks runs through the governed,
D5-guarded handle the loader built.

**Only granted tools are offered.** The schema list is derived from the bound
tools, so a tool the agent was not granted is not merely refused at call time —
the model never learns it exists. That is Citadel's bind-time gate, and it is
why the ungranted-tool scenario cannot be forced by prompting.

Tool *results* are never streamed to the user; they re-enter the model as
messages and the model decides what to say about them.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("demo.turn")

PRIMARY_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
"""Citadel's gateway. Core classifies a span as an LLM completion by the URL's
domain, so an internal hostname here silently demotes it to plain HTTP."""

MAX_ITERATIONS = 4

SYSTEM_PROMPT = (
    "You are a B2B sales assistant. Use the tools available to you to complete "
    "the user's request. Call tools when they are relevant. Keep replies to one "
    "or two short sentences."
)


@dataclass
class Turn:
    """One request to the agent."""

    prompt: str


def tool_schema(decl: Any) -> dict[str, Any]:
    """Project a ToolDecl onto the JSON Schema the model sees.

    Only DECLARED parameters appear, and `additionalProperties: false` says so:
    the argument list is the model's entire influence over a tool call.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in (decl.params or {}).items():
        spec: dict[str, Any] = {"type": param.type}
        if param.description:
            spec["description"] = param.description
        properties[name] = spec
        if param.required:
            required.append(name)
    return {
        "type": "function",
        "function": {
            "name": decl.name,
            "description": decl.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


async def _complete(messages: list[dict], schemas: list[dict]) -> dict:
    """One OpenRouter completion. Real HTTP, so the span is real."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY unset — no model call, and so no span")

    body: dict[str, Any] = {
        "model": PRIMARY_MODEL,
        "messages": messages,
        "max_tokens": 300,
    }
    if schemas:
        body["tools"] = schemas
        body["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(
            OPENROUTER_URL,
            json=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()


async def run_turn(
    turn: Turn,
    tools: list[Any],
    ctx: Any,
    *,
    gov: Any = None,
    on_text: Any = None,
    on_tool: Any = None,
) -> str:
    """Run the agent loop until the model stops calling tools."""
    by_name = {tool.name: tool for tool in tools}
    schemas = [tool_schema(tool.decl) for tool in tools]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": turn.prompt},
    ]

    for _iteration in range(MAX_ITERATIONS):
        call = _complete(messages, schemas)
        if gov is not None:
            # One `llm_call` activity per iteration. Authorization completes
            # before the request goes out, so a refusal costs no tokens.
            call = gov.govern_call(call, model=PRIMARY_MODEL, prompt=turn.prompt)
        payload = await call

        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content")
        tool_calls = message.get("tool_calls") or []

        if text and on_text:
            on_text(text)

        if not tool_calls:
            return text or ""

        messages.append(message)

        for requested in tool_calls:
            fn = requested.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}

            tool = by_name.get(name)
            if tool is None:
                # The model named something outside its bound set. Nothing was
                # attempted, so this is not a denial — tell it and let it correct.
                result: Any = f"Error: no tool named {name!r} is available."
                if on_tool:
                    on_tool(name, "unbound", result)
            else:
                result = await tool.call(args, ctx)
                if on_tool:
                    on_tool(name, "ok", result)

            messages.append({
                "role": "tool",
                "tool_call_id": requested.get("id") or name,
                "content": json.dumps(result, default=str)[:4000],
            })

    logger.warning("tool loop hit max_iterations=%d without a final answer", MAX_ITERATIONS)
    return ""


__all__ = ["MAX_ITERATIONS", "OPENROUTER_URL", "PRIMARY_MODEL", "Turn", "run_turn", "tool_schema"]
