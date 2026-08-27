"""Fixtures. Test doubles live in `tests/doubles.py` — see the note there."""

from __future__ import annotations

import pytest

from openbox_citadel.activity_registry import reset_registry
from openbox_citadel.config import GovernanceConfig, HITLConfig
from openbox_citadel.event_sequence import reset_sequencers
from openbox_citadel.middleware import (
    OpenBoxCitadelMiddleware,
    OpenBoxCitadelMiddlewareOptions,
)
from tests.doubles import Denied, FakeClient


@pytest.fixture(autouse=True)
def _clean_state():
    """Sequencers and the activity registry are process-global by design."""
    reset_sequencers()
    reset_registry()
    yield
    reset_sequencers()
    reset_registry()


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def mw(client: FakeClient) -> OpenBoxCitadelMiddleware:
    middleware = OpenBoxCitadelMiddleware(
        OpenBoxCitadelMiddlewareOptions(
            api_url="https://core.test",
            api_key="obx_test_key",
            deny_exc=Denied,
            config=GovernanceConfig(hitl=HITLConfig(poll_interval_ms=1)),
            instrument_http=False,
        )
    )
    middleware._client = client  # type: ignore[assignment]
    return middleware


class Backing:
    kind = "http"
    method = "POST"
    url = "https://api.test/x"


class Decl:
    def __init__(self, name: str) -> None:
        self.name = name
        self.backing = Backing()


class Ctx:
    agent_id = "agent-7"
    source = "config"
    tenant = "squidgy"
    user_id = "u1"
    location_id = None
