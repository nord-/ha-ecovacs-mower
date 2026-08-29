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


def _bare_event_entity():
    """An entity without HA, built through its real ``__init__``.

    Not ``__new__`` with the fields filled in by hand, which is what the sensor
    tests do: that would supply ``_library_source_seen`` and
    ``_logged_triggers`` itself, so no test would notice if the constructor
    stopped setting them — and the first job edge would then raise
    ``AttributeError`` inside a bus subscriber in production while the suite
    stayed green. The constructor needs nothing from hass; only
    ``device_info`` has to be subscriptable.

    ``async_write_ha_state`` is a Mock rather than a no-op lambda so tests can
    assert that a fire actually reaches the state machine. ``_trigger_event``
    alone only mutates private attributes.
    """
    from unittest.mock import MagicMock, Mock

    from custom_components.ecovacs_mower.event import EcovacsLastJobEventEntity

    device = Mock()
    device.device_info = MagicMock()
    device.device_info.__getitem__ = lambda _self, _key: "did-1"

    entity = EcovacsLastJobEventEntity(device)
    entity.async_write_ha_state = Mock()
    return entity


def _stop(trigger: str):
    from custom_components.ecovacs_mower.deebot_patch.messages import MowerJobEdgeEvent

    return MowerJobEdgeEvent(
        phase="stop", trigger=trigger, mowed_area=320.567505, work_area=320.567505
    )


def _report(status):
    from deebot_client.events import ReportStatsEvent

    return ReportStatsEvent(
        area=3205675, time=14490, type=None, cleaning_id="788398868",
        status=status, content=[],
    )


async def test_a_completion_fires_finished() -> None:
    """Issue #74. The entity had never fired on a mower with 148 jobs behind it.

    reportStats — the library's own source, and note the name has no "on"
    prefix — does not appear once in any capture, on either GOAT class, against
    28 observed job endings. The reason a job ended is only on the wire in the
    stop bury point's trigger.
    """
    entity = _bare_event_entity()

    await entity._on_job_edge(_stop("workComplete"))

    assert entity.state_attributes["event_type"] == "finished"
    # _trigger_event only mutates private attributes; without the write the
    # entity never changes state and issue #74 is not fixed.
    entity.async_write_ha_state.assert_called_once()


async def test_a_job_stopped_from_the_app_fires_manually_stopped() -> None:
    """mow-schedule-stop carries app as often as workComplete."""
    entity = _bare_event_entity()

    await entity._on_job_edge(_stop("app"))

    assert entity.state_attributes["event_type"] == "manually_stopped"


async def test_an_unmapped_stop_trigger_fires_nothing() -> None:
    """finished_with_warnings has no observed trigger, so nothing is guessed.

    Firing the wrong event type is worse than firing none: an automation
    reading it would be told a job finished cleanly when nobody knows that.
    """
    entity = _bare_event_entity()

    await entity._on_job_edge(_stop("alert"))

    assert entity.state_attributes["event_type"] is None


async def test_a_start_fires_nothing() -> None:
    """The entity is about jobs ending. Both phases arrive on the same event.

    ``app`` deliberately, not ``schedule``: it is the one trigger that appears
    in both vocabularies — a zone job launched from the app announces
    ``mow-spotarea-start`` with it — so it is the only input that tells the
    phase guard apart from the trigger lookup. With ``schedule`` this test
    passed with the guard deleted, while an app-launched job would have fired
    ``manually_stopped`` the moment it started.
    """
    from custom_components.ecovacs_mower.deebot_patch.messages import MowerJobEdgeEvent

    entity = _bare_event_entity()

    await entity._on_job_edge(MowerJobEdgeEvent(phase="start", trigger="app"))

    assert entity.state_attributes["event_type"] is None
    entity.async_write_ha_state.assert_not_called()


async def test_the_library_source_still_fires() -> None:
    """The bury point is a complement, not a replacement."""
    from deebot_client.events import CleanJobStatus

    entity = _bare_event_entity()

    await entity._on_report(_report(CleanJobStatus.FINISHED_WITH_WARNINGS))

    assert entity.state_attributes["event_type"] == "finished_with_warnings"
    entity.async_write_ha_state.assert_called_once()


async def test_the_library_source_wins_once_it_has_proved_itself() -> None:
    """A class carrying both must not fire twice per job.

    Nothing observed says reportStats is dead everywhere — only that it is dead
    on 2i0fns and 2px96q — so the first answer from it permanently stands the
    bury-point path down rather than the other way round.
    """
    from deebot_client.events import CleanJobStatus

    entity = _bare_event_entity()
    await entity._on_report(_report(CleanJobStatus.FINISHED))
    assert entity.state_attributes["event_type"] == "finished"

    await entity._on_job_edge(_stop("app"))

    assert entity.state_attributes["event_type"] == "finished"


async def test_a_job_still_running_fires_nothing() -> None:
    from deebot_client.events import CleanJobStatus

    for status in (CleanJobStatus.NO_STATUS, CleanJobStatus.CLEANING):
        entity = _bare_event_entity()

        await entity._on_report(_report(status))

        assert entity.state_attributes["event_type"] is None, status


def test_every_mapped_trigger_names_a_declared_event_type() -> None:
    """_trigger_event raises on a type outside event_types, at runtime only.

    A typo in the mapping would therefore surface as an exception inside a bus
    subscriber on the one occasion per job it matters, which is the worst
    possible moment to find it.
    """
    from custom_components.ecovacs_mower.event import (
        _TRIGGER_EVENT_TYPES,
        EcovacsLastJobEventEntity,
    )

    declared = set(EcovacsLastJobEventEntity.entity_description.event_types)

    assert set(_TRIGGER_EVENT_TYPES.values()) <= declared


async def test_a_mid_job_report_does_not_stand_the_bury_point_down() -> None:
    """The latch has to mean "the library produced an ending", not "it spoke".

    reportStats carries CLEANING while a job runs (stop == 0) and the ending
    only afterwards. A class that sends the running reports but never the
    terminal one would otherwise silence the second source on the strength of
    a message that reports no ending at all — and the entity would be dead
    again, which is the whole of issue #74.
    """
    from deebot_client.events import CleanJobStatus

    entity = _bare_event_entity()

    await entity._on_report(_report(CleanJobStatus.CLEANING))
    await entity._on_job_edge(_stop("workComplete"))

    assert entity.state_attributes["event_type"] == "finished"


async def test_a_replayed_ending_does_not_fire_again() -> None:
    """EventBus.subscribe hands the last event of a type to every new subscriber.

    Renaming the entity in the UI is enough to reach this: HA removes and
    re-adds the same object against the same config entry, Device and EventBus
    (helpers/entity.py's _async_registry_updated), so both subscriptions here
    replay. Without this guard a rename hours after a job re-fired `finished`
    with a fresh timestamp, and every automation on the entity ran again.

    Identity, not equality: the bus hands back the very object it notified, and
    a genuinely new ending is always a new instance — MowerJobEdgeEvent._seq
    exists precisely so two equal-looking ones cannot collapse.
    """
    from deebot_client.events import CleanJobStatus

    entity = _bare_event_entity()
    ending = _stop("workComplete")

    await entity._on_job_edge(ending)
    await entity._on_job_edge(ending)

    assert entity.async_write_ha_state.call_count == 1

    entity = _bare_event_entity()
    report = _report(CleanJobStatus.FINISHED)

    await entity._on_report(report)
    await entity._on_report(report)

    assert entity.async_write_ha_state.call_count == 1
