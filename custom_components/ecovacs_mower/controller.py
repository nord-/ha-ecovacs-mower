"""Controller module.

Forked from Home Assistant core (``homeassistant/components/ecovacs/controller.py``).
Support for XMPP-connected devices and its dependency on the legacy client
library has been removed: this integration only supports MQTT.
"""

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
from functools import partial
import logging
import ssl
from typing import Any

from deebot_client.api_client import ApiClient
from deebot_client.authentication import create_rest_config
from deebot_client.capabilities import DeviceType
from deebot_client.const import UNDEFINED, UndefinedType
from deebot_client.device import Device
from deebot_client.events import NetworkInfoEvent, StateEvent, StatsEvent
from deebot_client.exceptions import (
    DeebotError,
    DeviceVerificationRequiredError,
    InvalidAuthenticationError,
)
from deebot_client.events.map import PositionsEvent
from deebot_client.models import State
from deebot_client.mqtt_client import MqttClient, create_mqtt_config
from deebot_client.rs.map import PositionType
from deebot_client.util import md5

from homeassistant.const import (
    CONF_COUNTRY,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util.ssl import get_default_no_verify_context

from .const import (
    APP_PING_INTERVAL,
    CONF_CREDENTIALS,
    CONF_OVERRIDE_MQTT_URL,
    CONF_OVERRIDE_REST_URL,
    CONF_VERIFY_MQTT_CERTIFICATE,
    DOMAIN,
    ISSUE_TRACKER_URL,
    POLL_INTERVAL,
)
from .deebot_patch import (
    AccountAuthenticator,
    PatchContractError,
    apply as apply_deebot_patch,
    register_mower_bus,
    verify_capabilities,
)
from .deebot_patch.areas import MowerAreaEvent
from .deebot_patch.commands import AppPing, GetMapTrack, GetPosMower
from .deebot_patch.hardware import (
    SUPPORTED_CLASSES,
    patch_device_info,
    profile_for_class,
)
from .deebot_patch.map_messages import (
    MowerCoverageEvent,
    MowerRemainingEvent,
    MowerCoveredAreaEvent,
    MowerMapInfoEvent,
    MowerNoGoZonesEvent,
    MowerObstaclesEvent,
)
from .fault import FaultLatch
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
        self.fault_latches: dict[str, FaultLatch] = {}
        self._map_stores: dict[str, Store[dict[str, Any]]] = {}
        self._unsub_polls: dict[str, CALLBACK_TYPE] = {}
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
                    # Warning: all 25 MOWER classes in deebot-client 18.6.0
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
                        if device.device_info["class"] in SUPPORTED_CLASSES:
                            # Before initialize(), not after: __init__ creates
                            # the event bus and initialize() starts the MQTT
                            # subscription, so a push arriving in between would
                            # find no record and be handled as an ordinary
                            # vacuum's. Unlike the fault latch below, this one
                            # cannot wait until the device is up.
                            #
                            # Keyed on the patched classes rather than on
                            # DeviceType.MOWER: an unsupported mower runs with
                            # the library's own capabilities and only gets a
                            # warning, and this registry means "belongs to a
                            # patched mower". Registering one would apply the
                            # gate to a device nothing else in here patched.
                            #
                            # The profile goes with it because a message
                            # handler has no other way to learn which mower
                            # it is reading — see state_precedence.register.
                            register_mower_bus(
                                device.events,
                                profile_for_class(
                                    device.device_info["class"]
                                ),
                            )
                        await device.initialize(mqtt)
                        self._devices.append(device)
                        if device.capabilities.device_type is DeviceType.MOWER:
                            self._setup_polling(device)
                            self._setup_fault_latch(device)
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
        profile = profile_for_class(device.device_info["class"])
        # What onMapTrack means on this class, which the map has to carry
        # for the renderer and the store; see MowerProfile.remaining_plan.
        remaining_plan = profile is not None and profile.remaining_plan
        store = _map_store(self._hass, did)
        mower_map = MowerMap(remaining_plan=remaining_plan)
        if (data := await store.async_load()) is not None:
            try:
                mower_map = MowerMap.from_dict(
                    data, remaining_plan=remaining_plan
                )
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
                event.boundaries,
                event.zones,
                event.flush_edges,
                event.corridors,
            )
            save()

        async def on_areas(event: MowerAreaEvent) -> None:
            # Only the areas the mower gave both a name and a point for:
            # a label without either has nothing to draw.
            mower_map.update_labels(
                [
                    (area.label, area.name)
                    for area in sorted(event.areas, key=lambda a: a.area_id)
                    if area.label is not None and area.name
                ]
            )
            save()

        async def on_obstacles(event: MowerObstaclesEvent) -> None:
            mower_map.update_obstacles(event.obstacles)
            save()

        async def on_remaining(event: MowerRemainingEvent) -> None:
            # The one writer for the plan, the end of a job included: a
            # stop edge arrives here as an empty complete plan, which is
            # how the mower itself reports a job it finished. See
            # map_messages.reset_remaining_plan().
            mower_map.update_remaining(event.rows, event.complete)
            save()

        async def on_coverage(event: MowerCoverageEvent) -> None:
            mower_map.update_coverage(event.lanes)
            save()

        async def on_covered(event: MowerCoveredAreaEvent) -> None:
            mower_map.update_covered(event.areas, event.holes)
            save()

        async def on_nogo(event: MowerNoGoZonesEvent) -> None:
            mower_map.update_nogo(event.zones)
            save()

        async def on_positions(event: PositionsEvent) -> None:
            # Position is volatile — no save. No valid charger position was
            # observed on firmware 1.11.31 or 1.13.10, which is why the map
            # defaults to the origin; 1.36.208 does send one, and a reported
            # position beats the assumption either way (issue #52).
            for position in event.positions:
                if position.type is PositionType.DEEBOT:
                    mower_map.update_position(
                        position.x, position.y, position.a
                    )
                elif position.type is PositionType.CHARGER:
                    mower_map.dock = (position.x, position.y)

        device.events.subscribe(MowerMapInfoEvent, on_map_info)
        device.events.subscribe(MowerObstaclesEvent, on_obstacles)
        # Both readings of onMapTrack are subscribed on every device, and
        # OnMapTrack publishes whichever one the class has: the handler is
        # the single place the decision is made, so nothing here has to
        # agree with it.
        device.events.subscribe(MowerRemainingEvent, on_remaining)
        device.events.subscribe(MowerCoverageEvent, on_coverage)
        device.events.subscribe(MowerCoveredAreaEvent, on_covered)
        device.events.subscribe(MowerNoGoZonesEvent, on_nogo)
        device.events.subscribe(PositionsEvent, on_positions)
        # Only where the ar row's label point is validated. getAreaSet is
        # also the second refresh command of every area_parameters class,
        # so subscribing for all of them would turn whatever the A1600
        # and A3000 LiDAR Pro put after the name into a label point as
        # soon as it parses as a pair of integers — and a point that is
        # not a map coordinate drags the viewport out with it, shrinking
        # the lawn to make room. Nothing in this repo shows what those
        # two models send there; issue #131 collects such readings.
        if profile is not None and profile.area_names:
            device.events.subscribe(MowerAreaEvent, on_areas)

    def _setup_fault_latch(self, device: Device) -> None:
        """Hold this mower's faults until something explicitly clears them.

        Controller-owned for the same reason as the polling below, and eager
        for the same reason as the map subscriptions above: ``EventBus.notify``
        drops an event nobody is subscribed to, so a fault raised before the
        binary sensor was added would be lost. See ``fault.py`` for why the
        device's own ``code:[0]`` cannot be trusted to clear anything.
        """
        latch = FaultLatch(device)
        latch.subscribe()
        self.fault_latches[device.device_info["did"]] = latch

    def _setup_polling(self, device: Device) -> None:
        """Poll the mower's state and stats while it is out, not while docked.

        Controller-owned rather than tied to any one entity's lifecycle: the
        lawn_mower, activity and mowing_progress/stats entities can each be
        individually disabled in the entity registry, and a background refresh
        that only one of them happened to own must not silently stop feeding
        the other three.
        """

        async def on_status(event: StateEvent) -> None:
            # There is nothing to ask about in the dock — the battery reports
            # itself. A departure the mower does not push itself, as on a
            # G1-800's scheduled run, is written from the task bury points in
            # messages.py (issue #120), so it reaches this as well.
            if event.state is State.DOCKED:
                did = device.device_info["did"]
                if did in self._unsub_polls:
                    # Back from a run: the tick last read the Wi-Fi signal out
                    # on the lawn, and nothing asks again until the next
                    # reconnect, so the sensor would show a yard value at the
                    # dock (issue #139). Already docked at startup, it has
                    # just been asked.
                    device.events.request_refresh(NetworkInfoEvent)
                self._stop_polling(did)
            else:
                self.start_polling(device)

        device.events.subscribe(StateEvent, on_status)

    def start_polling(self, device: Device) -> None:
        """Start asking for the mower's state and stats, unless already doing so.

        Public: lawn_mower.py calls this directly from its start-mowing and
        mow-area commands, and button.py from the mow-border button. A
        command sent from HA never produces a StateEvent by itself — only a
        confirmed push does — so nothing else would restart the tick if that
        push never lands.

        A class with ``app_ping`` is also told the app is open, once now and
        every ``APP_PING_INTERVAL`` after, for as long as the tick runs: it
        streams ``onMapTrack`` to nobody otherwise, and the plan on the map
        stays empty (see ``AppPing``). Only while the mower is out, because
        that stream is the only thing that needs it; at the dock ``GetMI``
        pings for itself. Both timers share one entry in ``_unsub_polls``,
        so docking and teardown stop the ping with the tick.
        """
        did = device.device_info["did"]
        if did in self._unsub_polls:
            return
        unsub_poll = async_track_time_interval(
            self._hass, partial(self._poll, device), POLL_INTERVAL
        )
        profile = profile_for_class(device.device_info.get("class"))
        if profile is None or not profile.app_ping:
            self._unsub_polls[did] = unsub_poll
            return
        unsub_ping = async_track_time_interval(
            self._hass, partial(self._ping, device), APP_PING_INTERVAL
        )

        def unsub_both() -> None:
            unsub_poll()
            unsub_ping()

        self._unsub_polls[did] = unsub_both
        self._hass.async_create_task(self._ping(device))

    async def _ping(self, device: Device, now: datetime | None = None) -> None:
        """Tell the mower the app is open; it never answers."""
        await device.execute_command(AppPing())

    def _stop_polling(self, did: str) -> None:
        """Stop asking. Idempotent, so removal after a docking is harmless."""
        if (unsub := self._unsub_polls.pop(did, None)) is not None:
            unsub()

    async def _poll(self, device: Device, now: datetime) -> None:
        """One tick: refresh the state, stats, network info and position.

        One getStats notifies both StatsEvent and MowerStatsEvent, so
        refreshing StatsEvent — which Device.__init__ always subscribes to —
        covers the mowing_progress sensor too without depending on it being
        enabled.

        The network sensors are otherwise read at setup and on reconnect
        only, so the Wi-Fi signal would show the dock's value for the whole
        run (issue #139). The getNetInfo goes out even where those sensors
        are disabled, which is the default: Device.__init__ always subscribes
        to NetworkInfoEvent to keep device.mac, so request_refresh never
        skips it. One small command per tick while the mower is out.

        The position is asked for with a command of its own: some runs on the
        G1-800 never push onPos, and deebot-client defines no map capability
        for that class to refresh through the event bus. The tick only runs
        while the mower is out, so a parked mower is never asked.

        So is what is left to mow, and for a sharper reason: the mower
        pushes that one row at a time as it works, and sends the whole
        plan only when asked. Without this a listener holds the row
        under the mower and whatever the phone's last visit happened to
        broadcast — see GetMapTrack. Only on a class that reads
        onMapTrack that way: nothing else needs the whole set, and the
        request's own shape is confirmed on that one class too.

        The tick keeps asking on the way home from a job that has just
        ended, because it stops only once the mower reports DOCKED, and
        that is left as it is: with no job to report on the mower refuses
        getMapTrack outright (code 10000, measured at the dock), so the
        question is answered with nothing and the reset published from the
        job edge stands. Should a returning mower ever answer, the answer
        is a complete plan for the job it is on, which update_remaining
        treats as the mower's own word and nothing is lost either way.
        """
        device.events.request_refresh(StateEvent)
        device.events.request_refresh(StatsEvent)
        device.events.request_refresh(NetworkInfoEvent)
        await device.execute_command(GetPosMower())
        profile = profile_for_class(device.device_info["class"])
        if profile is not None and profile.remaining_plan:
            await device.execute_command(GetMapTrack())

    async def teardown(self) -> None:
        """Disconnect controller."""
        for unsub in self._unsub_polls.values():
            unsub()
        self._unsub_polls.clear()
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
