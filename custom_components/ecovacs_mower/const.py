"""Constants for Ecovacs Mower."""

from enum import StrEnum

from deebot_client.events import LifeSpan

DOMAIN = "ecovacs_mower"

# Mirrors manifest.json's issue_tracker. Used in log messages, which cannot
# read the manifest.
ISSUE_TRACKER_URL = "https://github.com/nord-/ha-ecovacs-mower/issues"

CONF_OVERRIDE_REST_URL = "override_rest_url"
CONF_OVERRIDE_MQTT_URL = "override_mqtt_url"
CONF_VERIFICATION_CODE = "verification_code"
CONF_VERIFY_MQTT_CERTIFICATE = "verify_mqtt_certificate"


class InstanceMode(StrEnum):
    """Installation mode."""

    CLOUD = "cloud"
    SELF_HOSTED = "self_hosted"


# Only the life span components a lawn mower actually has. Core exposes 12 of
# the ``LifeSpan`` enum's 26 members (vacuum-oriented — mops, dust bags,
# filters, UV lamp). BLADE and LENS_BRUSH are on that list, but TRIMMER_BRUSH
# and WEED_ROPE are not there at all: they are mower-specific components core
# never exposes, not a trimmed subset of its list. That is why they needed their
# own entries in icons.json.
SUPPORTED_LIFESPANS = (
    LifeSpan.BLADE,
    LifeSpan.LENS_BRUSH,
    LifeSpan.TRIMMER_BRUSH,
    LifeSpan.WEED_ROPE,
)
