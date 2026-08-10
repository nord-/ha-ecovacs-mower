"""The switches must correspond to the settings a GOAT has."""

from tests import requires_ha

pytestmark = requires_ha


def test_expected_switch_keys() -> None:
    """Locks the set. If it changes, that must be a decision, not an accident."""
    from custom_components.ecovacs_mower.switch import ENTITY_DESCRIPTIONS

    assert {d.key for d in ENTITY_DESCRIPTIONS} == {
        "advanced_mode",
        "true_detect",
        "border_switch",
        "child_lock",
        "move_up_warning",
        "cross_map_border_warning",
        "safe_protect",
    }


def test_no_vacuum_only_switches() -> None:
    """The capabilities do not exist on 2i0fns, so the entities would be empty anyway."""
    from custom_components.ecovacs_mower.switch import ENTITY_DESCRIPTIONS

    keys = {d.key for d in ENTITY_DESCRIPTIONS}
    assert keys.isdisjoint(
        {"continuous_cleaning", "carpet_auto_fan_boost", "clean_preference", "border_spin"}
    )


def test_every_description_has_a_translation() -> None:
    """A missing key yields raw strings in the UI."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.switch import ENTITY_DESCRIPTIONS

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    names = strings["entity"]["switch"]

    for description in ENTITY_DESCRIPTIONS:
        assert description.translation_key in names, description.key


def test_every_switch_has_an_icon() -> None:
    """A switch without its own icon gets HA's generic toggle — easy to miss."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.switch import ENTITY_DESCRIPTIONS

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    names = icons["entity"]["switch"]

    for description in ENTITY_DESCRIPTIONS:
        assert description.translation_key in names, description.key


def test_no_stale_switch_translations_or_icons() -> None:
    """Every key in strings.json/icons.json must belong to a real switch.

    The converse of the tests above: they check description -> string/icon, not
    the other way around. Without this, a leftover key for a removed switch would
    go unnoticed.
    """
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.switch import ENTITY_DESCRIPTIONS

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    keys = {d.translation_key for d in ENTITY_DESCRIPTIONS}
    assert set(strings["entity"]["switch"]) <= keys
    assert set(icons["entity"]["switch"]) <= keys
