"""Number entities: volume and cut direction."""

from tests import requires_ha

pytestmark = requires_ha


def test_expected_number_keys() -> None:
    from custom_components.ecovacs_mower.number import ENTITY_DESCRIPTIONS

    assert {d.key for d in ENTITY_DESCRIPTIONS} == {"volume", "cut_direction"}


def test_cut_direction_is_a_line_orientation() -> None:
    """0-180 degrees, not 0-359.

    The cut direction is a line orientation, not a compass bearing: 180 degrees
    covers every possible stripe pattern, since 190 and 10 give the same result.
    Verified against HA 2026.7.4.
    """
    from custom_components.ecovacs_mower.number import ENTITY_DESCRIPTIONS

    cut_direction = next(d for d in ENTITY_DESCRIPTIONS if d.key == "cut_direction")
    assert cut_direction.native_min_value == 0
    assert cut_direction.native_max_value == 180


def test_every_description_has_a_translation() -> None:
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.number import ENTITY_DESCRIPTIONS

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    names = strings["entity"]["number"]

    for description in ENTITY_DESCRIPTIONS:
        assert description.translation_key in names, description.key


def test_every_number_has_an_icon() -> None:
    """A number without its own icon gets HA's generic slider — easy to miss."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.number import ENTITY_DESCRIPTIONS

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    names = icons["entity"]["number"]

    for description in ENTITY_DESCRIPTIONS:
        assert description.translation_key in names, description.key


def test_no_stale_number_translations_or_icons() -> None:
    """Every key in strings.json/icons.json must belong to a real number.

    The converse of the tests above: they check description -> string/icon, not
    the other way around. Without this, a leftover key for a removed number would
    go unnoticed.
    """
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.number import ENTITY_DESCRIPTIONS

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    keys = {d.translation_key for d in ENTITY_DESCRIPTIONS}
    assert set(strings["entity"]["number"]) <= keys
    assert set(icons["entity"]["number"]) <= keys
