"""The patched area capability reaches the actual device definition."""

import pytest
from deebot_client.hardware import _DEVICES

from .. import requires_ha

O800 = "9bts2s"
A1600_LIDAR = "e4gqia"
SUPPORTED_CLASSES = (O800, A1600_LIDAR)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Empty the library's cache between tests."""
    for class_ in SUPPORTED_CLASSES:
        _DEVICES.pop(class_, None)
    yield
    for class_ in SUPPORTED_CLASSES:
        _DEVICES.pop(class_, None)


@pytest.mark.parametrize("class_", [O800, A1600_LIDAR])
async def test_verify_capabilities_accepts_patched_area_gating(class_: str) -> None:
    """Zone and non-zone classes both satisfy the capability contract."""
    from deebot_client.hardware import get_static_device_info

    from custom_components.ecovacs_mower.deebot_patch import verify_capabilities
    from custom_components.ecovacs_mower.deebot_patch.hardware import patch_device_info

    await patch_device_info(class_)
    info = await get_static_device_info(class_)

    verify_capabilities(info.capabilities, class_)


async def test_verify_capabilities_rejects_a_zone_device_without_mow_area() -> None:
    """The contract catches a zone device whose patched area was lost."""
    from dataclasses import replace
    from deebot_client.hardware import get_static_device_info

    from custom_components.ecovacs_mower.deebot_patch import (
        PatchContractError,
        verify_capabilities,
    )
    from custom_components.ecovacs_mower.deebot_patch.hardware import patch_device_info

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


async def test_zone_device_receives_mow_area() -> None:
    """The zone class carries MowArea while the O800 keeps its library area."""
    from deebot_client.hardware import get_static_device_info

    from custom_components.ecovacs_mower.deebot_patch.hardware import patch_device_info
    from custom_components.ecovacs_mower.deebot_patch.zonal import MowArea

    await patch_device_info(A1600_LIDAR)
    await patch_device_info(O800)
    zone = await get_static_device_info(A1600_LIDAR)
    non_zone = await get_static_device_info(O800)

    assert zone.capabilities.clean.action.area is MowArea
    assert non_zone.capabilities.clean.action.area is not MowArea
