"""Controller module.

Forked from Home Assistant core (``homeassistant/components/ecovacs/controller.py``).
Support for XMPP-connected devices and its dependency on the legacy client
library has been removed: this integration only supports MQTT.
"""

import asyncio
from collections.abc import Callable, Mapping
from functools import partial
import logging
import ssl
from typing import Any

from deebot_client.api_client import ApiClient
from deebot_client.authentication import create_rest_config
from deebot_client.capabilities import DeviceType
from deebot_client.const import UNDEFINED, UndefinedType
from deebot_client.device import Device
from deebot_client.exceptions import (
    DeebotError,
    DeviceVerificationRequiredError,
    InvalidAuthenticationError,
)
from deebot_client.events.map import PositionsEvent
from deebot_client.mqtt_client import MqttClient, create_mqtt_config
from deebot_client.rs.map import PositionType
from deebot_client.util import md5

from homeassistant.const import (
    CONF_COUNTRY,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.storage import Store
from homeassistant.util.ssl import get_default_no_verify_context

from .const import (
    CONF_CREDENTIALS,
    CONF_OVERRIDE_MQTT_URL,
    CONF_OVERRIDE_REST_URL,
    CONF_VERIFY_MQTT_CERTIFICATE,
    DOMAIN,
    ISSUE_TRACKER_URL,
)
from .deebot_patch import (
    AccountAuthenticator,
    PatchContractError,
    apply as apply_deebot_patch,
    verify_capabilities,
)
from .deebot_patch.hardware import SUPPORTED_CLASSES, patch_device_info
from .deebot_patch.map_messages import (
    MowerCoverageEvent,
    MowerMapInfoEvent,
    MowerNoGoZonesEvent,
    MowerObstaclesEvent,
)
from .map import MowerMap

_LOGGER = logging.getLogger(__name__)

MAP_STORAGE_VERSION = 1
MAP_SAVE_DELAY = 30  # seconds; Store.async_delay_save flushes at HA stop


def _map_store(hass: HomeAssistant, did: str) -> Store[dict[str, Any]]:
    """Return the per-device map Store, keyed the same way everywhere."""
    return Store(hass, MAP_STORAGE_VERSION, f"{DOMAIN}.map_{did}")


async def async_remove_map_store(hass: HomeAssistant, did: str) -> None:
    """Delete a device's persisted map store.

    Called from ``async_remove_entry`` when the config entry is deleted, so
    an unloaded device's ``.storage/ecovacs_mower.map_<did>`` file does not
    outlive the entry.
    """
    await _map_store(hass, did).async_remove()


class EcovacsController:
    """Ecovacs controller."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: Mapping[str, Any],
        on_account_credentials_changed: Callable[[dict[str, str]], None]
        | None = None,
    ) -> None:
        """Initialize controller."""
        self._hass = hass
        self._devices: list[Device] = []
        self.maps: dict[str, MowerMap] = {}
        self._map_stores: dict[str, Store[dict[str, Any]]] = {}
        rest_url = config.get(CONF_OVERRIDE_REST_URL)
        self._device_id = config[CONF_DEVICE_ID]
        country = config[CONF_COUNTRY]

        # Seeded with the account pair the config flow captured, so setup
        # renews the portal credentials without the password endpoint that
        # answers 1013 and would send the entry straight back into
        # reauthentication. See deebot_patch/authentication.py.
        #
        # The password fallback there can mint a *replacement* pair when the
        # stored one has gone stale; on_account_credentials_changed is how that
        # replacement reaches the config entry (async_setup_entry wires it to
        # async_update_entry), so a later reload does not pay a doomed token
        # login before falling back to the password again.
        try:
            self._authenticator = AccountAuthenticator(
                create_rest_config(
                    aiohttp_client.async_get_clientsession(self._hass),
                    device_id=self._device_id,
                    alpha_2_country=country,
                    override_rest_url=rest_url,
                ),
                config[CONF_USERNAME],
                md5(config[CONF_PASSWORD]),
                account_credentials=config.get(CONF_CREDENTIALS),
                on_account_credentials=on_account_credentials_changed,
            )
        except PatchContractError as ex:
            raise ConfigEntryError(str(ex)) from ex
        self._api_client = ApiClient(self._authenticator)

        mqtt_url = config.get(CONF_OVERRIDE_MQTT_URL)
        ssl_context: UndefinedType | ssl.SSLContext = UNDEFINED
        if not config.get(CONF_VERIFY_MQTT_CERTIFICATE, True) and mqtt_url:
            ssl_context = get_default_no_verify_context()

        self._mqtt_config_fn = partial(
            create_mqtt_config,
            device_id=self._device_id,
            country=country,
            override_mqtt_url=mqtt_url,
            ssl_context=ssl_context,
        )
        self._mqtt_client: MqttClient | None = None

    async def initialize(self) -> None:
        """Init controller."""
        try:
            # The patch must seed the cache BEFORE get_devices(), which bakes
            # the capabilities into DeviceInfo.static.
            apply_deebot_patch()
            for class_ in SUPPORTED_CLASSES:
                await patch_device_info(class_)

            devices = await self._api_client.get_devices()

            # Check the object the device actually got, not the cache.
            for info in devices.mqtt:
                device_class = info.api["class"]
                if device_class in SUPPORTED_CLASSES:
                    verify_capabilities(info.static.capabilities, device_class)
                elif info.static.capabilities.device_type is DeviceType.MOWER:
                    # Warning: all 25 MOWER classes in deebot-client 18.5.1
                    # carry the same CleanV2/GetCleanInfoV2 bugs, but
                    # SUPPORTED_CLASSES only covers the reported ones. Any other
                    # mower therefore gets an entity whose controls are dead and
                    # whose state lags — exactly the symptom this project exists
                    # to eliminate. That user should not have to read the debug
                    # log to understand why.
                    #
                    # The predicate is the same one lawn_mower.py uses to decide
                    # what becomes an entity, so the warning cannot false-alarm
                    # on a vacuum.
                    _LOGGER.warning(
                        "Mower class %s is not supported by this integration "
                        "and is used unpatched: controls will likely not work "
                        "and state will lag. Report the model at %s so it can "
                        "be added",
                        device_class,
                        ISSUE_TRACKER_URL,
                    )
                else:
                    # Debug, not warning: an ordinary Deebot vacuum on the same
                    # account lands here entirely correctly, unpatched. It
                    # becomes no entity and has no bug to fix — nothing to say.
                    _LOGGER.debug(
                        "Device class %s is not a mower and is used "
                        "unpatched, without capability verification",
                        device_class,
                    )

            if devices.mqtt:
                mqtt = await self._get_mqtt_client()
                mqtt_devices = [
                    Device(info, self._authenticator) for info in devices.mqtt
                ]
                async with asyncio.TaskGroup() as tg:

                    async def _init(device: Device) -> None:
                        """Initialize MQTT device."""
                        await device.initialize(mqtt)
                        self._devices.append(device)
                        if device.capabilities.device_type is DeviceType.MOWER:
                            # Map data is best effort; mower control is sacred.
                            # A failure here must not fail the TaskGroup and
                            # take mower control down with it.
                            did = device.device_info["did"]
                            try:
                                await self._setup_map(device)
                            except Exception:
                                _LOGGER.warning(
                                    "Map setup failed for %s; the map is "
                                    "unavailable but the mower remains "
                                    "controllable",
                                    did,
                                    exc_info=True,
                                )

                    for device in mqtt_devices:
                        tg.create_task(_init(device))

            for device_config in devices.not_supported:
                _LOGGER.warning(
                    (
                        'Device "%s" not supported. More information at '
                        "https://github.com/DeebotUniverse/client.py/issues/612: %s"
                    ),
                    device_config["deviceName"],
                    device_config,
                )

        except DeviceVerificationRequiredError as ex:
            raise ConfigEntryAuthFailed("Device verification required") from ex
        except InvalidAuthenticationError as ex:
            raise ConfigEntryAuthFailed("Invalid credentials") from ex
        except PatchContractError as ex:
            raise ConfigEntryError(str(ex)) from ex
        except DeebotError as ex:
            raise ConfigEntryNotReady("Error during setup") from ex

        _LOGGER.debug("Controller initialize complete")

    async def _setup_map(self, device: Device) -> None:
        """Restore the device's map and wire the events that feed it.

        Subscriptions are eager: EventBus.notify drops events without
        subscribers, so waiting for the image entity would lose the first
        messages of a session.
        """
        did = device.device_info["did"]
        store = _map_store(self._hass, did)
        mower_map = MowerMap()
        if (data := await store.async_load()) is not None:
            try:
                mower_map = MowerMap.from_dict(data)
            except (KeyError, TypeError, ValueError, IndexError):
                _LOGGER.warning(
                    "Discarding corrupt map store for %s; starting empty", did
                )
        self.maps[did] = mower_map
        self._map_stores[did] = store

        def save() -> None:
            store.async_delay_save(mower_map.as_dict, MAP_SAVE_DELAY)

        async def on_map_info(event: MowerMapInfoEvent) -> None:
            mower_map.update_map_info(
                event.boundary, event.zones, event.corridors
            )
            save()

        async def on_obstacles(event: MowerObstaclesEvent) -> None:
            mower_map.update_obstacles(event.obstacles)
            save()

        async def on_coverage(event: MowerCoverageEvent) -> None:
            mower_map.update_coverage(event.lanes)
            save()

        async def on_nogo(event: MowerNoGoZonesEvent) -> None:
            mower_map.update_nogo(event.zones)
            save()

        async def on_positions(event: PositionsEvent) -> None:
            # Position is volatile — no save. A valid charger position has
            # never been observed on the verified hardware, but if one
            # arrives it beats the origin assumption.
            for position in event.positions:
                if position.type is PositionType.DEEBOT:
                    mower_map.update_position(
                        position.x, position.y, position.a
                    )
                elif position.type is PositionType.CHARGER:
                    mower_map.dock = (position.x, position.y)

        device.events.subscribe(MowerMapInfoEvent, on_map_info)
        device.events.subscribe(MowerObstaclesEvent, on_obstacles)
        device.events.subscribe(MowerCoverageEvent, on_coverage)
        device.events.subscribe(MowerNoGoZonesEvent, on_nogo)
        device.events.subscribe(PositionsEvent, on_positions)

    async def teardown(self) -> None:
        """Disconnect controller."""
        for did, store in self._map_stores.items():
            # A failed save must never block device teardown or the MQTT
            # disconnect below: map is best effort, mower control is sacred.
            try:
                await store.async_save(self.maps[did].as_dict())
            except Exception:
                _LOGGER.warning(
                    "Failed to save map store for %s", did, exc_info=True
                )
        for device in self._devices:
            await device.teardown()
        if self._mqtt_client is not None:
            await self._mqtt_client.disconnect()
        await self._authenticator.teardown()

    async def _get_mqtt_client(self) -> MqttClient:
        """Return validated MQTT client."""
        if self._mqtt_client is None:
            config = await self._hass.async_add_executor_job(self._mqtt_config_fn)
            mqtt = MqttClient(config, self._authenticator)
            await mqtt.verify_config()
            self._mqtt_client = mqtt

        return self._mqtt_client

    @property
    def devices(self) -> list[Device]:
        """Return devices."""
        return self._devices
