"""Controller module.

Forked from Home Assistant core (``homeassistant/components/ecovacs/controller.py``).
Support for XMPP-connected devices and its dependency on the legacy client
library has been removed: this integration only supports MQTT.
"""

import asyncio
from collections.abc import Mapping
from functools import partial
import logging
import ssl
from typing import Any

from deebot_client.api_client import ApiClient
from deebot_client.authentication import Authenticator, create_rest_config
from deebot_client.capabilities import DeviceType
from deebot_client.const import UNDEFINED, UndefinedType
from deebot_client.device import Device
from deebot_client.exceptions import (
    DeebotError,
    DeviceVerificationRequiredError,
    InvalidAuthenticationError,
)
from deebot_client.mqtt_client import MqttClient, create_mqtt_config
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
from homeassistant.util.ssl import get_default_no_verify_context

from .const import (
    CONF_OVERRIDE_MQTT_URL,
    CONF_OVERRIDE_REST_URL,
    CONF_VERIFY_MQTT_CERTIFICATE,
    ISSUE_TRACKER_URL,
)
from .deebot_patch import (
    PatchContractError,
    apply as apply_deebot_patch,
    verify_capabilities,
)
from .deebot_patch.hardware import SUPPORTED_CLASSES, patch_device_info

_LOGGER = logging.getLogger(__name__)


class EcovacsController:
    """Ecovacs controller."""

    def __init__(self, hass: HomeAssistant, config: Mapping[str, Any]) -> None:
        """Initialize controller."""
        self._hass = hass
        self._devices: list[Device] = []
        rest_url = config.get(CONF_OVERRIDE_REST_URL)
        self._device_id = config[CONF_DEVICE_ID]
        country = config[CONF_COUNTRY]

        self._authenticator = Authenticator(
            create_rest_config(
                aiohttp_client.async_get_clientsession(self._hass),
                device_id=self._device_id,
                alpha_2_country=country,
                override_rest_url=rest_url,
            ),
            config[CONF_USERNAME],
            md5(config[CONF_PASSWORD]),
        )
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
                    # SUPPORTED_CLASSES only covers the verified ones. Any other
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

    async def teardown(self) -> None:
        """Disconnect controller."""
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
