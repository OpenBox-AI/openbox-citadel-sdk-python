"""SignalReceived, workflow output, memory ops, and LLM metadata."""

from __future__ import annotations

import pytest

from openbox_citadel.metadata import has_human_turn, last_user_message, response_metadata
from tests.doubles import Denied, FakeClient


async def _tool(args, ctx):
    return "ok"


# ── SignalReceived ──────────────────────────────────────────────────


async def test_signal_is_emitted_and_governed(mw, client: FakeClient) -> None:
    """Citadel's trigger is the user's message, so this is where a prompt-level
    policy gets its say before any model call is made."""
    await mw.before_turn(goal="g")
    await mw.signal("user_prompt", ["invoice cus_77 for $18,500"])

    signals = client.of_type("SignalReceived")
    assert len(signals) == 1
    assert signals[0]["signal_name"] == "user_prompt"
    assert signals[0]["signal_args"] == ["invoice cus_77 for $18,500"]
    assert signals[0]["activity_id"] == f"{mw._run_id}-sig"
    assert signals[0]["activity_type"] == "user_prompt"
    # `activity_input` must NOT be present: it makes Core run input-stage
    # processing on a workflow-level event and the session never closes.
    assert "activity_input" not in signals[0]


async def test_blocked_signal_stops_the_turn(mw, client: FakeClient) -> None:
    """A blocked prompt must never reach the model."""
    client.verdicts = {"user_prompt": "block"}
    await mw.before_turn(goal="g")
    with pytest.raises(Denied):
        await mw.signal("user_prompt", ["do something forbidden"])


async def test_blocked_signal_closes_its_activity(mw, client: FakeClient) -> None:
    client.verdicts = {"user_prompt": "block"}
    await mw.before_turn(goal="g")
    with pytest.raises(Denied):
        await mw.signal("user_prompt", ["x"])
    closures = [
        e for e in client.of_type("ActivityCompleted") if e["activity_type"] == "user_prompt"
    ]
    assert len(closures) == 1, "a refused signal must not dangle"


async def test_signal_requiring_approval_polls(mw, client: FakeClient) -> None:
    client.verdicts = {"user_prompt": "require_approval"}
    await mw.before_turn(goal="g")
    await mw.signal("user_prompt", ["x"])
    assert len(client.polls) == 1


# ── workflow anchor + output ────────────────────────────────────────


async def test_workflow_events_carry_an_activity_anchor(mw, client: FakeClient) -> None:
    """A bare marker gives the workflow no node of its own on the timeline."""
    await mw.before_turn(goal="g")
    started = client.of_type("WorkflowStarted")[0]
    assert started["activity_id"] == f"{mw._run_id}-wf"
    assert started["activity_type"] == "chat"


async def test_final_answer_is_sent_as_activity_output(mw, client: FakeClient) -> None:
    """Core binds `activity_output`; its payload struct has no `workflow_output`,
    so an SDK sending only that has its final answer dropped at unmarshal."""
    await mw.before_turn(goal="g")
    await mw.after_turn(output="Acme is a logistics SaaS.")

    completed = client.of_type("WorkflowCompleted")[0]
    assert completed["activity_output"] == {"result": "Acme is a logistics SaaS."}
    # Sent alongside, for anything reading the raw event stream.
    assert completed["workflow_output"] == {"result": "Acme is a logistics SaaS."}


async def test_failed_turn_carries_output_and_error(mw, client: FakeClient) -> None:
    await mw.before_turn(goal="g")
    await mw.after_turn(status="failed", error=ValueError("boom"), output="partial")
    failed = client.of_type("WorkflowFailed")[0]
    assert failed["error"]["type"] == "ValueError"
    assert failed["activity_output"] == {"result": "partial"}


# ── memory ops ──────────────────────────────────────────────────────


async def test_arbitrary_awaitable_is_a_governed_activity(mw, client: FakeClient) -> None:
    """The generic escape hatch. `activity_type` is the caller's vocabulary —
    Citadel happens to use it for its history read, but the SDK does not know
    that a "memory op" is a thing."""

    async def load():
        return [{"role": "user", "content": "hi"}]

    await mw.before_turn(goal="g")
    result = await mw.govern_activity("load_memory", load())
    assert result == [{"role": "user", "content": "hi"}]

    started = [e for e in client.of_type("ActivityStarted") if e["activity_type"] == "load_memory"]
    done = [e for e in client.of_type("ActivityCompleted") if e["activity_type"] == "load_memory"]
    assert len(started) == len(done) == 1
    assert started[0]["activity_id"] == done[0]["activity_id"]


