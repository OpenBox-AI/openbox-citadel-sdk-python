"""Configuration for one governed deployment."""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_APPROVAL_MAX_WAIT_SECONDS = 75.0


@dataclass
class HITLConfig:
    enabled: bool = True
    poll_interval_ms: int = 5_000
    skip_tool_types: set[str] = field(default_factory=set)


@dataclass
class GovernanceConfig:
    """Resolved governance configuration for one deployment."""

    on_api_error: str = "fail_open"
    api_timeout: float = 30.0
    send_tool_start_event: bool = True
    send_tool_end_event: bool = True
    fail_hard_on_auth_error: bool = True
    """Whether a rejected credential fails the run regardless of `on_api_error`.

    Default on, and the default is the safe one. `fail_open` exists for the case
    where OpenBox is *unreachable* — a network blip should not take Citadel down
    with it. A 401 is a different fact: the key is revoked, expired or wrong, and
    it will stay wrong on retry. Treating it as a transient error means a revoked
    key silently downgrades every agent to ungoverned, which is the one outcome a
    governance layer must never produce quietly.

    Turn it off only with a reason — a staged key rotation where a window of
    ungoverned execution is preferred to an outage, say — and know that is the
    trade you are making.
    """

    send_signal_events: bool = True
    """Whether the user's message is emitted as `SignalReceived`.

    Worth understanding before turning this off, and before leaving it on.

    A signal gives Core a stated user intent, which is what its goal-alignment
    step (`AGECheckActivity`) compares the session's activities against. That
    check needs the Guardrails service. With Guardrails unreachable it blocks for
    30s and Core's own `GovernanceEventWorkflow` hits its StartToClose timeout —
    so `WorkflowCompleted` returns 500 and is never stored, and the session shows
    as failed or in-progress with no terminal event even though the run was fine.

    With Guardrails up, leave this on: prompt-level policy and drift detection
    both depend on it. Without Guardrails, turn it off — the alternative is
    every session ending in a phantom failure.
    """

    send_llm_start_event: bool = True
    send_llm_end_event: bool = True
    skip_tool_types: set[str] = field(default_factory=set)
    hitl: HITLConfig = field(default_factory=HITLConfig)
    session_id: str | None = None
    agent_name: str | None = None
    task_queue: str = "citadel"
    tool_type_map: dict[str, str] = field(default_factory=dict)
    """Tool name -> `http` | `database` | `builtin` | `a2a`.

    Only needed for `python_function` backings. An `http`-backed Citadel tool
    already declares its own `kind`, `method` and `url`.
    """


__all__ = ["DEFAULT_APPROVAL_MAX_WAIT_SECONDS", "GovernanceConfig", "HITLConfig"]
