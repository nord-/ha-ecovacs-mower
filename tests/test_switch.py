"""The switches must correspond to the settings a GOAT has."""

import pytest

from tests import requires_ha

pytestmark = requires_ha


def _all_translation_keys() -> set[str]:
    """Every switch's translation key, including the three standalone switches.

    The rain sensor's switch is not in ENTITY_DESCRIPTIONS and cannot be: the
    setting is not a deebot-client capability, so there is no field for
    capability_fn to read (issue #54). The animal-protection switch is in the
    same position (issue #45). The edge cutting switch has a capability, but
    the library's command behind it writes a payload most mowers refuse, so
    it is built on the patch layer's own pair instead (issue #166). Reading
    their keys off the classes instead of hardcoded literals means a typo
    there fails these tests instead of merely looking like a permitted extra
    key.
    """
    from custom_components.ecovacs_mower.switch import (
        ENTITY_DESCRIPTIONS,
        EcovacsAnimalProtectionSwitch,
        EcovacsEdgeCuttingSwitch,
        EcovacsRainDetectionSwitch,
    )

    return {d.translation_key for d in ENTITY_DESCRIPTIONS} | {
        EcovacsRainDetectionSwitch.entity_description.translation_key,
        EcovacsAnimalProtectionSwitch.entity_description.translation_key,
        EcovacsEdgeCuttingSwitch.entity_description.translation_key,
    }


def test_expected_switch_keys() -> None:
    """Locks the set. If it changes, that must be a decision, not an accident."""
    from custom_components.ecovacs_mower.switch import ENTITY_DESCRIPTIONS

    assert {d.key for d in ENTITY_DESCRIPTIONS} == {
        "advanced_mode",
        "true_detect",
        "child_lock",
        "move_up_warning",
        "cross_map_border_warning",
        "safe_protect",
    }


def test_no_vacuum_only_switches() -> None:
    """The capabilities do not exist on 2i0fns, so the entities would be empty anyway."""
    from custom_components.ecovacs_mower.switch import ENTITY_DESCRIPTIONS

    keys = {d.key for d in ENTITY_DESCRIPTIONS}
    assert keys.isdisjoint(
        {"continuous_cleaning", "carpet_auto_fan_boost", "clean_preference", "border_spin"}
    )


def test_every_description_has_a_translation() -> None:
    """A missing key yields raw strings in the UI."""
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    names = strings["entity"]["switch"]

    for key in _all_translation_keys():
        assert key in names, key


def test_every_switch_has_an_icon() -> None:
    """A switch without its own icon gets HA's generic toggle — easy to miss."""
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    names = icons["entity"]["switch"]

    for key in _all_translation_keys():
        assert key in names, key


def test_no_stale_switch_translations_or_icons() -> None:
    """Every key in strings.json/icons.json must belong to a real switch.

    The converse of the tests above: they check description -> string/icon, not
    the other way around. Without this, a leftover key for a removed switch would
    go unnoticed.
    """
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    keys = _all_translation_keys()
    assert set(strings["entity"]["switch"]) <= keys
    assert set(icons["entity"]["switch"]) <= keys


def _rain_switch(device):
    from custom_components.ecovacs_mower.switch import EcovacsRainDetectionSwitch

    entity = EcovacsRainDetectionSwitch(device)
    entity.async_write_ha_state = lambda: None
    return entity


def _device():
    from unittest.mock import AsyncMock, Mock

    device = Mock()
    device.device_info = {"did": "test-did"}
    device.execute_command = AsyncMock(return_value={"ret": "ok"})
    return device


async def _rain_switch_callback(device):
    """Run async_added_to_hass and return the MowerRainDelayEvent callback."""
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    entity = _rain_switch(device)
    await entity.async_added_to_hass()

    for call in device.events.subscribe.call_args_list:
        event_type, callback = call.args
        if event_type is MowerRainDelayEvent:
            return entity, callback
    raise AssertionError("The rain switch never subscribed to MowerRainDelayEvent")


