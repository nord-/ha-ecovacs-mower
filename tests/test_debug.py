"""The area-parameter record behind the "Log area parameters" button.

Imports Home Assistant (async_call_later, persistent_notification), so the
file is marked requires_ha and the imports live inside the tests. The source
of truth is CI.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from . import requires_ha

pytestmark = requires_ha

_NOTIFY = "custom_components.ecovacs_mower.debug.persistent_notification.async_create"


def _device():
    from deebot_client.event_bus import EventBus

    device = MagicMock()
    device.device_info = {"did": "did", "class": "abc123", "nick": "GOAT"}
    device.events = EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _: []))
    return device


def _event(*areas):
    from custom_components.ecovacs_mower.deebot_patch.areas import (
        MowerArea,
        MowerAreaEvent,
    )

    return MowerAreaEvent(tuple(MowerArea(**area) for area in areas))


_FRONT = {"area_id": "1", "name": "Front", "mow_height_level": 6, "cut_mode": 7,
          "obstacle_height": 1, "angle": 227}
_BACK = {"area_id": "2", "mow_height_level": 3, "cut_mode": 7,
         "obstacle_height": 1, "angle": 90}


def _answering(device, event):
    """An execute that answers getAreaParameter with *event*, like the mower."""
    sent = []

    async def execute(command):
        sent.append(command)
        if command.NAME == "getAreaParameter":
            device.events.notify(event)
        return True

    return execute, sent


async def _settle() -> None:
    # Bus callbacks run as tasks; a few loop turns let them all finish.
    for _ in range(5):
        await asyncio.sleep(0)


def _shown(notify: Mock) -> str:
    """The message of the notification as last written."""
    return notify.call_args.args[1]


async def test_a_press_shows_every_area_as_a_table(hass) -> None:
    from custom_components.ecovacs_mower.debug import AreaParameterLog

    device = _device()
    execute, sent = _answering(device, _event(_FRONT, _BACK))
    log = AreaParameterLog(hass, device, execute)

    with patch(_NOTIFY) as notify:
        await log.async_start()
        await _settle()

    message = _shown(notify)
    assert notify.call_args.kwargs == {
        "title": "Area parameters: GOAT (abc123)",
        "notification_id": "ecovacs_mower_area_parameters_did",
    }
    assert "| Area | mow_height_level | cut_mode | obstacle_height | angle |" in message
    # Ordered by id; an unnamed area is still shown, by id alone.
    assert message.index("| Area 1 (Front) | 6 | 7 | 1 | 227 |") < message.index(
        "| Area 2 | 3 | 7 | 1 | 90 |"
    )
    assert "Changes" not in message
    assert [command.NAME for command in sent] == ["getAreaParameter"]
    log.async_shutdown()


async def test_each_change_is_listed_and_the_table_follows(hass) -> None:
    from custom_components.ecovacs_mower.debug import AreaParameterLog

    device = _device()
    execute, _ = _answering(device, _event(_FRONT, _BACK))
    log = AreaParameterLog(hass, device, execute)

    with patch(_NOTIFY) as notify:
        await log.async_start()
        await _settle()
        # What onAreaParameter produces when the app changes one value, twice.
        device.events.notify(_event({**_FRONT, "mow_height_level": 7}, _BACK))
        await _settle()
        device.events.notify(_event({**_FRONT, "mow_height_level": 8}, _BACK))
        await _settle()

    message = _shown(notify)
    assert "| Area 1 (Front) | 8 | 7 | 1 | 227 |" in message
    changes = [line for line in message.splitlines() if line.startswith("- ")]
    assert [line.split(" ", 2)[2] for line in changes] == [
        "Area 1 (Front): mow_height_level 6 → 7",
        "Area 1 (Front): mow_height_level 7 → 8",
    ]
    log.async_shutdown()


async def test_areas_that_come_and_go_are_listed(hass) -> None:
    from custom_components.ecovacs_mower.debug import AreaParameterLog

    device = _device()
    execute, _ = _answering(device, _event(_FRONT, _BACK))
    log = AreaParameterLog(hass, device, execute)

    with patch(_NOTIFY) as notify:
        await log.async_start()
        await _settle()
        device.events.notify(_event(_FRONT))
        await _settle()
        device.events.notify(_event(_FRONT, _BACK))
        await _settle()

    message = _shown(notify)
    assert "Area 2: no longer reported" in message
    assert "Area 2: new, mow_height_level=3, cut_mode=7" in message
    log.async_shutdown()


async def test_the_change_list_keeps_the_latest(hass) -> None:
    from custom_components.ecovacs_mower.debug import MAX_CHANGES, AreaParameterLog

    device = _device()
    execute, _ = _answering(device, _event({**_FRONT, "angle": 0}))
    log = AreaParameterLog(hass, device, execute)

    with patch(_NOTIFY) as notify:
        await log.async_start()
        await _settle()
        for angle in range(1, MAX_CHANGES + 6):
            device.events.notify(_event({**_FRONT, "angle": angle}))
            await _settle()

    changes = [line for line in _shown(notify).splitlines() if line.startswith("- ")]
    assert len(changes) == MAX_CHANGES
    assert changes[-1].endswith(f"angle {MAX_CHANGES + 4} → {MAX_CHANGES + 5}")
    log.async_shutdown()


async def test_the_names_are_asked_for_once_the_map_is_known(hass) -> None:
    from custom_components.ecovacs_mower.debug import AreaParameterLog
    from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
        register,
    )

    device = _device()
    register(device.events).note_map("1")
    execute, sent = _answering(device, _event(_FRONT))
    log = AreaParameterLog(hass, device, execute)

    with patch(_NOTIFY):
        await log.async_start()

    assert [command.NAME for command in sent] == ["getAreaParameter", "getAreaSet"]
    assert sent[1]._args["mid"] == "1"
    log.async_shutdown()


async def test_missing_names_and_a_silent_mower_are_explained(hass) -> None:
    from custom_components.ecovacs_mower.debug import AreaParameterLog

    log = AreaParameterLog(hass, _device(), AsyncMock(return_value=False))

    with patch(_NOTIFY) as notify:
        await log.async_start()

    message = _shown(notify)
    assert "did not answer getAreaParameter" in message
    assert "Area names are unknown until the mower names its map" in message
    assert "Waiting for the mower to report its areas." in message
    log.async_shutdown()


async def test_following_stops_when_the_window_closes(hass) -> None:
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.ecovacs_mower.debug import LOG_WINDOW, AreaParameterLog

    device = _device()
    execute, _ = _answering(device, _event(_FRONT))
    log = AreaParameterLog(hass, device, execute)

    with patch(_NOTIFY) as notify:
        await log.async_start()
        await _settle()
        async_fire_time_changed(hass, dt_util.utcnow() + LOG_WINDOW)
        await hass.async_block_till_done()
        written = notify.call_count
        device.events.notify(_event({**_FRONT, "angle": 0}))
        await _settle()

    # The closing rewrite says so, and nothing after it is recorded.
    assert _shown(notify).startswith("Stopped.")
    assert notify.call_count == written


async def test_a_second_press_starts_over(hass) -> None:
    from custom_components.ecovacs_mower.debug import AreaParameterLog

    device = _device()
    current = [_event(_FRONT)]

    async def execute(command):
        # The mower answers with what it has now, the change included.
        if command.NAME == "getAreaParameter":
            device.events.notify(current[0])
        return True

    log = AreaParameterLog(hass, device, execute)

    with patch(_NOTIFY) as notify:
        await log.async_start()
        await _settle()
        current[0] = _event({**_FRONT, "angle": 0})
        device.events.notify(current[0])
        await _settle()
        # The same answer again is dropped by the bus as a repeat; the table
        # comes from the last event the bus hands a new subscriber.
        await log.async_start()
        await _settle()

    message = _shown(notify)
    assert "| Area 1 (Front) | 6 | 7 | 1 | 0 |" in message
    assert "Changes" not in message
    log.async_shutdown()


async def test_the_button_starts_the_log_and_stops_it_on_removal(hass) -> None:
    from custom_components.ecovacs_mower.button import (
        DEBUG_DESCRIPTIONS,
        EcovacsDebugButtonEntity,
        EcovacsDebugButtonEntityDescription,
    )

    log = Mock(async_start=AsyncMock())
    description = EcovacsDebugButtonEntityDescription(
        key=DEBUG_DESCRIPTIONS[0].key,
        translation_key=DEBUG_DESCRIPTIONS[0].translation_key,
        log_fn=lambda hass, device, execute: log,
    )
    entity = EcovacsDebugButtonEntity(_device(), description)
    entity.hass = hass
    entity._subscribe = Mock()  # no availability wiring needed here
    entity.async_on_remove = Mock()

    await entity.async_added_to_hass()
    await entity.async_press()

    log.async_start.assert_awaited_once_with()
    # Stopped with the entity, so a removed button leaves no subscription.
    entity.async_on_remove.assert_called_once_with(log.async_shutdown)


async def test_the_areas_are_asked_again_for_a_mower_that_does_not_push(hass) -> None:
    """The onAreaParameter push has only been seen on one model."""
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.ecovacs_mower.debug import POLL_INTERVAL, AreaParameterLog

    device = _device()
    current = [_event(_FRONT)]
    sent = []

    async def execute(command):
        # A mower that only answers: nothing changes unless it is asked.
        sent.append(command.NAME)
        if command.NAME == "getAreaParameter":
            device.events.notify(current[0])
        return True

    log = AreaParameterLog(hass, device, execute)
    with patch(_NOTIFY) as notify:
        await log.async_start()
        await _settle()
        current[0] = _event({**_FRONT, "cut_mode": 6})
        async_fire_time_changed(hass, dt_util.utcnow() + POLL_INTERVAL)
        await hass.async_block_till_done()
        await _settle()

    assert sent == ["getAreaParameter", "getAreaParameter"]
    assert "Area 1 (Front): cut_mode 7 → 6" in _shown(notify)
    log.async_shutdown()


async def test_removing_the_button_mid_window_says_it_stopped(hass) -> None:
    """Otherwise the notification keeps claiming to follow changes."""
    from custom_components.ecovacs_mower.debug import AreaParameterLog

    device = _device()
    execute, sent = _answering(device, _event(_FRONT))
    log = AreaParameterLog(hass, device, execute)

    with patch(_NOTIFY) as notify:
        await log.async_start()
        await _settle()
        log.async_shutdown()
        written = notify.call_count
        # Nothing is followed or asked any more, and a second shutdown is quiet.
        device.events.notify(_event({**_FRONT, "angle": 0}))
        await _settle()
        log.async_shutdown()

    assert _shown(notify).startswith("Stopped.")
    assert notify.call_count == written
    assert [c.NAME for c in sent] == ["getAreaParameter"]
