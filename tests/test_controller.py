"""The controller must not drag the vacuum legacy along.

The modules under test import Home Assistant, which cannot be imported on
Windows (``fcntl``). The imports therefore live inside the test functions and the
whole file is marked ``requires_ha`` — otherwise collection itself crashes before
any skip marker gets a chance to apply. The source of truth is CI on
ubuntu-latest.
"""

import ast
import inspect
from pathlib import Path
import textwrap

import pytest

from . import requires_ha

pytestmark = requires_ha

# Registries in deebot-client that only deebot_patch/ may touch.
FORBIDDEN_MODULES = ("deebot_client.hardware", "deebot_client.messages")

# Private classes of deebot-client that only deebot_patch/ may touch. Their
# modules cannot go in FORBIDDEN_MODULES: deebot_client.authentication is
# imported all over for Authenticator and create_rest_config, it is the private
# _AuthClient inside it that is off limits.
#
# "_auth_client" (lowercase, single underscore) is included too: it is
# Authenticator's own attribute holding the _AuthClient instance, and the more
# likely real-world leak — reaching it through any Authenticator does not
# require spelling the class name at all.
FORBIDDEN_NAMES = ("_AuthClient", "_auth_client")


def test_controller_does_not_import_sucks() -> None:
    from custom_components.ecovacs_mower import controller

    source = inspect.getsource(controller)
    assert "sucks" not in source
    assert "VacBot" not in source


def test_entity_module_does_not_import_sucks() -> None:
    from custom_components.ecovacs_mower import entity

    source = inspect.getsource(entity)
    assert "sucks" not in source


def test_controller_has_no_legacy_device_api() -> None:
    from custom_components.ecovacs_mower import controller

    for removed in ("legacy_devices", "add_legacy_entity", "legacy_entity_is_added"):
        assert not hasattr(controller.EcovacsController, removed)


def test_entity_module_has_no_legacy_base_class() -> None:
    from custom_components.ecovacs_mower import entity

    assert not hasattr(entity, "EcovacsLegacyEntity")


def test_controller_exposes_devices() -> None:
    from custom_components.ecovacs_mower import controller

    assert isinstance(
        inspect.getattr_static(controller.EcovacsController, "devices"), property
    )


async def test_broken_auth_contract_raises_config_entry_error(hass) -> None:
    """A PatchContractError from the authenticator built in __init__ must not
    leak as a raw exception — async_setup_entry only gets the friendly retry
    dialog if it sees ConfigEntryError, and __init__ runs before initialize()'s
    own try/except is ever entered.
    """
    from unittest.mock import patch

    from homeassistant.const import (
        CONF_COUNTRY,
        CONF_DEVICE_ID,
        CONF_PASSWORD,
        CONF_USERNAME,
    )
    from homeassistant.exceptions import ConfigEntryError

    from custom_components.ecovacs_mower.controller import EcovacsController

    with (
        patch(
            "custom_components.ecovacs_mower.deebot_patch.authentication."
            "missing_wrapped_members",
            return_value=("login",),
        ),
        pytest.raises(ConfigEntryError),
    ):
        EcovacsController(
            hass,
            {
                CONF_DEVICE_ID: "STABLE-ID",
                CONF_COUNTRY: "SE",
                CONF_USERNAME: "user@example.com",
                CONF_PASSWORD: "hunter2",
            },
        )


async def test_account_credentials_callback_reaches_the_authenticator(hass) -> None:
    """on_account_credentials_changed must reach the authenticator unchanged.

    It is how a replacement pair minted by the password fallback (see
    deebot_patch/authentication.py) gets from there to async_update_entry,
    wired in async_setup_entry — nothing in between should drop it.
    """
    from homeassistant.const import (
        CONF_COUNTRY,
        CONF_DEVICE_ID,
        CONF_PASSWORD,
        CONF_USERNAME,
    )

    from custom_components.ecovacs_mower.controller import EcovacsController

    def callback(_account: dict[str, str]) -> None:
        pass

    controller = EcovacsController(
        hass,
        {
            CONF_DEVICE_ID: "STABLE-ID",
            CONF_COUNTRY: "SE",
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "hunter2",
        },
        on_account_credentials_changed=callback,
    )
    try:
        assert controller._authenticator._on_account_credentials is callback
    finally:
        await controller.teardown()