async def test_blocked_memory_op_raises_and_closes(mw, client: FakeClient) -> None:
    client.verdicts = {"save_context": "block"}

    async def save():
        raise AssertionError("must not run")

    coro = save()
    await mw.before_turn(goal="g")
    with pytest.raises(Denied):
        await mw.govern_activity("save_context", coro)
    # A blocked op never awaits its coroutine — that is the point, but Python
    # warns about it, so close it explicitly.
    coro.close()
    done = [e for e in client.of_type("ActivityCompleted") if e["activity_type"] == "save_context"]
    assert len(done) == 1


async def test_failing_memory_op_completes_with_error(mw, client: FakeClient) -> None:
    async def save():
        raise RuntimeError("neon down")

    await mw.before_turn(goal="g")
    with pytest.raises(RuntimeError):
        await mw.govern_activity("save_context", save())
    done = [e for e in client.of_type("ActivityCompleted") if e["activity_type"] == "save_context"]
    assert done[0]["status"] == "failed"


# ── LLM metadata ────────────────────────────────────────────────────


def test_response_metadata_extracts_model_tokens_and_text() -> None:
    meta = response_metadata({
        "model": "openai/gpt-4o-mini",
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        "choices": [{"message": {"content": "Acme is logistics."}, "finish_reason": "stop"}],
    })
    assert meta["llm_model"] == "openai/gpt-4o-mini"
    assert (meta["input_tokens"], meta["output_tokens"], meta["total_tokens"]) == (120, 30, 150)
    assert meta["completion"] == "Acme is logistics."
    assert meta["finish_reason"] == "stop"


def test_response_metadata_detects_tool_calls() -> None:
    meta = response_metadata({
        "choices": [{"message": {"tool_calls": [{"function": {"name": "Apollo"}}]}}],
    })
    assert meta["has_tool_calls"] is True


def test_response_metadata_survives_a_missing_usage_block() -> None:
    meta = response_metadata({"model": "m", "choices": []})
    assert meta["input_tokens"] is None and meta["total_tokens"] is None


def test_response_metadata_flattens_list_content() -> None:
    meta = response_metadata({
        "choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}],
    })
    assert meta["completion"] == "ab"


async def test_llm_completion_carries_metadata(mw, client: FakeClient) -> None:
    async def call():
        return {
            "model": "openai/gpt-4o-mini",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "choices": [{"message": {"content": "hi"}}],
        }

    await mw.before_turn(goal="g")
    await mw.govern_call(call(), model="openai/gpt-4o-mini", prompt="hello")
    done = client.of_type("LLMCompleted")[0]
    assert done["total_tokens"] == 15
    assert done["completion"] == "hi"


# ── message helpers ─────────────────────────────────────────────────


def test_last_user_message_ignores_history() -> None:
    """Joining every human message would feed prior turns to a policy as if the
    user had just said them."""
    messages = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "the actual request"},
    ]
    assert last_user_message(messages) == "the actual request"


def test_has_human_turn_is_about_presence_not_text() -> None:
    """An empty or multimodal first turn must still be governed."""
    assert has_human_turn([{"role": "user", "content": ""}]) is True
    assert has_human_turn([{"role": "system", "content": "x"}]) is False


async def test_terminal_event_never_fails_the_turn(mw, client: FakeClient) -> None:
    """The reply is already delivered by the time the terminal event fires, so a
    governance write failing there must not surface as a turn failure.

    `fail_closed` exists to stop actions before they happen, not to punish a run
    for a slow write after it finished.
    """
    await mw.before_turn(goal="g")
    client.fail = True
    mw._config.on_api_error = "fail_closed"
    await mw.after_turn(output="delivered")  # must not raise


async def test_signals_can_be_switched_off(mw, client: FakeClient) -> None:
    """Needed where Guardrails is unreachable: a signal makes Core attempt
    goal-alignment, which blocks 30s and loses the terminal event entirely."""
    mw._config.send_signal_events = False
    await mw.before_turn(goal="g")
    await mw.signal("user_prompt", ["hi"])
    assert client.of_type("SignalReceived") == []


async def test_llm_completion_text_lands_in_activity_output(mw, client: FakeClient) -> None:
    """Core persists `activity_output` into the event's `output` column. The
    loose `completion` field has no column and is dropped, so a session records
    that a model ran but not what it said."""

    async def call():
        return {"model": "m", "choices": [{"message": {"content": "the answer"}}]}

    await mw.before_turn(goal="g")
    await mw.govern_call(call(), model="m", prompt="q")
    done = client.of_type("LLMCompleted")[0]
    assert done["activity_output"] == {"result": "the answer"}


async def test_tool_only_turn_carries_no_output(mw, client: FakeClient) -> None:
    """A turn that only called tools produced no text — no output is correct."""

    async def call():
        return {"model": "m", "choices": [{"message": {"tool_calls": [{"function": {}}]}}]}

    await mw.before_turn(goal="g")
    await mw.govern_call(call(), model="m", prompt="q")
    assert "activity_output" not in client.of_type("LLMCompleted")[0]
