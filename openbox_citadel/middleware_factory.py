"""Single entry point for middleware creation."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from openbox_citadel.config import DEFAULT_APPROVAL_MAX_WAIT_SECONDS, GovernanceConfig
from openbox_citadel.middleware import (
    CORE_ENV_PREFIX,
    TASK_QUEUE,
    OpenBoxCitadelMiddleware,
    OpenBoxCitadelMiddlewareOptions,
)


def create_openbox_citadel_middleware(
    *,
    api_url: str | None = None,
    api_key: str | None = None,
    agent_did: str | None = None,
    agent_private_key: str | None = None,
    agent_name: str | None = None,
    session_id: str | None = None,
    governance_timeout: float = 30.0,
    on_api_error: str | None = None,
    validate: bool = True,
    deny_exc: Callable[[str], Exception] | None = None,
    halt_exc: Callable[[str, Any], Exception] | None = None,
    approval_max_wait_seconds: float | None = DEFAULT_APPROVAL_MAX_WAIT_SECONDS,
    config: GovernanceConfig | None = None,
    **options: Any,
) -> OpenBoxCitadelMiddleware:
    """Build the middleware for one Citadel deployment.

    Credentials fall back to `OPENBOX_*`, which in Citadel means Doppler
    (project `squidgy`) — never a `.env` file.
    """
    resolved_url = api_url or os.environ.get("OPENBOX_API_URL", "https://core.openbox.ai")
    resolved_key = api_key or os.environ.get("OPENBOX_API_KEY", "")
    if not resolved_key:
        raise ValueError(
            "OPENBOX_API_KEY is empty. Citadel reads secrets from Doppler "
            "(project 'squidgy') and no .env file — check the Doppler entry."
        )

    resolved_policy = on_api_error or os.environ.get("OPENBOX_ON_API_ERROR", "fail_open")
    if resolved_policy not in ("fail_open", "fail_closed"):
        raise ValueError(
            f"on_api_error must be 'fail_open' or 'fail_closed', got {resolved_policy!r}"
        )

    resolved_config = config or GovernanceConfig()
    resolved_config.task_queue = TASK_QUEUE
    resolved_config.on_api_error = resolved_policy
    if agent_name:
        resolved_config.agent_name = agent_name
    if session_id:
        resolved_config.session_id = session_id

    if validate:
        # The base normalizer: URL security (refuses non-localhost http://),
        # timeout coercion, API-key format, and the DID/private-key
        # both-or-neither rule. Raises OpenBoxConfigError on a bad config, which
        # is the point of asking — fail at construction, not mid-turn.
        from openbox_core.config import OpenBoxConfig

        OpenBoxConfig.resolve(
            env_prefix=CORE_ENV_PREFIX,
            api_url=resolved_url,
            api_key=resolved_key,
            timeout_seconds=governance_timeout,
            agent_did=agent_did or os.environ.get("OPENBOX_AGENT_DID"),
            agent_private_key=agent_private_key
            or os.environ.get("OPENBOX_AGENT_PRIVATE_KEY"),
            validate=True,
        )

    return OpenBoxCitadelMiddleware(
        OpenBoxCitadelMiddlewareOptions(
            api_url=resolved_url,
            api_key=resolved_key,
            agent_did=agent_did or os.environ.get("OPENBOX_AGENT_DID"),
            agent_private_key=agent_private_key
            or os.environ.get("OPENBOX_AGENT_PRIVATE_KEY"),
            governance_timeout=governance_timeout,
            on_api_error=resolved_policy,
            config=resolved_config,
            deny_exc=deny_exc,
            halt_exc=halt_exc,
            approval_max_wait_seconds=approval_max_wait_seconds,
            **options,
        )
    )


__all__ = ["create_openbox_citadel_middleware"]
