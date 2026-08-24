"""The seeding of the device registry."""

import pytest
from deebot_client.commands.json.charge_state import GetChargeState
from deebot_client.commands.json.clean import CleanV2, GetCleanInfo, GetCleanInfoV2
from deebot_client.commands.json.stats import GetStats
from deebot_client.events import BatteryEvent, StateEvent, StatsEvent
from deebot_client.hardware import _DEVICES, get_static_device_info

from custom_components.ecovacs_mower.deebot_patch.commands import (
    CleanMower,
    GetCleanInfoMower,
    GetProtectState,
    GetStatsMower,
)
from custom_components.ecovacs_mower.deebot_patch.hardware import (
    SUPPORTED_CLASSES,
    patch_device_info,
)
from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerProtectStateEvent,
    MowerStatsEvent,
)

O1200 = "2i0fns"
O800 = "9bts2s"
# Second class string for the same O800 RTK hardware; upstream's module is
# byte-identical to 9bts2s.py.
O800_ALT = "2px96q"
# A third class string with the same capabilities, verified on a GOAT G1-800
# (issue #30); upstream's module is byte-identical to 9bts2s.py, docstring
# included.
G1_800 = "77atlz"
# A fourth class string with the same capabilities, confirmed as a GOAT A1600
# LiDAR Pro (PR #29); upstream's docstring calls it A3000 LiDAR Pro instead.
A1600_LIDAR = "e4gqia"
# A fifth, reported as a GOAT A1600 RTK (issue #43) — a different machine from
# the LiDAR Pro, not another string for it. Named _LIDAR/_RTK rather than a
# bare A1600 so the two cannot be mistaken for each other. Upstream's module is
# byte-identical to 9bts2s.py apart from the docstring, which names the model
# outright.
A1600_RTK = "xmp9ds"


