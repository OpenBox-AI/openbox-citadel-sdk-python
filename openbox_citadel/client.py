"""HTTP client for Core's governance endpoints.

Its own client rather than the base SDK's, for one reason: the base
`ApprovalPollParams` carries only `(workflow_id, run_id, activity_id)` and has
nowhere to put the approval id Core returns on a `require_approval` verdict.
Polling without it asks Core about a key it is not tracking the approval under.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from openbox_langgraph.client import build_auth_headers
from openbox_langgraph.config import parse_optional_agent_identity_config

from openbox_citadel.types import to_server_event_type

logger = logging.getLogger("openbox_citadel.client")


class OpenBoxAuthError(Exception):
    """The credential was rejected — 401 or 403.

    Its own type because it is the one API failure that must not degrade to
    "carry on ungoverned" under `fail_open`.
    """

EVALUATE_PATH = "/api/v1/governance/evaluate"
APPROVAL_PATH = "/api/v1/governance/approval"


class GovernanceClient:
    """Async client. One per process — it holds a pooled connection."""

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        timeout: float = 30.0,
        on_api_error: str = "fail_open",
        fail_hard_on_auth_error: bool = True,
        agent_did: str | None = None,
        agent_private_key: str | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._on_api_error = on_api_error
        self._fail_hard_on_auth_error = fail_hard_on_auth_error
        self._client: httpx.AsyncClient | None = None
        self._identity = parse_optional_agent_identity_config(
            did=agent_did, private_key=agent_private_key
        )

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # Read timeout is generous because Core's own server-side timeout on
            # session close is 30s; a shorter one here abandons the request
            # before Core answers and loses the terminal event. Connect stays
            # tight — an unreachable Core should fail fast, not hang.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=5.0, read=max(self._timeout, 60.0))
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        import json

        body = json.dumps(payload, default=str).encode()
        headers = build_auth_headers(
            self._api_key,
            method="POST",
            pathname=path,
            body=body,
            agent_identity=self._identity,
        )
        response = await self._http().post(f"{self._api_url}{path}", content=body, headers=headers)
        if response.status_code in (401, 403):
            # Distinct from a transport failure: this will not come right on
            # retry, so it must not be swallowed by fail_open.
            raise OpenBoxAuthError(
                f"OpenBox rejected the credential ({response.status_code}) "
                f"on {path}: {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenBox {path} returned {response.status_code}: {response.text[:300]}")
        if not response.content:
            return None
        return response.json()

    async def evaluate(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Send one governance event. `None` when failing open on a transport error.

        The SDK-internal event name is mapped to the canonical wire name here,
        at the last possible moment, so logs and the sequencer keep the
        descriptive form while Core gets one of the six it accepts.
        """
        payload = {**event, "event_type": to_server_event_type(event.get("event_type", ""))}
        try:
            return await self._post(EVALUATE_PATH, payload)
        except OpenBoxAuthError:
            if self._fail_hard_on_auth_error:
                raise
            logger.error(
                "OpenBox credential rejected; continuing UNGOVERNED because "
                "fail_hard_on_auth_error is off"
            )
            return None
        except Exception:
            if self._on_api_error == "fail_closed":
                raise
            logger.warning(
                "governance %s failed; failing open", event.get("event_type"), exc_info=True
            )
            return None

    async def poll_approval(
        self,
        *,
        workflow_id: str,
        run_id: str,
        activity_id: str,
        approval_id: str | None = None,
    ) -> dict[str, Any] | None:
        """One HITL poll. `None` on a transport error so the caller can retry.

        When Core returned an approval id on the verdict, that id is the poll
        key — and it goes in all three fields, which is how the server
        addresses it. Only without one do we fall back to the run triple.
        """
        if approval_id:
            payload = {
                "workflow_id": approval_id,
                "run_id": approval_id,
                "activity_id": approval_id,
            }
        else:
            payload = {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "activity_id": activity_id,
            }

        try:
            data = await self._post(APPROVAL_PATH, payload)
        except OpenBoxAuthError:
            if self._fail_hard_on_auth_error:
                raise
            return None
        except Exception:
            if self._on_api_error == "fail_closed":
                raise
            return None
        if data is None:
            return None

        # Expiry is a client-side check — Core sends the expiration time, not an
        # `expired` flag.
        expiration = data.get("approval_expiration_time") or data.get("approvalExpirationTime")
        if isinstance(expiration, str) and expiration.strip():
            try:
                expires_at = datetime.fromisoformat(expiration)
            except ValueError:
                return data
            if expires_at < datetime.now(UTC):
                return {**data, "expired": True}
        return data


__all__ = ["GovernanceClient", "OpenBoxAuthError"]
