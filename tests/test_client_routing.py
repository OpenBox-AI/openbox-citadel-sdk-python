"""The real GovernanceClient against a scripted Core, over httpx.MockTransport.

Core serves each agent identity method on exactly one route family, and a call
on the wrong one is refused with `method_endpoint_mismatch`. These pin that the
client picks the family from the credentials it was given, and keeps this SDK's
own contract on top: raw dicts, `None` for "no verdict", its own auth error.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from openbox_core.errors import GovernanceAPIError

from openbox_citadel.client import GovernanceClient, OpenBoxAuthError, _pem

CORE = "http://localhost:8086"
ISSUER = "http://localhost:8080/realms/openbox"
VERDICT = {"verdict": "allow", "reason": "ok", "approval_id": None, "trust_tier": 2}


def _rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


WORKLOAD_PEM = _rsa_pem()
DID = f"did:aip:{uuid.uuid4()}"
DID_KEY = base64.b64encode(os.urandom(32)).decode()


def _bootstrap_doc() -> dict[str, Any]:
    return {
        "bootstrap_version": 3,
        "contract_version": 3,
        "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
        "issuer": ISSUER,
        "audience": "openbox-core",
        "client_id": "agent-client",
        "service_account_id": str(uuid.uuid4()),
        "activation_version": str(uuid.uuid4()),
        "identity_source": "openbox",
        "kid": "kid-1",
    }


class Core:
    """Records every request and answers like Core and Keycloak do."""

    def __init__(self, *, bootstrap_status: int = 200, token_status: int = 200,
                 evaluate_status: int = 200, evaluate_body: dict | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.bootstrap_status = bootstrap_status
        self.token_status = token_status
        self.evaluate_status = evaluate_status
        self.evaluate_body = evaluate_body if evaluate_body is not None else VERDICT

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v3/auth/bootstrap":
            if self.bootstrap_status != 200:
                return httpx.Response(self.bootstrap_status, json={"reason_code": "not_found"})
            return httpx.Response(200, json=_bootstrap_doc())
        if path.endswith("/protocol/openid-connect/token"):
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            return httpx.Response(200, json={
                "access_token": "workload-token-1", "token_type": "Bearer", "expires_in": 300,
            })
        if path.endswith("/governance/evaluate"):
            if self.evaluate_status != 200:
                return httpx.Response(self.evaluate_status, json={
                    "reason_code": "method_endpoint_mismatch",
                    "message": "keycloak_workload agent may not call v1 routes",
                })
            return httpx.Response(200, json=self.evaluate_body)
        if path.endswith("/governance/approval"):
            return httpx.Response(200, json={"verdict": "allow", "approval_id": "ap-1"})
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def last(self, suffix: str) -> httpx.Request:
        return next(r for r in reversed(self.requests) if r.url.path.endswith(suffix))


def _client(core: Core, **kwargs: Any) -> GovernanceClient:
    return GovernanceClient(api_url=CORE, api_key="obx_test_key", transport=httpx.MockTransport(core),
                            **kwargs)


EVENT = {"event_type": "ActivityStarted", "activity_type": "Apollo", "workflow_id": "wf-1"}


async def test_api_key_only_uses_v1() -> None:
    core = Core()
    result = await _client(core).evaluate(EVENT)
    assert result == VERDICT
    assert core.paths() == ["/api/v1/governance/evaluate"]
    request = core.last("/governance/evaluate")
    assert request.headers["authorization"] == "Bearer obx_test_key"
    assert "x-openbox-agent-signature" not in request.headers


async def test_did_signs_v1() -> None:
    core = Core()
    await _client(core, agent_did=DID, agent_private_key=DID_KEY).evaluate(EVENT)
    request = core.last("/governance/evaluate")
    assert request.url.path == "/api/v1/governance/evaluate"
    assert request.headers["x-openbox-agent-did"] == DID
    assert request.headers["x-openbox-agent-signature"]


async def test_workload_key_uses_v3_with_token() -> None:
    core = Core()
    client = _client(core, workload_private_key=WORKLOAD_PEM)
    assert await client.evaluate(EVENT) == VERDICT
    assert core.paths() == [
        "/api/v3/auth/bootstrap",
        "/realms/openbox/protocol/openid-connect/token",
        "/api/v3/governance/evaluate",
    ]
    request = core.last("/governance/evaluate")
    assert request.headers["x-openbox-workload-token"] == "workload-token-1"
    assert request.headers["authorization"] == "Bearer obx_test_key"
    # The event body is the event, with the wire name — not a re-shaped payload.
    assert json.loads(request.content)["activity_type"] == "Apollo"


async def test_workload_token_is_reused_across_calls() -> None:
    core = Core()
    client = _client(core, workload_private_key=WORKLOAD_PEM)
    await client.evaluate(EVENT)
    await client.evaluate(EVENT)
    assert core.paths().count("/api/v3/auth/bootstrap") == 1
    assert core.paths()[-1] == "/api/v3/governance/evaluate"


async def test_workload_key_on_an_agent_without_workload_authority_stays_on_v1() -> None:
    """A deployment or agent with no v3 authority answers bootstrap 404; the
    request keeps the route its other credentials select."""
    core = Core(bootstrap_status=404)
    await _client(core, workload_private_key=WORKLOAD_PEM,
                  agent_did=DID, agent_private_key=DID_KEY).evaluate(EVENT)
    assert core.paths()[-1] == "/api/v1/governance/evaluate"


async def test_pem_with_escaped_newlines_is_accepted() -> None:
    """The dashboard shows the key JSON-quoted on one line."""
    escaped = json.dumps(WORKLOAD_PEM)  # quoted, with literal \\n
    assert _pem(escaped) == WORKLOAD_PEM.strip()
    core = Core()
    await _client(core, workload_private_key=_pem(escaped)).evaluate(EVENT)
    assert core.paths()[-1] == "/api/v3/governance/evaluate"


async def test_wrong_route_family_is_an_auth_error_even_under_fail_open() -> None:
    core = Core(evaluate_status=403)
    with pytest.raises(OpenBoxAuthError):
        await _client(core, on_api_error="fail_open").evaluate(EVENT)


async def test_auth_error_can_be_downgraded_deliberately() -> None:
    core = Core(evaluate_status=401)
    assert await _client(core, fail_hard_on_auth_error=False).evaluate(EVENT) is None


async def test_rejected_workload_token_exchange_is_an_auth_error() -> None:
    core = Core(token_status=401)
    with pytest.raises(OpenBoxAuthError):
        await _client(core, workload_private_key=WORKLOAD_PEM).evaluate(EVENT)
    assert "/api/v3/governance/evaluate" not in core.paths()
    assert "/api/v1/governance/evaluate" not in core.paths()


async def test_server_error_fails_open_as_none_and_closed_as_raise() -> None:
    core = Core(evaluate_status=500)
    assert await _client(core, on_api_error="fail_open").evaluate(EVENT) is None
    with pytest.raises(GovernanceAPIError):
        await _client(Core(evaluate_status=500), on_api_error="fail_closed").evaluate(EVENT)


async def test_unreachable_workload_bootstrap_fails_open_not_downgraded() -> None:
    core = Core(bootstrap_status=503)
    assert await _client(core, workload_private_key=WORKLOAD_PEM).evaluate(EVENT) is None
    assert "/api/v1/governance/evaluate" not in core.paths()


async def test_approval_poll_keys_on_approval_id_over_v3() -> None:
    core = Core()
    client = _client(core, workload_private_key=WORKLOAD_PEM)
    data = await client.poll_approval(workflow_id="wf", run_id="run", activity_id="act",
                                      approval_id="ap-1")
    assert data["approval_id"] == "ap-1"
    request = core.last("/governance/approval")
    assert request.url.path == "/api/v3/governance/approval"
    assert json.loads(request.content) == {
        "workflow_id": "ap-1", "run_id": "ap-1", "activity_id": "ap-1",
    }


async def test_approval_expiry_is_flagged_client_side() -> None:
    class Expired(Core):
        def __call__(self, request):
            self.requests.append(request)
            return httpx.Response(200, json={
                "verdict": "require_approval", "approval_expiration_time": "2020-01-01T00:00:00Z",
            })

    data = await _client(Expired()).poll_approval(workflow_id="wf", run_id="r", activity_id="a")
    assert data["expired"] is True


async def test_did_and_okta_together_is_refused() -> None:
    with pytest.raises(ValueError):
        _client(Core(), agent_did=DID, agent_private_key=DID_KEY, okta_agent_private_key=WORKLOAD_PEM)


async def test_half_a_did_is_refused() -> None:
    with pytest.raises(ValueError):
        _client(Core(), agent_did=DID)