def test_the_rain_switch_is_not_capability_driven() -> None:
    """It cannot be, so it must not quietly reappear in the declarative table.

    ``get_supported_entities`` builds an entity only when ``capability_fn``
    returns something, and ``Capabilities`` has no field for this setting.
    """
    from custom_components.ecovacs_mower.switch import (
        ENTITY_DESCRIPTIONS,
        EcovacsRainDetectionSwitch,
    )

    assert EcovacsRainDetectionSwitch.entity_description.key not in {
        d.key for d in ENTITY_DESCRIPTIONS
    }


def test_the_rain_switch_is_a_config_entity_disabled_by_default() -> None:
    """Same treatment as the six capability-backed settings switches."""
    from homeassistant.const import EntityCategory

    from custom_components.ecovacs_mower.switch import EcovacsRainDetectionSwitch

    description = EcovacsRainDetectionSwitch.entity_description
    assert description.key == "rain_detection"
    assert description.entity_category is EntityCategory.CONFIG
    assert description.entity_registry_enabled_default is False


def test_the_rain_switch_is_unknown_before_the_first_event() -> None:
    """Not False.

    The capability switches default to off because the library refreshes them
    on subscribe; this one is honest about knowing nothing yet, and reporting
    "the rain sensor is off" from a mower nobody has asked is worse than a
    blank.
    """
    assert _rain_switch(_device()).is_on is None


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_rain_switch_follows_the_setting(enabled: bool) -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    entity, callback = await _rain_switch_callback(_device())
    await callback(MowerRainDelayEvent(enabled=enabled, delay=180))

    assert entity.is_on is enabled


@pytest.mark.parametrize(
    ("call", "expected"), [("async_turn_on", 1), ("async_turn_off", 0)]
)
async def test_the_rain_switch_carries_the_delay_along(
    call: str, expected: int
) -> None:
    """setRainDelay wants the pair, so the toggle has to resend the duration.

    Sending the toggle alone is the one way this entity could silently reset
    the owner's three-hour hold, which is why the value is read back off the
    last event rather than defaulted.
    """
    from custom_components.ecovacs_mower.deebot_patch.commands import SetRainDelay
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    device = _device()
    entity, callback = await _rain_switch_callback(device)
    await callback(MowerRainDelayEvent(enabled=not expected, delay=180))

    await getattr(entity, call)()

    (command,) = device.execute_command.call_args.args
    assert isinstance(command, SetRainDelay)
    assert command._args == {"enable": expected, "delay": 180}


async def test_the_rain_switch_requests_a_refresh_after_writing() -> None:
    """The device pushing ``onRainDelay`` on its own answer is unconfirmed.

    Asking for a refresh is what re-reads the state if that push never comes,
    instead of leaving the frontend's optimistic value to flip back once its
    timeout expires (issue #42).
    """
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    device = _device()
    entity, callback = await _rain_switch_callback(device)
    await callback(MowerRainDelayEvent(enabled=False, delay=180))

    await entity.async_turn_on()

    device.events.request_refresh.assert_called_once_with(MowerRainDelayEvent)


async def test_the_rain_switch_refuses_to_write_before_it_knows_the_delay() -> None:
    """A payload can carry ``enable`` without ``delay``; guessing one is not on.

    Any default here is a number the owner did not choose, written to the
    device as if they had.
    """
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    device = _device()
    entity, callback = await _rain_switch_callback(device)
    await callback(MowerRainDelayEvent(enabled=True, delay=None))

    with pytest.raises(HomeAssistantError):
        await entity.async_turn_off()
    device.execute_command.assert_not_called()


def _animal_switch(device):
    from custom_components.ecovacs_mower.switch import EcovacsAnimalProtectionSwitch

    entity = EcovacsAnimalProtectionSwitch(device)
    entity.async_write_ha_state = lambda: None
    return entity


