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


def test_auth_client_has_the_login_internals_we_wrap() -> None:
    # AccountAuthenticator replaces both on the instance to get a login that
    # does not touch the password endpoint. Its own constructor raises
    # PatchContractError when they are gone, so this only makes CI the first
    # place that says so.
    from deebot_client.authentication import _AuthClient

    from custom_components.ecovacs_mower.deebot_patch.authentication import (
        missing_wrapped_members,
    )

    assert missing_wrapped_members() == ()
    assert hasattr(_AuthClient, "_AuthClient__complete_login")


def test_complete_login_takes_the_raw_account_pair() -> None:
    # The token based login feeds __complete_login a synthetic
    # {"uid", "accessToken"} dict instead of reimplementing its tail. That only
    # works while those are the keys it reads and while it takes the response as
    # its first argument.
    import inspect

    from deebot_client.authentication import _AuthClient

    complete_login = _AuthClient._AuthClient__complete_login
    assert list(inspect.signature(complete_login).parameters) == [
        "self",
        "response",
        "error",
    ]
    # Quotes normalized: a quote-style refactor with no semantic change must
    # not fail this, only a rename or removal of the keys themselves should.
    source = inspect.getsource(complete_login).replace("'", '"')
    assert '"uid"' in source
    assert '"accessToken"' in source


def test_auth_client_has_the_login_helpers_the_tests_stub() -> None:
    # test_authentication.py replaces these private calls with mocks to steer a
    # login without a fake HTTP server. If a deebot-client bump renames one, the
    # assignment becomes an inert shadow attribute and the real method runs
    # against a Mock session instead of failing here first.
    from deebot_client.authentication import _AuthClient

    for name in (
        "_AuthClient__call_login_api",
        "_AuthClient__call_auth_api",
        "_AuthClient__call_login_by_it_token",
        "_AuthClient__encrypt_account",
        "_AuthClient__call_private_api",
    ):
        assert hasattr(_AuthClient, name)


def test_capabilities_has_the_fields_we_patch() -> None:
    names = {f.name for f in fields(Capabilities)}
    assert {"clean", "state", "device_type"} <= names


def test_capabilities_is_frozen() -> None:
    # The patching uses dataclasses.replace() precisely because it is frozen.
    assert Capabilities.__dataclass_params__.frozen


def test_a_command_the_device_did_not_answer_yields_an_empty_response() -> None:
    # EcovacsEntity._execute_command treats a falsy execute_command() return as
    # "not confirmed" and logs it. That only holds while raw_response defaults to
    # an empty dict on the failure paths.
    from deebot_client.command import DeviceCommandResult

    assert DeviceCommandResult(device_reached=False).raw_response == {}


def test_clean_info_v2_subclasses_clean_info() -> None:
    # This is why verify_capabilities compares exact types instead of using
    # isinstance: a GetCleanInfoV2 instance is a GetCleanInfo, so isinstance()
    # would accept the unpatched set and make the check toothless. If this stops
    # holding the check can be simplified — but it is correct either way.
    from deebot_client.commands.json.clean import GetCleanInfo, GetCleanInfoV2

    assert issubclass(GetCleanInfoV2, GetCleanInfo)


async def test_refresh_commands_empty_for_unknown_events() -> None:
    # map_messages and messages notify custom event types on the library's
    # EventBus. The bus looks up refresh commands per event type from
    # capabilities; for unregistered types this must degrade to "no commands",
    # not raise. It is also why those entities cannot be refreshed on demand.
    from deebot_client.hardware import get_static_device_info

    from custom_components.ecovacs_mower.deebot_patch.map_messages import (
        MowerMapInfoEvent,
    )
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerProtectStateEvent,
    )

    static = await get_static_device_info("2i0fns")
    assert static is not None
    for event in (MowerMapInfoEvent, MowerProtectStateEvent):
        assert static.capabilities.get_refresh_commands(event) == []
