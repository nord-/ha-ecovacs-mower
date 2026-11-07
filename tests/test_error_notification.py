"""The persistent notification behind unknown error codes (issue #133).

Imports Home Assistant (persistent_notification), so the file is marked
requires_ha and the imports live inside the tests. No test needs the ``hass``
fixture: the notification calls are patched at the module that makes them and
the dismissal is delivered by calling the callback the notifier registers, so
what is under test is this integration's logic and not Home Assistant's.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from . import requires_ha

pytestmark = requires_ha

_MODULE = "custom_components.ecovacs_mower.error_notification"
_CREATE = f"{_MODULE}.persistent_notification.async_create"
_REGISTER = f"{_MODULE}.persistent_notification.async_register_callback"

_KNOWN = 622
_UNKNOWN = 999


def _device():
    from deebot_client.event_bus import EventBus

    device = MagicMock()
    device.device_info = {"did": "did", "class": "2i0fns", "nick": "Garden"}
    device.events = EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _: []))
    return device


async def _settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


def _push(device, *codes: int) -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerErrorCodesEvent,
    )

    device.events.notify(MowerErrorCodesEvent(codes))


def _notifier(device):
    from custom_components.ecovacs_mower.error_notification import (
        UnknownErrorNotifier,
    )

    notifier = UnknownErrorNotifier(MagicMock(), device)
    notifier.subscribe()
    return notifier


async def test_an_unknown_code_writes_one_notification_for_the_mower() -> None:
    device = _device()

    with patch(_CREATE) as create, patch(_REGISTER):
        notifier = _notifier(device)
        _push(device, _UNKNOWN)
        await _settle()

    create.assert_called_once()
    assert create.call_args.kwargs == {
        "title": "Unknown error code on Garden (2i0fns)",
        "notification_id": "ecovacs_mower_unknown_error_did",
    }
    assert "999 (unknown)" in create.call_args.args[1]
    notifier.shutdown()


async def test_known_codes_alone_write_nothing() -> None:
    device = _device()

    with patch(_CREATE) as create, patch(_REGISTER):
        notifier = _notifier(device)
        _push(device, _KNOWN)
        await _settle()

    create.assert_not_called()
    notifier.shutdown()


async def test_every_following_push_rewrites_the_same_notification() -> None:
    device = _device()

    with patch(_CREATE) as create, patch(_REGISTER):
        notifier = _notifier(device)
        _push(device, _KNOWN)
        await _settle()
        _push(device, _KNOWN, _UNKNOWN)
        await _settle()
        _push(device, _UNKNOWN, _KNOWN)
        await _settle()

    assert create.call_count == 2
    ids = {call.kwargs["notification_id"] for call in create.call_args_list}
    assert ids == {"ecovacs_mower_unknown_error_did"}
    message = create.call_args.args[1]
    # Both pushes are there, oldest first, with the order inside each kept.
    assert message.index("622 (GOAT tilted.), 999 (unknown)") < message.index(
        "999 (unknown), 622 (GOAT tilted.)"
    )
    notifier.shutdown()


async def test_dismissing_it_ends_the_report_and_the_next_unknown_code_starts_one() -> (
    None
):
    from homeassistant.components import persistent_notification

    device = _device()
    removed = persistent_notification.UpdateType.REMOVED

    with patch(_CREATE) as create, patch(_REGISTER):
        notifier = _notifier(device)
        _push(device, _UNKNOWN)
        await _settle()
        notifier._on_notifications(removed, {"ecovacs_mower_unknown_error_did": Mock()})
        _push(device, _KNOWN)
        await _settle()
        assert create.call_count == 1

        _push(device, _UNKNOWN)
        await _settle()

    assert create.call_count == 2
    # A fresh report: the first push's line is gone.
    assert create.call_args.args[1].count("999 (unknown)") == 1
    notifier.shutdown()


async def test_another_notification_being_dismissed_changes_nothing() -> None:
    from homeassistant.components import persistent_notification

    device = _device()
    removed = persistent_notification.UpdateType.REMOVED

    with patch(_CREATE) as create, patch(_REGISTER):
        notifier = _notifier(device)
        _push(device, _UNKNOWN)
        await _settle()
        notifier._on_notifications(removed, {"some_other_notification": Mock()})
        _push(device, _KNOWN)
        await _settle()

    # Still open: the known code was listed, not ignored.
    assert create.call_count == 2
    notifier.shutdown()


async def test_shutdown_stops_listening() -> None:
    device = _device()
    unsub_dismiss = Mock()

    with patch(_CREATE) as create, patch(_REGISTER, return_value=unsub_dismiss):
        notifier = _notifier(device)
        notifier.shutdown()
        _push(device, _UNKNOWN)
        await _settle()

    unsub_dismiss.assert_called_once()
    create.assert_not_called()
