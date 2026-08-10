"""Assumptions about deebot-client's internal structure.

If any of these break, upstream has changed something we hook into. Better that
CI goes red than that the mower silently stops reporting.
"""

from dataclasses import fields

from deebot_client.capabilities import Capabilities
from deebot_client.hardware import _DEVICES
from deebot_client.messages.json import MESSAGES


def test_devices_cache_is_a_mutable_dict() -> None:
    assert isinstance(_DEVICES, dict)


def test_messages_registry_is_a_mutable_dict() -> None:
    assert isinstance(MESSAGES, dict)


def test_messages_registry_is_shared_by_reference() -> None:
    # messages/__init__.py holds a reference to the same object. If we mutate it
    # in place the change is visible in get_message().
    from deebot_client.const import DataType
    from deebot_client.messages import MESSAGES as ALL_MESSAGES

    assert ALL_MESSAGES[DataType.JSON] is MESSAGES


def test_capabilities_has_the_fields_we_patch() -> None:
    names = {f.name for f in fields(Capabilities)}
    assert {"clean", "state", "device_type"} <= names


def test_capabilities_is_frozen() -> None:
    # The patching uses dataclasses.replace() precisely because it is frozen.
    assert Capabilities.__dataclass_params__.frozen


def test_clean_info_v2_subclasses_clean_info() -> None:
    # This is why verify_capabilities compares exact types instead of using
    # isinstance: a GetCleanInfoV2 instance is a GetCleanInfo, so isinstance()
    # would accept the unpatched set and make the check toothless. If this stops
    # holding the check can be simplified — but it is correct either way.
    from deebot_client.commands.json.clean import GetCleanInfo, GetCleanInfoV2

    assert issubclass(GetCleanInfoV2, GetCleanInfo)