def _call_order(func: object) -> list[str]:
    """Return the names of the calls in *func*, in source order.

    AST rather than a string search: the comments in initialize() mention
    ``get_devices()`` by name, and an index search would have hit the comment
    instead of the call.
    """
    source = textwrap.dedent(inspect.getsource(func))  # type: ignore[arg-type]
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else target.id
            if isinstance(target, ast.Name)
            else None
        )
        if name is not None:
            found.append((node.lineno, node.col_offset, name))
    return [name for _, _, name in sorted(found)]


def test_patch_runs_before_get_devices() -> None:
    """The patch must seed the cache before the devices are built.

    ``get_devices()`` calls ``get_static_device_info()`` and bakes the result into
    ``DeviceInfo.static``, a frozen dataclass. If the patch happens after that
    call the devices have already got the unpatched capabilities, and a cache
    lookup still looks correct. The verification in turn must happen after
    ``get_devices()``, on the object the device actually got.

    Note what the test proves: **source order, not execution order.** It would
    pass for ``if False: patch_device_info(...)`` or for a call moved into a
    helper that runs later. What it catches is the realistic regression — someone
    moving a line.
    """
    from custom_components.ecovacs_mower import controller

    calls = _call_order(controller.EcovacsController.initialize)

    assert calls.index("patch_device_info") < calls.index("get_devices")
    assert calls.index("get_devices") < calls.index("verify_capabilities")


def test_verification_reads_static_device_info() -> None:
    """The verification must not look in the cache — then it proves nothing."""
    from custom_components.ecovacs_mower import controller

    source = inspect.getsource(controller.EcovacsController.initialize)
    assert "info.static.capabilities" in source


# A GOAT class outside SUPPORTED_CLASSES. All 25 MOWER classes in deebot-client
# 18.5.1 carry the same CleanV2/GetCleanInfoV2 bugs, so this device becomes an
# entity with dead controls.
_OTHER_MOWER = "cr0e4u"
# The T5PRO vacuum: a valid class, DeviceType.VACUUM, never becomes an entity.
_VACUUM = "npwtuz"
# The verified mower classes, spelled out rather than imported: importing
# deebot_patch at module level would pull in Home Assistant during collection,
# which is exactly what this file's requires_ha marker exists to avoid.
_SUPPORTED = ("2i0fns", "9bts2s", "2px96q")


