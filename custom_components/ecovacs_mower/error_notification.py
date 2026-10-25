"""The persistent notification that asks for the app's wording of an unknown code.

Issue #133. The part that touches Home Assistant; what the notification says and
when it opens or closes is ``error_report.py``.

One notification per mower under a fixed id, rewritten in place as the burst
goes on, the way the area-parameter log (``debug.py``) does it. It is owned by
``EcovacsController``, like the fault latch, and for the same reason: an entity
can be disabled in the entity registry, and the one moment this is needed is a
fault nobody was watching.

Dismissing the notification is how the owner says they have read it. The next
unknown code after that starts a fresh one, which is why this listens for the
removal instead of keeping the notification open for the life of the process.
"""

from __future__ import annotations

import logging

from deebot_client.device import Device
from homeassistant.components import persistent_notification
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .deebot_patch.messages import MowerErrorCodesEvent
from .error_report import ErrorReport

_LOGGER = logging.getLogger(__name__)


class UnknownErrorNotifier:
    """Keep one mower's unknown-error notification."""

    def __init__(self, hass: HomeAssistant, device: Device) -> None:
        """Remember the mower; nothing is subscribed until ``subscribe``."""
        self._hass = hass
        self._device = device
        info = device.device_info
        label = f"{info.get('nick') or info['class']} ({info['class']})"
        self._title = f"Unknown error code on {label}"
        self._notification_id = f"{DOMAIN}_unknown_error_{info['did']}"
        self._report = ErrorReport(label)
        self._unsubs: list[CALLBACK_TYPE] = []

    def subscribe(self) -> None:
        """Listen for the mower's error pushes, and for the notification going away.

        Eager, like the fault latch: ``EventBus.notify`` drops an event nobody
        is subscribed to, so a burst before a late subscription would be lost.
        """
        self._unsubs = [
            self._device.events.subscribe(MowerErrorCodesEvent, self._on_codes),
            persistent_notification.async_register_callback(
                self._hass, self._on_notifications
            ),
        ]

    @callback
    def shutdown(self) -> None:
        """Stop listening. A showing notification is left for the owner to read."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    async def _on_codes(self, event: MowerErrorCodesEvent) -> None:
        """Open the report on an unknown code, and write every push once open."""
        if not self._report.add(event.codes, dt_util.now()):
            return
        persistent_notification.async_create(
            self._hass,
            self._report.render(),
            title=self._title,
            notification_id=self._notification_id,
        )

    @callback
    def _on_notifications(
        self,
        update_type: persistent_notification.UpdateType,
        notifications: dict[str, persistent_notification.Notification],
    ) -> None:
        """Close the report when its notification is dismissed."""
        if (
            update_type is persistent_notification.UpdateType.REMOVED
            and self._notification_id in notifications
        ):
            _LOGGER.debug("The unknown-error notification was dismissed")
            self._report.close()
