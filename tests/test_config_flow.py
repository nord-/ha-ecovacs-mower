"""Config flow, with emphasis on the device verification that solves error 1013.

Home Assistant cannot be imported on Windows (``fcntl``), see
``tests/conftest.py``. The imports therefore live inside the test functions and
the whole file is marked ``requires_ha`` — otherwise collection itself crashes.
The source of truth is CI on ubuntu-latest.
"""

from unittest.mock import patch

from . import requires_ha

pytestmark = requires_ha

_AUTHENTICATOR = (
    "custom_components.ecovacs_mower.config_flow.AccountAuthenticator"
)
_VALIDATE_MQTT = "custom_components.ecovacs_mower.config_flow._validate_mqtt"

AUTH = {
    "username": "someone@example.com",
    "password": "hunter2",
    "country": "SE",
}


async def _start(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.ecovacs_mower.const import DOMAIN

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mode": "cloud"}
    )


async def test_happy_path_creates_entry(hass) -> None:
    from homeassistant.const import CONF_DEVICE_ID, CONF_USERNAME
    from homeassistant.data_entry_flow import FlowResultType

    with (
        patch(_AUTHENTICATOR, autospec=True) as authenticator,
        patch(_VALIDATE_MQTT, return_value={}),
    ):
        authenticator.return_value.account_credentials = None
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], AUTH
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == AUTH[CONF_USERNAME]
    assert result["data"][CONF_DEVICE_ID]


async def test_verification_required_shows_code_step(hass) -> None:
    from deebot_client.exceptions import DeviceVerificationRequiredError
    from homeassistant.data_entry_flow import FlowResultType

    with patch(_AUTHENTICATOR, autospec=True) as authenticator:
        authenticator.return_value.account_credentials = None
        authenticator.return_value.authenticate.side_effect = (
            DeviceVerificationRequiredError
        )
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], AUTH
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "device_verification"


async def test_bad_verification_code_reports_error(hass) -> None:
    from deebot_client.exceptions import (
        DeviceVerificationRequiredError,
        InvalidVerificationCodeError,
    )
    from homeassistant.data_entry_flow import FlowResultType

    from custom_components.ecovacs_mower.const import CONF_VERIFICATION_CODE

    with patch(_AUTHENTICATOR, autospec=True) as authenticator:
        authenticator.return_value.account_credentials = None
        authenticator.return_value.authenticate.side_effect = (
            DeviceVerificationRequiredError
        )
        authenticator.return_value.verify_device.side_effect = (
            InvalidVerificationCodeError
        )
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], AUTH
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "000000"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_verification_code"}


async def test_device_id_is_persisted_after_verification(hass) -> None:
    # The core of the 1013 fix: the same client ID must be reused on every
    # login, otherwise Ecovacs demands a new verification.
    from deebot_client.exceptions import DeviceVerificationRequiredError
    from homeassistant.const import CONF_DEVICE_ID
    from homeassistant.data_entry_flow import FlowResultType

    from custom_components.ecovacs_mower.const import CONF_VERIFICATION_CODE

    with (
        patch(_AUTHENTICATOR, autospec=True) as authenticator,
        patch(_VALIDATE_MQTT, return_value={}),
    ):
        authenticator.return_value.account_credentials = None
        authenticator.return_value.authenticate.side_effect = (
            DeviceVerificationRequiredError
        )
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], AUTH
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "123456"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_ID]


async def test_account_credentials_are_persisted_after_verification(hass) -> None:
    # The other half of the 1013 fix: without the account pair in the entry, the
    # reload right after a successful verification logs in with the password,
    # gets 1013 back and asks for another code (issue #21).
    from deebot_client.exceptions import DeviceVerificationRequiredError
    from homeassistant.data_entry_flow import FlowResultType

    from custom_components.ecovacs_mower.const import (
        CONF_CREDENTIALS,
        CONF_VERIFICATION_CODE,
    )

    account = {"access_token": "token-abc", "user_id": "uid-1"}

    with (
        patch(_AUTHENTICATOR, autospec=True) as authenticator,
        patch(_VALIDATE_MQTT, return_value={}),
    ):
        authenticator.return_value.account_credentials = account
        authenticator.return_value.authenticate.side_effect = (
            DeviceVerificationRequiredError
        )
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], AUTH
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "123456"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CREDENTIALS] == account
