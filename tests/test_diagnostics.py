"""REDACT must mask everything upstream core masks.

The module under test imports Home Assistant (via ``diagnostics.py``, which in
turn imports the package's ``__init__``), which cannot be imported on Windows
(``fcntl``). The imports therefore live inside the test functions and the whole
file is marked ``requires_ha`` — otherwise collection itself crashes before any
skip marker gets a chance to apply. The source of truth is CI on ubuntu-latest.
"""

from . import requires_ha

pytestmark = requires_ha


def test_redact_covers_everything_core_redacts() -> None:
    """The contract, not a one-off observation.

    homeassistant/components/ecovacs/diagnostics.py masks CONF_USERNAME,
    CONF_PASSWORD, "title", CONF_OVERRIDE_MQTT_URL, CONF_OVERRIDE_REST_URL
    (config) plus "did", CONF_NAME, "homeId" (device). "title" is not needed here
    — we dump entry.data, not entry.as_dict() — but the rest must be present,
    otherwise a diagnostics report leaks data a user shares publicly.
    """
    from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_USERNAME

    from custom_components.ecovacs_mower.const import (
        CONF_OVERRIDE_MQTT_URL,
        CONF_OVERRIDE_REST_URL,
    )
    from custom_components.ecovacs_mower.diagnostics import REDACT

    must_redact = {
        CONF_USERNAME,
        CONF_PASSWORD,
        CONF_OVERRIDE_MQTT_URL,
        CONF_OVERRIDE_REST_URL,
        "did",
        CONF_NAME,
        "homeId",
    }
    assert must_redact <= REDACT


def test_redact_covers_the_fork_specific_leaks() -> None:
    """Three keys upstream never had to mask.

    * ``CONF_DEVICE_ID``: core never persists the client's device ID — it
      generates a new one on every start, which *is* the 1013 bug. We store it in
      ``entry.data``, so it reaches the dump. Self-hosted, the value is
      ``HA-{slugify(location_name)}`` (the home name, PII); in cloud mode it is
      the verified client identity, which combined with a leaked account skips
      email verification.
    * ``nick``/``resource``: present in ``ApiDeviceInfo`` and carried straight
      into the dump, since ``device.device_info`` *is* the raw api dict.
      ``resource`` is the other half of the MQTT topic.
    """
    from homeassistant.const import CONF_DEVICE_ID

    from custom_components.ecovacs_mower.diagnostics import REDACT

    from custom_components.ecovacs_mower.const import CONF_CREDENTIALS

    # CONF_CREDENTIALS is the account access token, which mints portal
    # credentials on its own: leaking it is leaking the account.
    assert {CONF_DEVICE_ID, CONF_CREDENTIALS, "nick", "resource"} <= REDACT


def test_device_info_keys_are_covered_or_deliberately_public() -> None:
    """Every key in ApiDeviceInfo must be a deliberate decision.

    ``device.device_info`` returns the api dict unabridged. If upstream adds a
    key, this line must go red, so that somebody actually takes a position
    instead of letting it leak out in a diagnostics report pasted into a GitHub
    issue.
    """
    from deebot_client.models import ApiDeviceInfo

    from custom_components.ecovacs_mower.diagnostics import REDACT

    # Deliberately unmasked: the model class and manufacturer are not identifying
    # and are exactly what you need to debug an issue.
    public = {"class", "company", "deviceName"}

    assert set(ApiDeviceInfo.__annotations__) <= REDACT | public


def test_keys_observed_on_real_hardware_are_covered_or_deliberately_public() -> None:
    """The TypedDict is not the payload.

    ``ApiDeviceInfo`` declares a subset. ``api_client.py`` feeds raw API JSON
    straight into it, so a real mower's ``device.device_info`` carries keys the
    annotations never mention — and the test above, which walks
    ``__annotations__``, is structurally blind to every one of them. ``homeId``
    was already masked for exactly this reason; it was added by hand, because
    nothing could have failed to point at it.

    This is the key set observed on GOAT hardware, which is the shape that
    actually reaches a diagnostics report. It is what caught ``btMac``: a
    Bluetooth MAC shipping in the clear past a REDACT that listed ``mac`` and
    therefore looked like it covered the case.

    Keys only, never values — the point of the file this guards is that real
    values do not get published, and a fixture is not an exception to that.
    """
    from custom_components.ecovacs_mower.diagnostics import REDACT

    observed = {
        "did",
        "name",
        "class",
        "resource",
        "company",
        "bindTs",
        "service",
        "deviceName",
        "icon",
        "ota",
        "UILogicId",
        "materialNo",
        "pid",
        "product_category",
        "model",
        "updateInfo",
        "nick",
        "homeId",
        "homeSort",
        "status",
        "btName",
        "btMac",
        "otaUpgrade",
        "networkMode",
    }

    # Deliberately unmasked, in three groups.
    #
    # Model identity ("class", "deviceName", "model", "pid", "materialNo",
    # "product_category", "UILogicId", "icon", "company") describes which machine
    # this is, not whose: it is shared by every unit of the same product, and it
    # is the first thing anyone triaging an issue needs.
    #
    # Firmware and connectivity posture ("ota", "otaUpgrade", "updateInfo",
    # "networkMode", "status") is state, not identity, and a report without it
    # cannot distinguish a stale firmware from a broken patch layer.
    #
    # "service" is the regional endpoint pair (which Ecovacs datacenter answers
    # this account) and "bindTs" is when the mower was bound. Regional and
    # coarse rather than personal, and both change the interpretation of an
    # authentication failure — the class of bug this fork exists for.
    # "homeSort" is this device's ordinal within a household whose "homeId" is
    # already masked, so on its own it names nothing.
    public = {
        "class",
        "company",
        "deviceName",
        "model",
        "pid",
        "materialNo",
        "product_category",
        "UILogicId",
        "icon",
        "ota",
        "otaUpgrade",
        "updateInfo",
        "networkMode",
        "status",
        "service",
        "bindTs",
        "homeSort",
    }

    assert observed <= REDACT | public
