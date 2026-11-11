"""Ecovacs select module.

Not forked from Home Assistant core: the core integration's selects (water
amount, work mode) are all vacuum settings, so nothing of that file is kept.
The one select here is the mower's edge mode (issue #166).
"""

from typing import override

from deebot_client.capabilities import Capabilities, DeviceType
from deebot_client.device import Device

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
from .deebot_patch.commands import SetBorderSwitchMower
from .deebot_patch.hardware import profile_for_class
from .deebot_patch.messages import MowerBorderSwitchEvent
from .entity import EcovacsEntity

# The raw ``mode`` of ``setBorderSwitch`` and the option it is shown as. The
# three values are what the A1600 RTK's app writes for its three choices (issue
# #23) and the O800 RTK's app for the same three
# (DeebotUniverse/client.py#1949). A class gets this select only through the
# ``edge_mode`` profile flag, which is the claim that the values mean this on
# that class.
EDGE_MODE_OPTIONS: dict[int, str] = {
    0: "along_boundary",
    1: "edge_mowing",
    2: "cross_boundary",
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data
    entities = [
        EcovacsEdgeModeSelect(device)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
        and (profile := profile_for_class(device.device_info["class"])) is not None
        and profile.edge_mode
    ]
    if entities:
        async_add_entities(entities)


class EcovacsEdgeModeSelect(
    EcovacsEntity[Capabilities],
    SelectEntity,
):
    """How the mower treats the edge of the lawn in an ordinary job.

    The ``mode`` half of the setting behind ``getBorderSwitch`` and
    ``setBorderSwitch``, for a class whose app offers the three modes and no
    full off (issue #166). The ``enable`` half is not shown: a write sends
    back the one the mower last reported, read from the patch layer and not
    from this entity's own copy of the last event.

    A mode outside the three known ones reads as unknown. It stays on the
    mower until the owner picks one of the three here.

    Disabled by default, like the settings switches.
    """

    entity_description = SelectEntityDescription(
        key="edge_mode",
        translation_key="edge_mode",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    )
    _attr_options = list(EDGE_MODE_OPTIONS.values())
    _attr_current_option: str | None = None

    def __init__(self, device: Device) -> None:
        """Initialize entity."""
        super().__init__(device, device.capabilities)

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: MowerBorderSwitchEvent) -> None:
            self._attr_current_option = (
                None if event.mode is None else EDGE_MODE_OPTIONS.get(event.mode)
            )
            self.async_write_ha_state()

        self._subscribe(MowerBorderSwitchEvent, on_event)

    @override
    async def async_select_option(self, option: str) -> None:
        """Write the chosen mode, with the toggle the mower last reported.

        Refused while the toggle is unknown: a made-up ``enable`` could
        switch edge mowing off on a mower whose app has no way to say so. The
        state follows the mower's own report, as on the edge cutting switch;
        the read after a confirmed write is what delivers it on a class that
        does not push the change.
        """
        mode = next(raw for raw, name in EDGE_MODE_OPTIONS.items() if name == option)
        command = SetBorderSwitchMower.from_reported(self._device.events, mode=mode)
        if command is None:
            raise HomeAssistantError(
                "The mower has not reported its edge setting, so the edge mode "
                "cannot be changed without overwriting it. Wait for the mower "
                "to report it, or change the mode in the Ecovacs app"
            )

        if await self._execute_command(command):
            self._device.events.request_refresh(MowerBorderSwitchEvent)