def test_supported_classes_are_the_ones_we_patch() -> None:
    # Membership is "the patch layer covers this class", not "a human confirmed
    # it on the hardware" — hardware.py's comment block records that per class.
    assert set(SUPPORTED_CLASSES) == {
        O1200,
        O800,
        O800_ALT,
        G1_800,
        A1600_LIDAR,
        A1600_RTK,
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    """Empty the library's cache between tests."""
    for class_ in SUPPORTED_CLASSES:
        _DEVICES.pop(class_, None)
    yield
    for class_ in SUPPORTED_CLASSES:
        _DEVICES.pop(class_, None)


async def test_unpatched_library_uses_the_broken_command() -> None:
    # Documents the bug we fix: upstream wires the O1200 to CleanV2.
    info = await get_static_device_info(O1200)
    assert info.capabilities.clean.action.command is CleanV2


async def test_unpatched_library_refreshes_state_with_clean_info_v2() -> None:
    # Documents the second bug: GOAT does not answer getCleanInfo_V2.
    info = await get_static_device_info(O1200)
    commands = info.capabilities.get_refresh_commands(StateEvent)
    assert any(type(c) is GetCleanInfoV2 for c in commands)
    assert not any(type(c) is GetCleanInfo for c in commands)

    # The length is not an arbitrary number — do not remove it.
    #
    # patch_device_info() builds an entirely new CapabilityEvent with exactly two
    # commands instead of swapping out the single broken one. That is correct as
    # long as upstream's list is also exactly [GetChargeState, GetCleanInfoV2],
    # but if upstream adds a third state command our patch would drop it without
    # a trace — and the other assertions here would not notice, since they only
    # check the presence and absence of two types.
    #
    # If this line goes red: decide whether the new command should be carried
    # through the patch (probably yes) and update hardware.py, not just the
    # number.
    assert [type(c).__name__ for c in commands] == ["GetChargeState", "GetCleanInfoV2"]


@pytest.mark.parametrize("class_", SUPPORTED_CLASSES)
async def test_patch_swaps_in_clean_mower(class_: str) -> None:
    await patch_device_info(class_)
    info = await get_static_device_info(class_)
    assert info.capabilities.clean.action.command is CleanMower


@pytest.mark.parametrize("class_", SUPPORTED_CLASSES)
async def test_patch_swaps_clean_info_v2_for_the_mower_variant(class_: str) -> None:
    await patch_device_info(class_)
    info = await get_static_device_info(class_)
    commands = info.capabilities.get_refresh_commands(StateEvent)
    # Exact type, not isinstance: GetCleanInfoV2 inherits from GetCleanInfo, so
    # isinstance() would pass even without the patch and the test would be
    # meaningless.
    assert any(type(c) is GetCleanInfoMower for c in commands)
    assert not any(type(c) is GetCleanInfoV2 for c in commands)
    # Nor the library's own: it is answered, but with a constant idle (#48).
    assert not any(type(c) is GetCleanInfo for c in commands)
    assert any(type(c) is GetChargeState for c in commands)


@pytest.mark.parametrize("class_", SUPPORTED_CLASSES)
async def test_patch_wires_a_refresh_command_for_the_protection_flags(
    class_: str,
) -> None:
    # Issue #31: without this the five binary sensors sit at "unknown" forever.
    # The bus asks for a refresh when the first entity subscribes, finds no
    # command for an event the library does not know, and the device only pushes
    # onProtectState when a flag flips.
    await patch_device_info(class_)
    info = await get_static_device_info(class_)
    commands = info.capabilities.get_refresh_commands(MowerProtectStateEvent)
    assert [type(c) for c in commands] == [GetProtectState]


async def test_patch_leaves_the_librarys_own_refresh_commands_alone() -> None:
    # The protection entry is added to the mapping get_refresh_commands() reads,
    # so the entries the library built must all still be there.
    before = await get_static_device_info(O1200)
    battery_before = before.capabilities.get_refresh_commands(BatteryEvent)
    assert battery_before
    _DEVICES.pop(O1200, None)

    await patch_device_info(O1200)
    after = await get_static_device_info(O1200)

    assert after.capabilities.get_refresh_commands(BatteryEvent) == battery_before


async def test_patch_preserves_untouched_capabilities() -> None:
    before = await get_static_device_info(O1200)
    battery_before = before.capabilities.battery
    lifespans_before = before.capabilities.life_span.types
    _DEVICES.pop(O1200, None)

    await patch_device_info(O1200)
    after = await get_static_device_info(O1200)

    assert after.capabilities.battery == battery_before
    assert after.capabilities.life_span.types == lifespans_before


async def test_patch_preserves_the_area_command_on_the_o800() -> None:
    """The O800's definition carries ``clean.action.area``, the O1200's does not.

    The patch replaces ``clean.action.command`` with ``dataclasses.replace``, so
    the sibling field must survive. Rebuilding ``CapabilityCleanAction`` from
    scratch instead would silently drop it.
    """
    before = await get_static_device_info(O800)
    area_before = before.capabilities.clean.action.area
    assert area_before is not None
    _DEVICES.pop(O800, None)

    await patch_device_info(O800)
    after = await get_static_device_info(O800)

    assert after.capabilities.clean.action.area is area_before


async def test_patch_is_idempotent() -> None:
    await patch_device_info(O1200)
    await patch_device_info(O1200)
    info = await get_static_device_info(O1200)
    assert info.capabilities.clean.action.command is CleanMower


async def test_unknown_device_class_is_left_alone() -> None:
    # An unknown class must not crash. Verified in 18.5.1: get_static_device_info
    # returns None on ModuleNotFoundError, there is no fallback definition.
    await patch_device_info("nonexistent_class")
    assert "nonexistent_class" not in _DEVICES


async def test_unsupported_class_is_not_patched() -> None:
    # The T5PRO vacuum (npwtuz) is a valid class in the library but sits outside
    # SUPPORTED_CLASSES. It must not be touched.
    from deebot_client.hardware import _DEVICES as cache

    cache.pop("npwtuz", None)
    await patch_device_info("npwtuz")
    assert "npwtuz" not in cache


async def test_apply_registers_the_message_handlers() -> None:
    from deebot_client.messages.json import MESSAGES

    from custom_components.ecovacs_mower.deebot_patch import apply
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        OnChargeInfo,
        OnPos,
        OnProtectState,
        OnScheduleTaskInfo,
    )

    apply()
    assert MESSAGES["onChargeInfo"] is OnChargeInfo
    assert MESSAGES["onPos"] is OnPos
    assert MESSAGES["onProtectState"] is OnProtectState
    assert MESSAGES["onScheduleTaskInfo"] is OnScheduleTaskInfo


async def test_apply_is_idempotent() -> None:
    from deebot_client.messages.json import MESSAGES

    from custom_components.ecovacs_mower.deebot_patch import apply
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        OnChargeInfo,
        OnPos,
        OnProtectState,
        OnScheduleTaskInfo,
    )

    apply()
    apply()

    # Without these assertions the test would rest entirely on apply()'s own
    # post-check having raised — it would not stand on its own.
    assert MESSAGES["onChargeInfo"] is OnChargeInfo
    assert MESSAGES["onPos"] is OnPos
    assert MESSAGES["onProtectState"] is OnProtectState
    assert MESSAGES["onScheduleTaskInfo"] is OnScheduleTaskInfo


async def test_get_message_finds_the_registered_handlers() -> None:
    # This is the path the library actually uses to look up messages.
    from deebot_client.messages import get_message

    from custom_components.ecovacs_mower.deebot_patch import apply
    from custom_components.ecovacs_mower.deebot_patch.messages import OnChargeInfo

    apply()
    info = await get_static_device_info(O1200)
    assert get_message("onChargeInfo", info) is OnChargeInfo


@pytest.mark.parametrize("class_", SUPPORTED_CLASSES)
async def test_verify_capabilities_passes_after_patching(class_: str) -> None:
    from custom_components.ecovacs_mower.deebot_patch import verify_capabilities

    await patch_device_info(class_)
    info = await get_static_device_info(class_)
    verify_capabilities(info.capabilities, class_)


