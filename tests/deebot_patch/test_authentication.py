"""The password-free login backported in ``deebot_patch/authentication.py``.

The api calls a login goes through are stubbed on the instance rather than served
by a fake http server: what these tests are about is *which* of them a login
touches, and ``test_contract.py`` pins that the private names they are stubbed
under still exist on ``_AuthClient``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from deebot_client.authentication import create_rest_config
from deebot_client.exceptions import (
    AuthenticationError,
    DeviceVerificationRequiredError,
)

from custom_components.ecovacs_mower.deebot_patch.authentication import (
    AccountAuthenticator,
)

ACCOUNT = {"access_token": "token-abc", "user_id": "uid-account"}

# What ``user/login`` and ``user/verifyDevice`` answer with.
LOGIN_RESPONSE = {"uid": "uid-password", "accessToken": "token-from-password"}


def _authenticator(account: dict[str, str] | None = None) -> AccountAuthenticator:
    """Build an authenticator whose http session is never reached."""
    return AccountAuthenticator(
        create_rest_config(
            Mock(),
            device_id="deviceid",
            alpha_2_country="SE",
        ),
        "someone@example.com",
        "passwordhash",
        account_credentials=account,
    )


def _stub_api(authenticator: AccountAuthenticator) -> dict[str, AsyncMock]:
    """Stub the private api calls of a login and return them by name."""
    client = authenticator._auth_client
    mocks = {
        "login_api": AsyncMock(return_value=dict(LOGIN_RESPONSE)),
        "auth_api": AsyncMock(return_value="authcode"),
        "login_by_it_token": AsyncMock(
            return_value={"userId": "uid-portal", "token": "portal-token"}
        ),
    }
    client._AuthClient__call_login_api = mocks["login_api"]
    client._AuthClient__call_auth_api = mocks["auth_api"]
    client._AuthClient__call_login_by_it_token = mocks["login_by_it_token"]
    return mocks


CAPTURED = {
    "access_token": LOGIN_RESPONSE["accessToken"],
    "user_id": LOGIN_RESPONSE["uid"],
}


async def test_stored_account_skips_the_password_endpoint() -> None:
    # The fix for issue #21: the endpoint that answers 1013 is never called.
    authenticator = _authenticator(ACCOUNT)
    mocks = _stub_api(authenticator)

    credentials = await authenticator.authenticate()

    mocks["login_api"].assert_not_called()
    mocks["auth_api"].assert_awaited_once_with(
        ACCOUNT["access_token"], ACCOUNT["user_id"]
    )
    assert credentials.token == "portal-token"
    await authenticator.teardown()


async def test_password_login_captures_the_account_pair() -> None:
    # Without a stored pair the password login runs, and what it returns is what
    # the config flow persists for the next load.
    authenticator = _authenticator()
    mocks = _stub_api(authenticator)

    await authenticator.authenticate()

    mocks["login_api"].assert_awaited_once()
    assert authenticator.account_credentials == CAPTURED
    await authenticator.teardown()


async def test_device_verification_captures_the_account_pair() -> None:
    # The path an affected account actually takes: the password endpoint answers
    # 1013, so only the emailed code produces a pair to persist.
    authenticator = _authenticator()
    mocks = _stub_api(authenticator)
    mocks["login_api"].side_effect = DeviceVerificationRequiredError
    client = authenticator._auth_client
    client._AuthClient__encrypt_account = AsyncMock(return_value="encrypted")
    client._AuthClient__call_private_api = AsyncMock(
        return_value=dict(LOGIN_RESPONSE)
    )

    with pytest.raises(DeviceVerificationRequiredError):
        await authenticator.authenticate()
    await authenticator.verify_device("123456")

    assert authenticator.account_credentials == CAPTURED
    await authenticator.teardown()


async def test_dead_account_pair_falls_back_to_the_password() -> None:
    # A stored pair the server no longer accepts must not lock the account out
    # of the login that does still work.
    authenticator = _authenticator(ACCOUNT)
    mocks = _stub_api(authenticator)
    mocks["auth_api"].side_effect = [AuthenticationError("expired"), "authcode"]

    await authenticator.authenticate()

    mocks["login_api"].assert_awaited_once()
    assert authenticator.account_credentials == CAPTURED
    await authenticator.teardown()


async def test_connection_error_is_not_retried_as_a_password_login() -> None:
    # Only an AuthenticationError means "this pair is no good". A connection
    # error has to reach the caller, which turns it into ConfigEntryNotReady.
    authenticator = _authenticator(ACCOUNT)
    mocks = _stub_api(authenticator)
    mocks["auth_api"].side_effect = TimeoutError

    with pytest.raises(TimeoutError):
        await authenticator.authenticate()

    mocks["login_api"].assert_not_called()
    await authenticator.teardown()


async def test_malformed_stored_pair_falls_back_to_the_password() -> None:
    # A hand-edited entry or a divergent writer only guarantees the key name
    # (CONF_CREDENTIALS), not the value shape. A missing sub-key must not raise
    # KeyError — that would skip the fallback below and surface as an
    # unhandled traceback instead of the password login that would have worked.
    authenticator = _authenticator({"user_id": "uid-only"})
    mocks = _stub_api(authenticator)

    await authenticator.authenticate()

    mocks["login_api"].assert_awaited_once()
    await authenticator.teardown()


async def test_replacement_pair_reaches_the_callback() -> None:
    # The password fallback can mint a pair that differs from the stale one
    # supplied at construction. on_account_credentials is how a caller (the
    # controller, wired to async_update_entry) learns about the replacement.
    captured: list[dict[str, str]] = []
    authenticator = AccountAuthenticator(
        create_rest_config(Mock(), device_id="deviceid", alpha_2_country="SE"),
        "someone@example.com",
        "passwordhash",
        account_credentials=ACCOUNT,
        on_account_credentials=captured.append,
    )
    mocks = _stub_api(authenticator)
    mocks["auth_api"].side_effect = [AuthenticationError("expired"), "authcode"]

    await authenticator.authenticate()

    assert captured == [CAPTURED]
    await authenticator.teardown()


async def test_token_based_login_does_not_retrigger_the_callback() -> None:
    # A routine token based re-login re-affirms the same pair; a caller wiring
    # the callback to async_update_entry must not rewrite the entry every time.
    captured: list[dict[str, str]] = []
    authenticator = AccountAuthenticator(
        create_rest_config(Mock(), device_id="deviceid", alpha_2_country="SE"),
        "someone@example.com",
        "passwordhash",
        account_credentials=ACCOUNT,
        on_account_credentials=captured.append,
    )
    _stub_api(authenticator)

    await authenticator.authenticate()

    assert captured == []
    await authenticator.teardown()