async def _initialize_with(hass: object, device_classes: tuple[str, ...]) -> None:
    """Run ``initialize()`` with the devices *device_classes* coming from the API.

    Everything outside the verification loop is mocked: get_devices, the MQTT
    client and Device. What is tested is which branch a class ends up in, not
    connectivity.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from deebot_client.api_client import Devices
    from deebot_client.hardware import _DEVICES, get_static_device_info
    from deebot_client.models import DeviceInfo
    from homeassistant.const import (
        CONF_COUNTRY,
        CONF_DEVICE_ID,
        CONF_PASSWORD,
        CONF_USERNAME,
    )

    from custom_components.ecovacs_mower.controller import EcovacsController

    async def fake_get_devices() -> Devices:
        # Built at call time, not up front: the real get_devices() runs after
        # patch_device_info() and picks the patched capabilities out of the cache.
        # If DeviceInfo were built before initialize(), the supported class would
        # carry the unpatched definition and the verification would fail — on the
        # test's setup, not on the code.
        return Devices(
            mqtt=[
                DeviceInfo(
                    {
                        "class": class_,
                        "company": "eco-ng",
                        "did": f"did-{class_}",
                        "name": f"name-{class_}",
                        "resource": "res",
                    },
                    await get_static_device_info(class_),
                )
                for class_ in device_classes
            ],
            xmpp=[],
            not_supported=[],
        )

    controller = EcovacsController(
        hass,  # type: ignore[arg-type]
        {
            CONF_DEVICE_ID: "STABLE-ID",
            CONF_COUNTRY: "SE",
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "hunter2",
        },
    )
    try:
        with (
            patch(
                "deebot_client.api_client.ApiClient.get_devices",
                AsyncMock(side_effect=fake_get_devices),
            ),
            patch.object(EcovacsController, "_get_mqtt_client", AsyncMock()),
            patch(
                "custom_components.ecovacs_mower.controller.Device",
                MagicMock(return_value=AsyncMock()),
            ),
        ):
            await controller.initialize()
    finally:
        await controller.teardown()
        # initialize() seeds the cache globally; leave it as we found it.
        for class_ in (*_SUPPORTED, *device_classes):
            _DEVICES.pop(class_, None)


async def test_unsupported_mower_class_warns(hass, caplog) -> None:
    """A mower outside SUPPORTED_CLASSES must not go quiet in debug.

    That user gets an entity whose controls are dead and whose state lags —
    exactly the production symptom this project exists to eliminate. The warning
    must name the class, so the model can be reported and added to
    SUPPORTED_CLASSES.
    """
    import logging

    from custom_components.ecovacs_mower.const import ISSUE_TRACKER_URL

    caplog.set_level(logging.DEBUG)
    await _initialize_with(hass, (_OTHER_MOWER,))

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert _OTHER_MOWER in message
    assert ISSUE_TRACKER_URL in message


async def test_vacuum_on_the_same_account_stays_quiet(hass, caplog) -> None:
    """The counter-proof: a vacuum is not worth a false alarm.

    Without this test the ``elif ... is DeviceType.MOWER`` in the controller would
    be untested in its negative direction, and a simplification to "warn about
    everything unsupported" would pass green.
    """
    import logging

    caplog.set_level(logging.DEBUG)
    await _initialize_with(hass, (_VACUUM,))

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(_VACUUM in r.getMessage() for r in caplog.records)


async def test_supported_mowers_are_verified(hass, caplog) -> None:
    """Every supported class must pass the verification, without a warning."""
    import logging

    from custom_components.ecovacs_mower.deebot_patch import SUPPORTED_CLASSES

    # The spelled-out tuple above must not drift from the real one.
    assert set(_SUPPORTED) == set(SUPPORTED_CLASSES)

    caplog.set_level(logging.DEBUG)
    await _initialize_with(hass, _SUPPORTED)

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def _forbidden_imports(path: Path) -> list[str]:
    """Return the forbidden upstream modules imported in *path*.

    Four forms are caught:

    * ``import deebot_client.hardware``
    * ``from deebot_client.hardware import _DEVICES``
    * ``from deebot_client import hardware`` — never contains the string
      ``deebot_client.hardware``, so a substring search would have missed it
    * ``deebot_client.hardware._DEVICES`` after a bare ``import deebot_client`` —
      plain attribute access, the most likely leak of them all since it demands
      nothing unusual of whoever writes the code

    String literals are inspected too, so that
    ``import_module("deebot_client.hardware")`` does not slip through.

    The limit is static analysis. These are **not** caught: aliases
    (``import deebot_client as dc``), string concatenation
    (``"deebot_client." + "hardware"``) and ``getattr`` indirection. Anyone who
    wants to get around the guard can, but nobody does it by accident.

    Relative imports are let through deliberately: ``from .deebot_patch.hardware
    import ...`` is the escape hatch, and laundering upstream objects through it
    is the entire point of the module.
    """

    def is_forbidden(name: str) -> bool:
        return any(name == m or name.startswith(f"{m}.") for m in FORBIDDEN_MODULES)

    def dotted_name(node: ast.Attribute) -> str | None:
        """Reconstruct ``a.b.c`` from an attribute chain rooted in a name."""
        parts = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if not isinstance(current, ast.Name):
            return None
        parts.append(current.id)
        return ".".join(reversed(parts))

    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if is_forbidden(a.name))
        elif isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            if is_forbidden(module):
                found.append(module)
            elif module == "deebot_client":
                found.extend(
                    f"deebot_client.{a.name}"
                    for a in node.names
                    if is_forbidden(f"deebot_client.{a.name}")
                )
        elif isinstance(node, ast.Attribute):
            if (name := dotted_name(node)) and is_forbidden(name):
                found.append(name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if is_forbidden(node.value):
                found.append(node.value)

    # ast.walk visits every link in an attribute chain, so the same leak can be
    # reported several times. Deduplicated for a readable failure message.
    return list(dict.fromkeys(found))


def test_only_deebot_patch_touches_upstream_internals() -> None:
    """Enforce the isolation constraint mechanically.

    The whole point of deebot_patch/ is that a vendored client should be able to
    replace it without any entity file changing. If an import of deebot-client's
    hardware or messages registry, or of a private class of it, leaks out into the
    other files that guarantee is gone, and nobody notices until upstream
    refactors.
    """
    from custom_components.ecovacs_mower import controller

    package = Path(controller.__file__).parent
    offenders = []

    for path in package.rglob("*.py"):
        if "deebot_patch" in path.parts:
            continue
        for name in _forbidden_imports(path):
            offenders.append(f"{path.relative_to(package)}: {name}")
        # Plain substring search, unlike the imports above: a private class can
        # be reached by too many spellings for the AST to be worth it here, and
        # the name is distinctive enough not to false-alarm.
        source = path.read_text(encoding="utf-8")
        for name in FORBIDDEN_NAMES:
            if name in source:
                offenders.append(f"{path.relative_to(package)}: {name}")

    assert not offenders, (
        "Only deebot_patch/ may touch deebot-client's internals. "
        f"Leaks: {offenders}"
    )


def test_constraint_check_catches_a_leak(tmp_path: Path) -> None:
    """The constraint test must actually catch a leak, in every form it claims."""
    leaks = (
        "from deebot_client.hardware import _DEVICES",
        "from deebot_client import hardware",
        "import deebot_client.messages.json",
        'import_module("deebot_client.hardware")',
        "import deebot_client\n_DEVICES = deebot_client.hardware._DEVICES\n",
    )
    for index, leak in enumerate(leaks):
        path = tmp_path / f"leak{index}.py"
        path.write_text(leak, encoding="utf-8")
        assert _forbidden_imports(path), f"missed leak: {leak}"

    # The escape hatch must keep being let through — reaching upstream objects via
    # deebot_patch is the entire point of the module.
    clean = tmp_path / "clean.py"
    clean.write_text(
        "from deebot_client.device import Device\n"
        "from .deebot_patch.hardware import SUPPORTED_CLASSES\n"
        "from . import deebot_patch\n"
        "_DEVICES = deebot_patch.hardware.SUPPORTED_CLASSES\n",
        encoding="utf-8",
    )
    assert not _forbidden_imports(clean)


async def test_setup_map_restores_persisted_geometry() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from custom_components.ecovacs_mower.controller import EcovacsController

    controller = EcovacsController.__new__(EcovacsController)
    controller._hass = MagicMock()
    controller.maps = {}
    controller._map_stores = {}

    device = MagicMock()
    device.device_info = {"did": "did-1"}

    stored = {
        "boundary": [[0, 0], [100, 0], [100, 100]],
        "zones": [],
        "corridors": [],
        "obstacles": [],
        "nogo_zones": [],
        "lanes": [["1", 5, [[[0, 0], [0, 100]]]]],
    }
    with patch(
        "custom_components.ecovacs_mower.controller.Store"
    ) as store_cls:
        store_cls.return_value.async_load = AsyncMock(return_value=stored)
        await controller._setup_map(device)

    mower_map = controller.maps["did-1"]
    assert mower_map.boundary == [(0, 0), (100, 0), (100, 100)]
    assert mower_map.lanes == {("1", 5): [((0, 0), (0, 100))]}
    # Eager subscriptions: 4 map events + PositionsEvent. EventBus.notify
    # drops events nobody subscribes to, so waiting for the entity to
    # subscribe would lose data.
    assert device.events.subscribe.call_count == 5


async def test_setup_map_survives_corrupt_store() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from custom_components.ecovacs_mower.controller import EcovacsController

    controller = EcovacsController.__new__(EcovacsController)
    controller._hass = MagicMock()
    controller.maps = {}
    controller._map_stores = {}

    device = MagicMock()
    device.device_info = {"did": "did-1"}

    with patch(
        "custom_components.ecovacs_mower.controller.Store"
    ) as store_cls:
        store_cls.return_value.async_load = AsyncMock(
            return_value={"lanes": "not-a-list"}
        )
        await controller._setup_map(device)  # must not raise

    assert controller.maps["did-1"].is_empty


async def test_async_remove_map_store_deletes_the_right_key() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from custom_components.ecovacs_mower.controller import (
        MAP_STORAGE_VERSION,
        async_remove_map_store,
    )

    hass = MagicMock()
    with patch(
        "custom_components.ecovacs_mower.controller.Store"
    ) as store_cls:
        store_cls.return_value.async_remove = AsyncMock()
        await async_remove_map_store(hass, "did-1")

    store_cls.assert_called_once_with(
        hass, MAP_STORAGE_VERSION, "ecovacs_mower.map_did-1"
    )
    store_cls.return_value.async_remove.assert_awaited_once()
