"""Pull model metadata off a completion response.

`llm_model`, token counts and the completion text are first-class fields on an
LLM event. Without them a session shows that a model was called but not which
one, at what cost, or what it said.
"""

from __future__ import annotations

from typing import Any


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text_of(content: Any) -> str | None:
    """Completion text, flattening the list-of-parts shape providers also use."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return "".join(parts) if parts else None
    return None


def response_metadata(response: Any) -> dict[str, Any]:
    """Project an OpenAI/OpenRouter chat completion onto the event's LLM fields.

    Returns only what it actually found: `build_event` drops `None`s, so a
    provider that omits usage does not produce a row full of nulls.
    """
    if not isinstance(response, dict):
        return {}

    usage = response.get("usage") or {}
    input_tokens = _number(usage.get("prompt_tokens") or usage.get("input_tokens"))
    output_tokens = _number(usage.get("completion_tokens") or usage.get("output_tokens"))
    total_tokens = _number(usage.get("total_tokens"))
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)

    choices = response.get("choices") or []
    message = (choices[0] or {}).get("message", {}) if choices else {}
    tool_calls = message.get("tool_calls") or []

    return {
        "llm_model": response.get("model"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "completion": _text_of(message.get("content")),
        "has_tool_calls": bool(tool_calls) or None,
        "finish_reason": (choices[0] or {}).get("finish_reason") if choices else None,
    }


def last_user_message(messages: list[Any]) -> str | None:
    """The LAST human message, not all of them.

    Joining every human message concatenates the chat history loaded from memory
    into one blob, so a policy matching on prompt content reads prior turns as if
    the user had just said them.
    """
    for message in reversed(messages or []):
        role = (
            message.get("role")
            if isinstance(message, dict)
            else getattr(message, "type", None) or getattr(message, "role", None)
        )
        if role in ("user", "human"):
            content = (
                message.get("content")
                if isinstance(message, dict)
                else getattr(message, "content", None)
            )
            return _text_of(content) or (content if isinstance(content, str) else None)
    return None


def has_human_turn(messages: list[Any]) -> bool:
    """Whether the conversation contains a human turn at all.

    An empty or multimodal first turn must still be governed, not silently
    skipped — so this asks whether a human spoke, not whether text was extracted.
    """
    for message in messages or []:
        role = (
            message.get("role")
            if isinstance(message, dict)
            else getattr(message, "type", None) or getattr(message, "role", None)
        )
        if role in ("user", "human"):
            return True
    return False


__all__ = ["has_human_turn", "last_user_message", "response_metadata"]
