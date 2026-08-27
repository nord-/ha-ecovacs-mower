"""Assumptions about deebot-client's internal structure.

If any of these break, upstream has changed something we hook into. Better that
CI goes red than that the mower silently stops reporting.
"""

from dataclasses import fields

from deebot_client.capabilities import Capabilities
from deebot_client.events import StateEvent
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


async def test_a_command_the_device_did_not_answer_yields_an_empty_response() -> None:
    # EcovacsEntity._execute_command treats a falsy execute_command() return as
    # "not confirmed" and logs it. This pins the actual failure path it depends
    # on — a REST timeout — rather than just DeviceCommandResult's default, so
    # an upstream change to what a failure carries in raw_response is caught
    # here first.
    from unittest.mock import AsyncMock, MagicMock

    from deebot_client.command import Command
    from deebot_client.const import DataType
    from deebot_client.exceptions import ApiTimeoutError
    from deebot_client.message import HandlingResult, HandlingState

    class _Command(Command):
        NAME = "test"
        DATA_TYPE = DataType.JSON

        def _get_payload(self) -> dict:
            return {}

        def _handle_response(self, event_bus: object, response: dict) -> HandlingResult:
            return HandlingResult(HandlingState.SUCCESS)

    authenticator = MagicMock()
    authenticator.authenticate = AsyncMock(return_value=MagicMock(user_id="uid"))
    authenticator.post_authenticated = AsyncMock(side_effect=ApiTimeoutError)
    device_info = {"class": "test", "did": "did", "resource": "res"}

    result = await _Command().execute(authenticator, device_info, MagicMock())

    assert result.device_reached is False
    assert result.raw_response == {}


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
    # not raise.
    #
    # It is also the reason patch_device_info has to add an entry for
    # MowerProtectStateEvent by hand — an unpatched definition has none, so the
    # bus asks nobody and the flags stay unknown (issue #31). MowerMapInfoEvent
    # is still in that position: the map has no get command wired up.
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


def test_refresh_commands_are_read_from_the_events_mapping() -> None:
    # patch_device_info adds the protection flags' get command straight into
    # Capabilities._events, because there is no dataclass field to hang a
    # CapabilityEvent on. If upstream renames the attribute or stops reading it
    # in get_refresh_commands, our object.__setattr__ would quietly write to
    # nothing and the flags would go back to "unknown" with no error anywhere.
    assert "_events" in {field.name for field in fields(Capabilities)}

    # Borrowing the method beats constructing a Capabilities: it is an ABC with
    # a dozen required fields, and the only thing under test is which attribute
    # the lookup reads.
    class _Probe:
        get_refresh_commands = Capabilities.get_refresh_commands

    probe = _Probe()
    marker = [object()]
    probe._events = {StateEvent: marker}
    assert probe.get_refresh_commands(StateEvent) is marker


def test_upstream_get_pos_drops_every_sample_it_calls_invalid() -> None:
    # The whole reason OnPos exists. GetPos reads "invalid" as a boolean, and
    # firmware 1.13.10 flags roughly nine of ten position samples 2. When this
    # goes red upstream has learned to read the flag and OnPos can go.
    from unittest.mock import Mock

    from deebot_client.commands.json.pos import GetPos

    event_bus = Mock()
    GetPos._handle_body_data_dict(
        event_bus, {"deebotPos": {"x": 1, "y": 2, "a": 3, "invalid": 2}}
    )
    assert event_bus.notify.call_args_list == []


async def test_an_exact_message_name_beats_the_legacy_fallback() -> None:
    # OnPos only takes effect because get_message() resolves MESSAGES before it
    # falls back to the getPos command. It is the only handler here whose effect
    # depends on that order: the rest either name a message with no legacy
    # counterpart, or — onStats — overwrite an exact upstream entry, which the
    # fallback is never consulted for.
    from deebot_client.hardware import get_static_device_info
    from deebot_client.messages import get_message

    from custom_components.ecovacs_mower.deebot_patch import apply
    from custom_components.ecovacs_mower.deebot_patch.messages import OnPos

    static = await get_static_device_info("2i0fns")
    assert static is not None
    apply()
    assert get_message("onPos", static) is OnPos


def test_upstream_on_stats_is_the_class_we_subclass() -> None:
    # OnStatsMower inherits the name and calls super() for StatsEvent. A rename
    # upstream would register our handler under a name nothing publishes on, and
    # the area and time sensors would stop being fed by the push.
    from deebot_client.messages.json.stats import OnStats

    assert OnStats.NAME == "onStats"
    # Whether the live entry is upstream's or ours depends on whether apply()
    # has run in this session, and MESSAGES is a process-wide mutable dict. What
    # matters either way is that the slot is filled by something derived from
    # this class: upstream ships an onStats entry, so apply() has to overwrite
    # one rather than fill a gap, and apply() verifies its own write took.
    assert issubclass(MESSAGES["onStats"], OnStats)


def test_upstream_on_stats_publishes_the_two_fields_it_keeps() -> None:
    # The half of the payload our subclass does not touch. If upstream stopped
    # notifying here, super() would go quiet and only MowerStatsEvent would
    # survive the push.
    from unittest.mock import Mock

    from deebot_client.events import StatsEvent
    from deebot_client.messages.json.stats import OnStats

    event_bus = Mock()
    OnStats._handle_body_data_dict(
        event_bus, {"time": 977, "area": 208900, "mowedArea": 105925}
    )
    event_bus.notify.assert_called_once_with(
        StatsEvent(area=208900, time=977, type=None)
    )