async def test_verify_capabilities_raises_on_unpatched_object() -> None:
    from custom_components.ecovacs_mower.deebot_patch import (
        PatchContractError,
        verify_capabilities,
    )

    info = await get_static_device_info(O1200)
    with pytest.raises(PatchContractError, match="too late"):
        verify_capabilities(info.capabilities, O1200)


async def test_get_devices_path_produces_patched_capabilities() -> None:
    """The positive proof: the device built by get_devices() carries CleanMower.

    This is the test that would have caught the ordering bug. The cache can look
    correct while DeviceInfo.static carries the unpatched capabilities, so
    checking _DEVICES is not enough. The test also goes through the library's real
    code path: if get_devices() ever stops reading the cache, this goes red.
    """
    from unittest.mock import AsyncMock

    from deebot_client.api_client import ApiClient

    authenticator = AsyncMock()
    authenticator.post_authenticated.return_value = {
        "devices": [{"did": "abc123", "class": O1200, "company": "eco-ng"}]
    }

    await patch_device_info(O1200)  # before get_devices, just like in the controller
    devices = await ApiClient(authenticator).get_devices()

    assert len(devices.mqtt) == 1
    assert devices.mqtt[0].static.capabilities.clean.action.command is CleanMower


async def test_get_devices_without_patch_is_broken() -> None:
    """The counter-proof: without the patch get_devices() builds the device with CleanV2.

    If this test starts failing, upstream has fixed the bug and our patch can be
    removed.
    """
    from unittest.mock import AsyncMock

    from deebot_client.api_client import ApiClient

    authenticator = AsyncMock()
    authenticator.post_authenticated.return_value = {
        "devices": [{"did": "abc123", "class": O1200, "company": "eco-ng"}]
    }

    devices = await ApiClient(authenticator).get_devices()

    assert devices.mqtt[0].static.capabilities.clean.action.command is CleanV2


async def test_patch_must_run_before_get_devices() -> None:
    """Regression guard for the ordering bug.

    ApiClient.get_devices() bakes the capabilities into DeviceInfo.static, which
    is a frozen dataclass. If the cache is patched afterwards the device still
    gets the old ones.
    """
    from deebot_client.models import DeviceInfo

    from custom_components.ecovacs_mower.deebot_patch import (
        PatchContractError,
        verify_capabilities,
    )

    # This is what it looks like when the patch came too late:
    stale = await get_static_device_info(O1200)
    device_info = DeviceInfo({"class": O1200, "did": "x"}, stale)
    await patch_device_info(O1200)  # too late for device_info

    with pytest.raises(PatchContractError):
        verify_capabilities(device_info.static.capabilities, O1200)


async def test_unpatched_library_drops_the_mowed_area() -> None:
    # Documents the gap issue #39 asks about: the library's GetStats parses
    # area and time and throws mowedArea away, and it is the only field that
    # moves while a job runs.
    info = await get_static_device_info(O1200)
    commands = info.capabilities.get_refresh_commands(StatsEvent)
    assert [type(c) for c in commands] == [GetStats]


@pytest.mark.parametrize("class_", SUPPORTED_CLASSES)
async def test_patch_swaps_in_the_stats_command_that_keeps_mowed_area(
    class_: str,
) -> None:
    await patch_device_info(class_)
    info = await get_static_device_info(class_)
    commands = info.capabilities.get_refresh_commands(StatsEvent)
    # Exact type, not isinstance: GetStatsMower inherits from GetStats, so
    # isinstance() would pass on the unpatched definition too.
    assert [type(c) for c in commands] == [GetStatsMower]


@pytest.mark.parametrize("class_", SUPPORTED_CLASSES)
async def test_patch_wires_a_refresh_command_for_the_mowing_progress(
    class_: str,
) -> None:
    # Same trap as the protection flags (issue #31): MowerStatsEvent is not a
    # library capability, so without an entry in the events mapping the bus
    # finds no command when the progress sensor subscribes and the value stays
    # unknown until something else happens to ask for stats.
    await patch_device_info(class_)
    info = await get_static_device_info(class_)
    commands = info.capabilities.get_refresh_commands(MowerStatsEvent)
    assert [type(c) for c in commands] == [GetStatsMower]


@pytest.mark.parametrize("class_", SUPPORTED_CLASSES)
async def test_patch_preserves_the_other_stats_capabilities(class_: str) -> None:
    # Only stats.clean is replaced; the total and report siblings are the
    # library's own and must survive dataclasses.replace.
    before = await get_static_device_info(class_)
    total_before = before.capabilities.stats.total
    report_before = before.capabilities.stats.report
    _DEVICES.pop(class_, None)

    await patch_device_info(class_)
    after = await get_static_device_info(class_)

    assert after.capabilities.stats.total == total_before
    assert after.capabilities.stats.report == report_before
