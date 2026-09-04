"""Diagnostics for Ecovacs Mower."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_DEVICE_ID, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import EcovacsMowerConfigEntry
from .const import CONF_CREDENTIALS, CONF_OVERRIDE_MQTT_URL, CONF_OVERRIDE_REST_URL

# CONF_OVERRIDE_MQTT_URL/CONF_OVERRIDE_REST_URL are redacted so that
# self-hosted installations do not leak their internal broker or REST address in
# a diagnostics report shared in a GitHub issue.
#
# CONF_DEVICE_ID is fork-specific and absent from upstream's REDACT: core never
# stores the client's device ID but generates a new one on every start — which is
# exactly the 1013 bug this integration exists to fix. We persist it in
# entry.data (config_flow.py), so it reaches the dump where core's version never
# could. Both modes leak:
#
# * self-hosted: the value is ``HA-{slugify(location_name)}`` — the user's
#   instance/home name, PII outright.
# * cloud: the value is the Ecovacs-verified, stable client identity. Combined
#   with a leaked account it skips email verification entirely — exactly the
#   protection the whole fix is built on.
#
# CONF_CREDENTIALS holds the Ecovacs account access token, which mints portal
# credentials on its own — a password equivalent, and the one secret in the entry
# that is enough to control the mower without knowing anything else.
#
# "homeId" is redacted even though it is not in the ApiDeviceInfo TypedDict:
# api_client.py feeds raw API JSON straight into it, so keys outside the
# TypedDict shape do not disappear — they still come along in
# device.device_info and end up in the dump. Same leak mechanism as the
# override URLs.
#
# "nick" (the user's own name for the mower) and "resource" (the other half of
# the MQTT topic; "did" is already masked, but without "resource" the topic can
# still be reconstructed) are in the TypedDict and come along entirely
# unabridged — device.device_info *is* the raw api dict.
#
# "btMac" and "btName" are the mower's Bluetooth identity, and they leak by the
# same mechanism as "homeId": outside the TypedDict, carried through whole. The
# set below has listed "mac" since it was written, which reads like it covers
# the first of them and does not — async_redact_data matches keys exactly, and a
# GOAT payload has no "mac" key at all. The Wi-Fi MAC reaches the device
# registry by an entirely different path (Device.mac, into DeviceInfo's
# connections), never through this dict, so "mac" masks nothing here; it stays
# only because the payload shape is Ecovacs' to change, not ours to pin.
#
# Masking both matters more than the usual "it is an identifier" argument: a MAC
# is globally unique and geolocatable through public BSSID databases, and the
# diagnostics dump is the artifact users are asked to attach to a GitHub issue —
# the one path every other entry in this set exists to protect.
REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_CREDENTIALS,
    CONF_DEVICE_ID,
    CONF_OVERRIDE_MQTT_URL,
    CONF_OVERRIDE_REST_URL,
    "did",
    "name",
    "nick",
    "resource",
    "homeId",
    "mac",
    "btMac",
    "btName",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EcovacsMowerConfigEntry
) -> dict[str, Any]:
    """Diagnostics for a config entry."""
    controller = entry.runtime_data
    return {
        "config": async_redact_data(dict(entry.data), REDACT),
        "devices": [
            async_redact_data(dict(device.device_info), REDACT)
            for device in controller.devices
        ],
    }