async def _animal_switch_callback(device):
    """Run async_added_to_hass and return the MowerAnimProtectEvent callback."""
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerAnimProtectEvent,
    )

    entity = _animal_switch(device)
    await entity.async_added_to_hass()

    for call in device.events.subscribe.call_args_list:
        event_type, callback = call.args
        if event_type is MowerAnimProtectEvent:
            return entity, callback
    raise AssertionError("The animal switch never subscribed to MowerAnimProtectEvent")


def _window(enabled: bool, start=(19, 0), end=(7, 0)):
    from datetime import time

    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerAnimProtectEvent,
    )

    return MowerAnimProtectEvent(
        enabled=enabled,
        start=time(*start) if start else None,
        end=time(*end) if end else None,
    )


def test_the_animal_switch_is_a_config_entity_disabled_by_default() -> None:
    """Same treatment as every other settings switch."""
    from homeassistant.const import EntityCategory

    from custom_components.ecovacs_mower.switch import (
        ENTITY_DESCRIPTIONS,
        EcovacsAnimalProtectionSwitch,
    )

    description = EcovacsAnimalProtectionSwitch.entity_description
    assert description.key == "animal_protection"
    assert description.key not in {d.key for d in ENTITY_DESCRIPTIONS}
    assert description.entity_category is EntityCategory.CONFIG
    assert description.entity_registry_enabled_default is False


def test_the_animal_switch_is_unknown_before_the_first_event() -> None:
    assert _animal_switch(_device()).is_on is None


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_animal_switch_follows_the_setting(enabled: bool) -> None:
    entity, callback = await _animal_switch_callback(_device())
    await callback(_window(enabled))

    assert entity.is_on is enabled


async def test_the_animal_switch_shows_the_window_padded() -> None:
    """As the app shows it, whichever form the mower sent."""
    entity, callback = await _animal_switch_callback(_device())
    await callback(_window(True, start=(19, 0), end=(7, 5)))

    assert entity.extra_state_attributes == {"start": "19:00", "end": "07:05"}


@pytest.mark.parametrize(
    ("call", "expected"), [("async_turn_on", 1), ("async_turn_off", 0)]
)
async def test_the_animal_switch_carries_the_window_along(
    call: str, expected: int
) -> None:
    """setAnimProtect wants all three fields, so the toggle resends the window.

    Sending a default window instead is the one way this entity could silently
    move the owner's nightly protection.
    """
    from custom_components.ecovacs_mower.deebot_patch.commands import SetAnimProtect

    device = _device()
    entity, callback = await _animal_switch_callback(device)
    await callback(_window(not expected, start=(21, 30), end=(6, 0)))

    await getattr(entity, call)()

    (command,) = device.execute_command.call_args.args
    assert isinstance(command, SetAnimProtect)
    assert command._args == {"enable": expected, "start": "21:30", "end": "6:0"}


async def test_the_animal_switch_requests_a_refresh_after_writing() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerAnimProtectEvent,
    )

    device = _device()
    entity, callback = await _animal_switch_callback(device)
    await callback(_window(False))

    await entity.async_turn_on()

    device.events.request_refresh.assert_called_once_with(MowerAnimProtectEvent)


@pytest.mark.parametrize(("start", "end"), [(None, (7, 0)), ((19, 0), None)])
async def test_the_animal_switch_refuses_to_write_without_the_window(
    start, end
) -> None:
    from homeassistant.exceptions import HomeAssistantError

    device = _device()
    entity, callback = await _animal_switch_callback(device)
    await callback(_window(True, start=start, end=end))

    with pytest.raises(HomeAssistantError):
        await entity.async_turn_off()
    device.execute_command.assert_not_called()


# --- Edge cutting (issue #166) ------------------------------------------------


def _edge_switch(device):
    from custom_components.ecovacs_mower.switch import EcovacsEdgeCuttingSwitch

    entity = EcovacsEdgeCuttingSwitch(device)
    entity.async_write_ha_state = lambda: None
    return entity


