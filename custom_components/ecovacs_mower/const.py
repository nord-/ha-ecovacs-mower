"""Constants for Ecovacs Mower."""

from datetime import timedelta
from enum import StrEnum

from deebot_client.events import LifeSpan

DOMAIN = "ecovacs_mower"

# Mirrors manifest.json's issue_tracker. Used in log messages, which cannot
# read the manifest.
ISSUE_TRACKER_URL = "https://github.com/nord-/ha-ecovacs-mower/issues"

# Same key name as the pending core fix (home-assistant/core#178558), so an
# entry written by either one is readable by the other.
CONF_CREDENTIALS = "credentials"
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

# The mower is not a reliable narrator of its own state. On 2026-08-21 it
# finished a run, drove home and started charging without sending
# onChargeInfo, onChargeState, or even the bury-point task events it logs for
# itself — while onStats, onBattery, onPos and onMapTrack all kept arriving.
# That missing bury point is also the recorded case of the completion the
# progress sensor reads its final percentage from going astray (issue #73),
# which is why nothing downstream of it may depend on its arriving.
# The entity stayed "mowing" for two hours; one homeassistant.update_entity
# corrected it in 200 ms, over REST, so the answer was there the whole time
# and nobody had asked for it.
#
# Five minutes: worst case the state is that stale, against two commands per
# interval on Ecovacs' cloud API. Push still does the fast path — this only
# catches what push drops.
#
# Owned by EcovacsController rather than any one entity: lawn_mower, activity
# and mowing_progress/stats can each be individually disabled in the entity
# registry, and this has to keep running regardless of which of them are.
POLL_INTERVAL = timedelta(minutes=5)

# How often to ask a mower that is out where it is. The G1-800 (firmware
# 1.36.208) does not push onPos unprompted: a zone run with nobody watching
# in the Ecovacs app delivered onStats, onBattery and onMapTrace_V2 for
# minutes and not one position, so the map marker stood in the dock while
# the mower mowed. Its device definition in deebot-client has no map
# capability either, so there is nothing to refresh through the event bus
# and the position is asked for with a command of its own (the Ecovacs app
# does the same while its live map is open).
#
# Fifteen seconds: close enough to draw a path (a mower covers a few metres
# in that time), against one more command per interval on Ecovacs' cloud
# API while it is out. Nothing is asked while it is docked, and nothing while
# the mower is pushing positions by itself (see _poll_position).
POSITION_POLL_INTERVAL = timedelta(seconds=15)
