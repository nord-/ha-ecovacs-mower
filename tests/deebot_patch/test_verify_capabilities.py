"""The patched area capability reaches the actual device definition."""

from dataclasses import replace

import pytest
from deebot_client.hardware import _DEVICES, get_static_device_info

from custom_components.ecovacs_mower.deebot_patch import (
    PatchContractError,
    verify_capabilities,
)
from custom_components.ecovacs_mower.deebot_patch.hardware import (
    SUPPORTED_CLASSES,
    patch_device_info,
)
from custom_components.ecovacs_mower.deebot_patch.zonal import MowArea

O800 = "9bts2s"
A1600_LIDAR = "e4gqia"


@pytest.fixture(autouse=True)
def _clear_cache():
    """Empty the library's cache between tests."""
    for class_ in SUPPORTED_CLASSES:
        _DEVICES.pop(class_, None)
    yield
    for class_ in SUPPORTED_CLASSES:
        _DEVICES.pop(class_, None)


@pytest.mark.parametrize("class_", sorted(SUPPORTED_CLASSES))
async def test_verify_capabilities_accepts_patched_area_gating(class_: str) -> None:
    """Zone and non-zone classes both satisfy the capability contract."""
    await patch_device_info(class_)
    info = await get_static_device_info(class_)

    verify_capabilities(info.capabilities, class_)


async def test_verify_capabilities_rejects_a_zone_device_without_mow_area() -> None:
    """The contract catches a zone device whose patched area was lost."""
    await patch_device_info(A1600_LIDAR)
    info = await get_static_device_info(A1600_LIDAR)
    capabilities = replace(
        info.capabilities,
        clean=replace(
            info.capabilities.clean,
            action=replace(info.capabilities.clean.action, area=None),
        ),
    )

    with pytest.raises(PatchContractError, match="MowArea capability"):
        verify_capabilities(capabilities, A1600_LIDAR)


@pytest.mark.parametrize("class_", sorted(SUPPORTED_CLASSES))
async def test_mow_area_follows_the_profile_flag(class_: str) -> None:
    """Exactly the classes whose profile sets zone_mowing carry MowArea."""
    await patch_device_info(class_)
    info = await get_static_device_info(class_)

    has_mow_area = info.capabilities.clean.action.area is MowArea
    assert has_mow_area is SUPPORTED_CLASSES[class_].zone_mowing


async def test_the_profile_is_the_only_zone_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning the flag on for a class is all it takes to give it MowArea.

    Guards against a hardcoded class list surviving next to the profile: the
    O800 has no zone confirmation, so it can only get MowArea from the flag.
    """
    monkeypatch.setitem(
        SUPPORTED_CLASSES, O800, replace(SUPPORTED_CLASSES[O800], zone_mowing=True)
    )

    await patch_device_info(O800)
    info = await get_static_device_info(O800)

    assert info.capabilities.clean.action.area is MowArea
    verify_capabilities(info.capabilities, O800)


async def test_verify_capabilities_reads_the_flag_from_the_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A class flagged for zone mowing but built without MowArea fails loudly."""
    await patch_device_info(O800)
    info = await get_static_device_info(O800)
    monkeypatch.setitem(
        SUPPORTED_CLASSES, O800, replace(SUPPORTED_CLASSES[O800], zone_mowing=True)
    )

    with pytest.raises(PatchContractError, match="MowArea capability"):
        verify_capabilities(info.capabilities, O800)
