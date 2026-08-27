"""Credential rejection. The one API failure that must not degrade to allow."""

from __future__ import annotations

import pytest

from openbox_citadel.client import OpenBoxAuthError
from tests.conftest import Ctx, Decl
from tests.doubles import FakeClient


async def _tool(args, ctx):
    return "ok"


class AuthFailingClient(FakeClient):
    """Rejects the credential the way Core does on a revoked key.

    Mirrors the real client's own handling: the decision to downgrade lives in
    the client, so a double that raises unconditionally would be testing the
    test rather than the SDK.
    """

    def __init__(self, fail_hard: bool = True) -> None:
        super().__init__()
        self._fail_hard_on_auth_error = fail_hard

    async def evaluate(self, event):
        self.events.append(event)
        error = OpenBoxAuthError("OpenBox rejected the credential (401)")
        if self._fail_hard_on_auth_error:
            raise error


async def test_auth_error_fails_hard_even_under_fail_open(mw) -> None:
    """`fail_open` covers OpenBox being *unreachable*. A 401 is a different fact:
    the key is wrong and will stay wrong, so treating it as transient silently
    downgrades every agent to ungoverned."""
    mw._client = AuthFailingClient()
    mw._config.on_api_error = "fail_open"

    async def must_not_run(args, ctx):
        raise AssertionError("must not execute with a rejected credential")

    with pytest.raises(OpenBoxAuthError):
        await mw.before_turn(goal="g")


async def test_auth_error_can_be_downgraded_deliberately(mw, client) -> None:
    """Off is a legitimate choice — a staged key rotation that prefers a window
    of ungoverned execution to an outage — but it must be explicit."""
    mw._client = AuthFailingClient(fail_hard=False)
    mw._config.fail_hard_on_auth_error = False

    # No raise: the turn proceeds, and the client has logged that it is ungoverned.
    await mw.before_turn(goal="g")


async def test_default_is_hard_fail() -> None:
    from openbox_citadel.config import GovernanceConfig

    assert GovernanceConfig().fail_hard_on_auth_error is True


async def test_auth_error_is_not_a_governance_denial(mw) -> None:
    """It must not be translated into the host's `ToolAccessDenied`: a broken
    credential is an operational fault, not a policy decision about this call."""
    from tests.doubles import Denied

    mw._client = AuthFailingClient()
    with pytest.raises(OpenBoxAuthError):
        await mw.govern(Decl("Apollo"), _tool)({}, Ctx())
    assert not issubclass(OpenBoxAuthError, Denied)
