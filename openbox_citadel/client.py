"""HTTP client for Core's governance endpoints.

Its own client rather than the base SDK's `GovernanceGate`, for one reason: the
base `ApprovalPollParams` carries only `(workflow_id, run_id, activity_id)` and
has nowhere to put the approval id Core returns on a `require_approval` verdict.
Polling without it asks Core about a key it is not tracking the approval under.

Transport and authentication are the base `EvaluationClient`'s, though. Core now
gates every runtime route on the agent's identity method, and the method picks
the route family:

    legacy_unsigned, openbox_did  ->  /api/v1   (API key, + DID signature)
    okta_ai_agent                 ->  /api/v2   (API key + RS256 assertion)
    keycloak_workload             ->  /api/v3   (API key + workload token)

A request on the wrong family is refused with `method_endpoint_mismatch`, so
hard-coding v1 here broke every agent the dashboard now creates with a workload
identity. The base client already owns that selection, the Keycloak token
exchange and its refresh; duplicating them would mean two implementations of a
security contract drifting apart.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from openbox_core.client import EvaluationClient
from openbox_core.errors import GovernanceAPIError, OpenBoxNetworkError
from openbox_core.errors import OpenBoxAuthError as CoreAuthError
from openbox_core.identity import AgentIdentity

from openbox_citadel.types import to_server_event_type

logger = logging.getLogger("openbox_citadel.client")

SDK_ENGINE = "citadel"
SDK_LANGUAGE = "python"
SDK_VERSION = "0.2.0"


def _optional_identity(did: str | None, private_key: str | None) -> AgentIdentity | None:
    """Both or neither — one alone is a misconfiguration, not unsigned mode.

    Returning `None` when only the DID is set would silently downgrade to
    bare-Bearer at the trust boundary, which is the failure this refuses.
    """
    did = (did or "").strip() or None
    private_key = (private_key or "").strip() or None
    if did is None and private_key is None:
        return None
    if did is None or private_key is None:
        raise ValueError(
            "Both OPENBOX_AGENT_DID and OPENBOX_AGENT_PRIVATE_KEY are required "
            "when enabling OpenBox agent identity signing."
        )
    return AgentIdentity.from_private_key(did, private_key)


def _pem(value: str | None) -> str | None:
    """A PEM from an env var, with literal `\\n` restored to newlines.

    The dashboard hands the workload key out JSON-quoted on one line, and most
    secret stores keep it that way.
    """
    value = (value or "").strip().strip('"') or None
    if value is not None and "\\n" in value:
        value = value.replace("\\n", "\n").strip()
    return value


class OpenBoxAuthError(Exception):
    """The credential was rejected — 401 or 403.

    Its own type because it is the one API failure that must not degrade to
    "carry on ungoverned" under `fail_open`.
    """


class _CitadelEvaluationClient(EvaluationClient):
    """The base client with Citadel's timeouts.

    Read is generous because Core's own server-side timeout on session close is
    30s; a shorter one here abandons the request before Core answers and loses
    the terminal event. Connect stays tight — an unreachable Core should fail
    fast, not hang. The base client takes a single scalar timeout, so this is
    the one seam overridden.
    """

    def _async(self) -> Any:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=5.0, read=max(self._timeout, 60.0)),
                transport=self._async_transport,
            )
        return self._async_client


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
        workload_private_key: str | None = None,
        okta_agent_private_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._on_api_error = on_api_error
        self._fail_hard_on_auth_error = fail_hard_on_auth_error
        identity = _optional_identity(agent_did, agent_private_key)
        okta_key = _pem(okta_agent_private_key)
        if identity is not None and okta_key is not None:
            raise ValueError(
                "OPENBOX_AGENT_DID/OPENBOX_AGENT_PRIVATE_KEY and "
                "OPENBOX_OKTA_AGENT_PRIVATE_KEY are two different agent identities; "
                "set the one the dashboard issued for this agent."
            )
        # A workload key composes with either: while Core advertises a workload
        # authority for this agent the v3 token wins, and when it does not the
        # request stays on the route the other credentials select.
        self._base = _CitadelEvaluationClient(
            self._api_url,
            api_key,
            timeout_seconds=timeout,
            on_api_error=on_api_error,
            identity=identity,
            okta_bootstrap_private_key=okta_key,
            workload_private_key=_pem(workload_private_key),
            sdk_version=SDK_VERSION,
            sdk_engine=SDK_ENGINE,
            sdk_language=SDK_LANGUAGE,
            async_transport=transport,
        )

    async def close(self) -> None:
        await self._base.aclose()

    @staticmethod
    def _plain(payload: dict[str, Any]) -> dict[str, Any]:
        # The round-trip through `default=str` is what keeps a datetime or UUID
        # anywhere in an event from raising: core's serializer is strict, and
        # this SDK's payloads have always been built leniently.
        return json.loads(json.dumps(payload, default=str))

    def _auth_failure(self, path: str, exc: Exception) -> OpenBoxAuthError:
        return OpenBoxAuthError(f"OpenBox rejected the credential on {path}: {exc}")

    async def evaluate(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Send one governance event. `None` when failing open on a transport error.

        The SDK-internal event name is mapped to the canonical wire name here,
        at the last possible moment, so logs and the sequencer keep the
        descriptive form while Core gets one of the six it accepts.
        """
        payload = self._plain({**event, "event_type": to_server_event_type(event.get("event_type", ""))})
        try:
            result = await self._base.aevaluate(payload)
        except CoreAuthError as exc:
            # Includes the signing/assertion rejections, which carry Core's
            # reason code (e.g. method_endpoint_mismatch) in the message.
            if self._fail_hard_on_auth_error:
                raise self._auth_failure("governance/evaluate", exc) from exc
            logger.error(
                "OpenBox credential rejected; continuing UNGOVERNED because "
                "fail_hard_on_auth_error is off: %s", exc
            )
            return None
        except (GovernanceAPIError, OpenBoxNetworkError):
            # GovernanceAPIError is only raised under fail_closed. A network
            # error here is the workload bootstrap or token exchange failing,
            # which is as much a transport failure as Core being down.
            if self._on_api_error == "fail_closed":
                raise
            logger.warning(
                "governance %s failed; failing open", event.get("event_type"), exc_info=True
            )
            return None
        if result.fallback_used:
            # The base client's fail_open ALLOW. This SDK has always signalled
            # "no verdict" as None so callers cannot mistake it for a policy allow.
            logger.warning("governance %s failed; failing open", event.get("event_type"))
            return None
        return result.raw

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
            key = (approval_id, approval_id, approval_id)
        else:
            key = (workflow_id, run_id, activity_id)

        try:
            result = await self._base.apoll_approval(*key)
        except CoreAuthError as exc:
            if self._fail_hard_on_auth_error:
                raise self._auth_failure("governance/approval", exc) from exc
            return None
        except (GovernanceAPIError, OpenBoxNetworkError):
            if self._on_api_error == "fail_closed":
                raise
            return None
        if result is None:
            return None

        # The base client has already set `expired` from the snake_case field;
        # Core has also been seen sending the camelCase one.
        data = dict(result.raw)
        expiration = data.get("approvalExpirationTime")
        if not data.get("expired") and isinstance(expiration, str) and expiration.strip():
            try:
                expires_at = datetime.fromisoformat(expiration)
            except ValueError:
                return data
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at < datetime.now(UTC):
                data["expired"] = True
        return data


__all__ = ["GovernanceClient", "OpenBoxAuthError"]
