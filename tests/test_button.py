"""Buttons: lifespan reset and sound signal."""

from tests import requires_ha

pytestmark = requires_ha


def test_four_lifespan_reset_buttons() -> None:
    from custom_components.ecovacs_mower.button import LIFESPAN_ENTITY_DESCRIPTIONS

    assert {d.key for d in LIFESPAN_ENTITY_DESCRIPTIONS} == {
        "reset_lifespan_blade",
        "reset_lifespan_lens_brush",
        "reset_lifespan_trimmer_brush",
        "reset_lifespan_weed_rope",
    }


def test_play_sound_button_exists() -> None:
    """The capability exists on 2i0fns but core does not expose it."""
    from custom_components.ecovacs_mower.button import ENTITY_DESCRIPTIONS

    assert {d.key for d in ENTITY_DESCRIPTIONS} == {"play_sound"}


def test_no_station_buttons() -> None:
    from custom_components.ecovacs_mower import button

    assert not hasattr(button, "STATION_ENTITY_DESCRIPTIONS")
    assert not hasattr(button, "EcovacsStationActionButtonEntity")


def test_every_description_has_a_translation() -> None:
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.button import (
        ENTITY_DESCRIPTIONS,
        LIFESPAN_ENTITY_DESCRIPTIONS,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    names = strings["entity"]["button"]

    for description in (*ENTITY_DESCRIPTIONS, *LIFESPAN_ENTITY_DESCRIPTIONS):
        assert description.translation_key in names, description.key


def test_every_button_has_an_icon() -> None:
    """A button without its own icon gets HA's generic icon — easy to miss."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.button import (
        ENTITY_DESCRIPTIONS,
        LIFESPAN_ENTITY_DESCRIPTIONS,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    names = icons["entity"]["button"]

    for description in (*ENTITY_DESCRIPTIONS, *LIFESPAN_ENTITY_DESCRIPTIONS):
        assert description.translation_key in names, description.key


def test_no_stale_button_translations_or_icons() -> None:
    """Every key in strings.json/icons.json must belong to a real button.

    The converse of the tests above: they check description -> string/icon, not
    the other way around. Without this, a leftover key for a removed button would
    go unnoticed.
    """
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.button import (
        ENTITY_DESCRIPTIONS,
        LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsClearFaultButtonEntity,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    keys = {
        d.translation_key
        for d in (*ENTITY_DESCRIPTIONS, *LIFESPAN_ENTITY_DESCRIPTIONS)
    } | {EcovacsClearFaultButtonEntity.entity_description.translation_key}
    assert set(strings["entity"]["button"]) <= keys
    assert set(icons["entity"]["button"]) <= keys


def test_the_clear_fault_button_has_a_translation_and_an_icon() -> None:
    """Not in either description tuple, so the loops above never reach it."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.button import EcovacsClearFaultButtonEntity

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    key = EcovacsClearFaultButtonEntity.entity_description.translation_key
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    assert key in strings["entity"]["button"]
    assert key in icons["entity"]["button"]


async def test_the_clear_fault_button_releases_the_latch() -> None:
    """Issue #53. The one clear that needs nothing from the mower.

    Deliberately sends no command: the latch is ours, the device has no notion
    of an acknowledged fault, and this must therefore work while the mower is
    unreachable — which is when a stale fault is most likely to be the thing
    bothering someone.
    """
    from unittest.mock import AsyncMock, Mock

    from custom_components.ecovacs_mower.button import EcovacsClearFaultButtonEntity

    latch = Mock()
    device = Mock(execute_command=AsyncMock())
    device.device_info = {"did": "test-did"}
    entity = EcovacsClearFaultButtonEntity(device, latch)

    await entity.async_press()

    latch.clear_by_request.assert_called_once_with()
    device.execute_command.assert_not_called()


def test_the_clear_fault_button_stays_available_when_the_mower_is_not() -> None:
    """A latch is local state; releasing it cannot depend on reaching the mower."""
    from custom_components.ecovacs_mower.button import EcovacsClearFaultButtonEntity

    assert EcovacsClearFaultButtonEntity._always_available is True
