"""The last job as an event entity."""

from tests import requires_ha

pytestmark = requires_ha


def test_last_job_entity_exists() -> None:
    from custom_components.ecovacs_mower.event import EcovacsLastJobEventEntity

    assert EcovacsLastJobEventEntity.entity_description.key == "stats_report"


def test_event_types_cover_the_report_states() -> None:
    """A reported state outside the list is dropped by HA."""
    from custom_components.ecovacs_mower.event import EcovacsLastJobEventEntity

    types = EcovacsLastJobEventEntity.entity_description.event_types
    assert types, "event_types must not be empty"


def test_event_types_match_get_name_key_of_the_reportable_statuses() -> None:
    """event_types is locked to the CleanJobStatus values the entity triggers on.

    NO_STATUS and CLEANING are filtered out in on_event — only finished jobs
    should reach _trigger_event, so only the three remaining statuses belong here.
    """
    from deebot_client.events import CleanJobStatus

    from custom_components.ecovacs_mower.event import EcovacsLastJobEventEntity
    from custom_components.ecovacs_mower.util import get_name_key

    reportable = {
        status
        for status in CleanJobStatus
        if status not in (CleanJobStatus.NO_STATUS, CleanJobStatus.CLEANING)
    }

    assert set(EcovacsLastJobEventEntity.entity_description.event_types) == {
        get_name_key(status) for status in reportable
    }


def test_translation_exists() -> None:
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.event import EcovacsLastJobEventEntity

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    key = EcovacsLastJobEventEntity.entity_description.translation_key
    assert key in strings["entity"]["event"]


def test_translated_states_cover_every_event_type() -> None:
    """A state without a translation shows a raw string in the UI."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.event import EcovacsLastJobEventEntity

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    key = EcovacsLastJobEventEntity.entity_description.translation_key
    states = strings["entity"]["event"][key]["state_attributes"]["event_type"]["state"]

    for event_type in EcovacsLastJobEventEntity.entity_description.event_types:
        assert event_type in states, event_type


def test_last_job_entity_has_an_icon() -> None:
    """An event entity without its own icon gets HA's generic icon."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.event import EcovacsLastJobEventEntity

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    key = EcovacsLastJobEventEntity.entity_description.translation_key
    assert key in icons["entity"]["event"]


def test_no_stale_event_translations_or_icons() -> None:
    """Every key in strings.json/icons.json must belong to a real event entity.

    The converse of the tests above: they check description -> string/icon, not
    the other way around. Without this, a leftover key for a removed event entity would
    go unnoticed.
    """
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.event import EcovacsLastJobEventEntity

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    key = EcovacsLastJobEventEntity.entity_description.translation_key

    assert set(strings["entity"]["event"]) <= {key}
    assert set(icons["entity"]["event"]) <= {key}