def _report_edge(device, data: dict) -> None:
    """Let the mower report its edge setting, as a push or an answer does."""
    from custom_components.ecovacs_mower.deebot_patch.messages import OnBorderSwitch

    OnBorderSwitch.handle(device.events, {"header": {}, "body": {"data": data}})


async def _edge_switch_callback(device):
    """Run async_added_to_hass and return the MowerBorderSwitchEvent callback."""
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerBorderSwitchEvent,
    )

    entity = _edge_switch(device)
    await entity.async_added_to_hass()

    for call in device.events.subscribe.call_args_list:
        event_type, callback = call.args
        if event_type is MowerBorderSwitchEvent:
            return entity, callback
    raise AssertionError("The edge switch never subscribed to MowerBorderSwitchEvent")


def _mower(class_: str):
    from unittest.mock import MagicMock

    from deebot_client.capabilities import DeviceType

    device = MagicMock()
    device.capabilities.device_type = DeviceType.MOWER
    device.device_info = {"did": f"did-{class_}", "class": class_}
    return device


async def _set_up_switches_for(*classes: str, registered: tuple[str, ...] = ()):
    """Run the platform setup for one mower per class.

    ``registered`` are the unique ids the entity registry already holds an
    *Edge cutting* switch under. Returns the entities added and the entity ids
    removed from the registry.
    """
    from unittest.mock import MagicMock, patch

    from custom_components.ecovacs_mower import switch

    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda domain, platform, unique_id: (
        f"switch.{unique_id}"
        if (domain, platform) == ("switch", "ecovacs_mower")
        and unique_id in registered
        else None
    )
    config_entry = MagicMock()
    config_entry.runtime_data.devices = [_mower(class_) for class_ in classes]
    added: list = []
    # The capability-backed switches need a real Capabilities to walk; they
    # are not what this is about.
    with (
        patch.object(switch, "get_supported_entities", return_value=[]),
        patch.object(switch.er, "async_get", return_value=registry),
    ):
        await switch.async_setup_entry(MagicMock(), config_entry, added.extend)
    removed = [call.args[0] for call in registry.async_remove.call_args_list]
    return added, removed


async def _edge_switches_added_for(*classes: str) -> list[str]:
    from custom_components.ecovacs_mower import switch

    added, _removed = await _set_up_switches_for(*classes)
    return [
        e.unique_id for e in added if isinstance(e, switch.EcovacsEdgeCuttingSwitch)
    ]


async def test_a_class_that_lost_the_edge_switch_loses_its_registry_entry() -> None:
    """Otherwise an enabled one would sit unavailable for good.

    Only the edge switch, only where it is no longer built: the G1-800 keeps
    its entry, and a mower that never enabled the switch has nothing to remove.
    """
    _added, removed = await _set_up_switches_for(
        "77atlz",
        "xmp9ds",
        "e4gqia",
        "2i0fns",
        registered=(
            "did-77atlz_border_switch",
            "did-xmp9ds_border_switch",
            "did-e4gqia_border_switch",
        ),
    )

    assert removed == [
        "switch.did-xmp9ds_border_switch",
        "switch.did-e4gqia_border_switch",
    ]


def test_the_edge_switch_keeps_the_key_the_library_switch_had() -> None:
    """The unique id is ``<did>_<key>``: a new key would be a new entity.

    An owner who enabled *Edge cutting* before keeps that entity, with its id
    and history, only if the key stays ``border_switch``.
    """
    from homeassistant.const import EntityCategory

    from custom_components.ecovacs_mower.switch import (
        ENTITY_DESCRIPTIONS,
        EcovacsEdgeCuttingSwitch,
    )

    description = EcovacsEdgeCuttingSwitch.entity_description
    assert description.key == "border_switch"
    assert description.key not in {d.key for d in ENTITY_DESCRIPTIONS}
    assert description.entity_category is EntityCategory.CONFIG
    assert description.entity_registry_enabled_default is False
    assert _edge_switch(_device()).unique_id == "test-did_border_switch"


async def test_the_edge_switch_exists_only_for_a_class_with_the_flag() -> None:
    """The G1-800 has it. The classes with a mode, or with no setting, do not."""
    assert await _edge_switches_added_for(
        "77atlz", "xmp9ds", "2px96q", "e4gqia", "2i0fns", "not-a-real-class"
    ) == ["did-77atlz_border_switch"]


def test_the_edge_switch_is_unknown_before_the_first_event() -> None:
    assert _edge_switch(_device()).is_on is None


@pytest.mark.parametrize("enabled", [True, False, None])
async def test_the_edge_switch_follows_enable(enabled) -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerBorderSwitchEvent,
    )

    entity, callback = await _edge_switch_callback(_device())
    await callback(MowerBorderSwitchEvent(enabled=enabled, mode=0))

    assert entity.is_on is enabled


@pytest.mark.parametrize("mode", [0, 1, 2])
@pytest.mark.parametrize(
    ("call", "expected"), [("async_turn_on", 1), ("async_turn_off", 0)]
)
async def test_the_edge_switch_resends_the_reported_mode(
    call: str, expected: int, mode: int
) -> None:
    """One write, both fields: the mode is the mower's, never a fixed 0."""
    from custom_components.ecovacs_mower.deebot_patch.commands import (
        SetBorderSwitchMower,
    )

    device = _device()
    entity = _edge_switch(device)
    _report_edge(device, {"enable": 1 - expected, "mode": mode})

    await getattr(entity, call)()

    (command,) = device.execute_command.call_args.args
    assert isinstance(command, SetBorderSwitchMower)
    assert command._args == {"enable": expected, "mode": mode}


async def test_the_edge_switch_writes_from_the_report_not_from_its_own_event() -> None:
    """The entity was shown mode 0, the mower has since reported mode 2.

    That is what a repeat the bus dropped looks like from the entity's side.
    The write has to carry what the handler recorded.
    """
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerBorderSwitchEvent,
    )

    device = _device()
    entity, callback = await _edge_switch_callback(device)
    await callback(MowerBorderSwitchEvent(enabled=True, mode=0))
    _report_edge(device, {"enable": 1, "mode": 2})

    await entity.async_turn_off()

    (command,) = device.execute_command.call_args.args
    assert command._args == {"enable": 0, "mode": 2}


@pytest.mark.parametrize("reported", [None, {"enable": 1}])
async def test_the_edge_switch_refuses_to_write_without_a_mode(reported) -> None:
    from homeassistant.exceptions import HomeAssistantError

    device = _device()
    entity = _edge_switch(device)
    if reported is not None:
        _report_edge(device, reported)

    with pytest.raises(HomeAssistantError, match="edge mode"):
        await entity.async_turn_off()
    device.execute_command.assert_not_called()


async def test_the_edge_switch_reads_back_after_a_confirmed_write() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerBorderSwitchEvent,
    )

    device = _device()
    entity = _edge_switch(device)
    _report_edge(device, {"enable": 1, "mode": 0})

    await entity.async_turn_off()

    device.events.request_refresh.assert_called_once_with(MowerBorderSwitchEvent)


async def test_an_unconfirmed_edge_write_changes_nothing() -> None:
    """The state and the base of the next write stay what the mower reported."""
    from unittest.mock import AsyncMock

    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerBorderSwitchEvent,
        border_switch_for,
    )

    device = _device()
    device.execute_command = AsyncMock(return_value={})
    entity, callback = await _edge_switch_callback(device)
    _report_edge(device, {"enable": 1, "mode": 1})
    await callback(MowerBorderSwitchEvent(enabled=True, mode=1))

    await entity.async_turn_off()

    assert entity.is_on is True
    assert border_switch_for(device.events) == (True, 1)
    device.events.request_refresh.assert_not_called()
