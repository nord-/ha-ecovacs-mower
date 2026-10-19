"""The last job as an event entity."""

import pytest

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

    from custom_components.ecovacs_mower.event import (
        EcovacsJobStartedEventEntity,
        EcovacsLastJobEventEntity,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    keys = {
        EcovacsLastJobEventEntity.entity_description.translation_key,
        EcovacsJobStartedEventEntity.entity_description.translation_key,
    }

    assert set(strings["entity"]["event"]) <= keys
    assert set(icons["entity"]["event"]) <= keys


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
        phase="stop",
        trigger=trigger,
        job_type="schedule",
        mowed_area=320.567505,
        work_area=320.567505,
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

    await entity._on_job_edge(
        MowerJobEdgeEvent(phase="start", trigger="app", job_type="spotarea")
    )

    assert entity.state_attributes["event_type"] is None
    entity.async_write_ha_state.assert_not_called()


async def test_a_pause_or_resume_fires_nothing() -> None:
    """Issue #120 registers the pause and resume edges; they end no job.

    ``app`` for the reason ``test_a_start_fires_nothing`` gives: it is mapped
    for a stop, so only the phase guard keeps these from firing.
    """
    from custom_components.ecovacs_mower.deebot_patch.messages import MowerJobEdgeEvent

    entity = _bare_event_entity()

    for phase in ("pause", "resume"):
        await entity._on_job_edge(
            MowerJobEdgeEvent(phase=phase, trigger="app", job_type="schedule")
        )

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


async def test_async_added_to_hass_subscribes_both_sources() -> None:
    """The wiring itself, not just the handlers behind it.

    Every other test calls ``_on_report``/``_on_job_edge`` directly, so none of
    them would notice if the ``_subscribe(MowerJobEdgeEvent, ...)`` call were
    deleted from ``async_added_to_hass`` — the suite would stay green while
    #74 stayed unfixed in production.
    """
    from unittest.mock import Mock

    from custom_components.ecovacs_mower.deebot_patch.messages import MowerJobEdgeEvent

    entity = _bare_event_entity()
    entity._subscribe = Mock()

    await entity.async_added_to_hass()

    calls = {call.args for call in entity._subscribe.call_args_list}
    assert (entity._capability.event, entity._on_report) in calls
    assert (MowerJobEdgeEvent, entity._on_job_edge) in calls


async def test_an_unmapped_trigger_is_logged_only_once(caplog) -> None:
    """The debug line is meant to surface an unseen trigger, not flood the log."""
    import logging

    entity = _bare_event_entity()

    with caplog.at_level(logging.DEBUG):
        await entity._on_job_edge(_stop("rain"))
        await entity._on_job_edge(_stop("rain"))

    assert sum("maps to no event type" in record.message for record in caplog.records) == 1


async def test_a_replayed_ending_does_not_fire_again() -> None:
    """EventBus.subscribe hands the last event of a type to every new subscriber.

    Changing the entity ID in the UI is enough to reach this: HA removes and
    re-adds the same object against the same config entry, Device and EventBus
    (helpers/entity.py's _async_registry_updated), so both subscriptions here
    replay. A name-only rename does not — that hits the
    registry_entry.entity_id == old.entity_id branch and just calls
    async_write_ha_state(). Without this guard an entity-ID change hours after
    a job re-fired `finished` with a fresh timestamp, and every automation on
    the entity ran again.

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


def _bare_job_started_entity():
    """Built through its real ``__init__``, for the reason ``_bare_event_entity`` gives."""
    from unittest.mock import MagicMock, Mock

    from custom_components.ecovacs_mower.event import EcovacsJobStartedEventEntity

    device = Mock()
    device.device_info = MagicMock()
    device.device_info.__getitem__ = lambda _self, _key: "did-1"

    entity = EcovacsJobStartedEventEntity(device)
    entity.async_write_ha_state = Mock()
    return entity


def _edge(phase: str, trigger: str, job_type: str = "schedule"):
    from custom_components.ecovacs_mower.deebot_patch.messages import MowerJobEdgeEvent

    return MowerJobEdgeEvent(phase=phase, trigger=trigger, job_type=job_type)


def test_job_started_translations_and_icon() -> None:
    """Name, one state per event type, and an icon of its own."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.event import EcovacsJobStartedEventEntity

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    description = EcovacsJobStartedEventEntity.entity_description
    entry = strings["entity"]["event"][description.translation_key]

    assert entry["name"] == "Job started"
    states = entry["state_attributes"]["event_type"]["state"]
    for event_type in description.event_types:
        assert event_type in states, event_type
    assert description.translation_key in icons["entity"]["event"]


def test_job_started_is_not_a_diagnostic() -> None:
    """It is meant for automations, so it belongs on the device's main card."""
    from custom_components.ecovacs_mower.event import EcovacsJobStartedEventEntity

    assert EcovacsJobStartedEventEntity.entity_description.entity_category is None


async def test_a_scheduled_resume_fires_with_its_phase_and_job_type() -> None:
    """Issue #120: the G1-800's schedule window opening, mow-schedule-resume
    with triggerType schedule. The reporter's weather check triggers on this."""
    entity = _bare_job_started_entity()

    await entity._on_job_edge(_edge("resume", "schedule"))

    attributes = entity.state_attributes
    assert attributes["event_type"] == "schedule"
    assert attributes["phase"] == "resume"
    assert attributes["job_type"] == "schedule"
    entity.async_write_ha_state.assert_called_once()


@pytest.mark.parametrize(
    ("phase", "trigger", "job_type"),
    [
        ("start", "schedule", "schedule"),
        ("resume", "continue", "schedule"),
        ("start", "app", "spotarea"),
        ("start", "reborn", "spotarea"),
    ],
)
async def test_every_observed_start_word_fires_verbatim(
    phase: str, trigger: str, job_type: str
) -> None:
    """The words stay raw: the O1200's resume says continue where the G1-800's
    says schedule, and job_type is what names the schedule's job."""
    entity = _bare_job_started_entity()

    await entity._on_job_edge(_edge(phase, trigger, job_type))

    assert entity.state_attributes["event_type"] == trigger
    assert entity.state_attributes["phase"] == phase
    assert entity.state_attributes["job_type"] == job_type


@pytest.mark.parametrize("phase", ["pause", "stop"])
async def test_a_pause_or_stop_fires_no_job_started(phase: str) -> None:
    """``app`` is a declared event type, so only the phase guard stops these."""
    entity = _bare_job_started_entity()

    await entity._on_job_edge(_edge(phase, "app"))

    assert entity.state_attributes["event_type"] is None
    entity.async_write_ha_state.assert_not_called()


async def test_an_unknown_start_word_fires_nothing_and_is_logged_once(caplog) -> None:
    """_trigger_event raises on a type outside event_types, so an unseen word
    is dropped, and surfaces once in the debug log."""
    import logging

    entity = _bare_job_started_entity()

    with caplog.at_level(logging.DEBUG):
        await entity._on_job_edge(_edge("start", "lowBattery"))
        await entity._on_job_edge(_edge("start", "lowBattery"))

    assert entity.state_attributes["event_type"] is None
    assert sum("is no event type" in record.message for record in caplog.records) == 1


async def test_a_replayed_start_does_not_fire_again() -> None:
    """EventBus.subscribe replays the last event to a new subscriber, and an
    entity-ID change re-adds the entity: held by identity, as _reported_ending
    is. Two genuine, equal-looking starts are distinct objects (_seq), so both
    fire."""
    entity = _bare_job_started_entity()
    start = _edge("start", "schedule")

    await entity._on_job_edge(start)
    await entity._on_job_edge(start)

    assert entity.async_write_ha_state.call_count == 1

    await entity._on_job_edge(_edge("start", "schedule"))

    assert entity.async_write_ha_state.call_count == 2


async def test_job_started_subscribes_to_the_job_edges() -> None:
    from unittest.mock import Mock

    from custom_components.ecovacs_mower.deebot_patch.messages import MowerJobEdgeEvent

    entity = _bare_job_started_entity()
    entity._subscribe = Mock()

    await entity.async_added_to_hass()

    calls = {call.args for call in entity._subscribe.call_args_list}
    assert (MowerJobEdgeEvent, entity._on_job_edge) in calls


def test_the_two_event_entities_have_distinct_unique_ids() -> None:
    """Same device, same platform: equal ids would make HA drop one of them."""
    started = _bare_job_started_entity()
    last_job = _bare_event_entity()

    assert started.unique_id == "did-1_job_started"
    assert started.unique_id != last_job.unique_id


async def test_job_started_exists_only_on_a_mower() -> None:
    """The gate EcovacsActivitySensor uses; Last mowing job stays on every device."""
    from unittest.mock import MagicMock

    from deebot_client.capabilities import DeviceType

    from custom_components.ecovacs_mower.event import (
        EcovacsJobStartedEventEntity,
        EcovacsLastJobEventEntity,
        async_setup_entry,
    )

    def _device(did: str, device_type: DeviceType):
        device = MagicMock()
        device.capabilities.device_type = device_type
        device.device_info = {"did": did}
        return device

    mower = _device("did-mower", DeviceType.MOWER)
    vacuum = _device("did-vacuum", DeviceType.VACUUM)
    config_entry = MagicMock()
    config_entry.runtime_data.devices = [mower, vacuum]

    added: list = []
    await async_setup_entry(MagicMock(), config_entry, added.extend)

    assert {
        e._device for e in added if isinstance(e, EcovacsJobStartedEventEntity)
    } == {mower}
    assert {
        e._device for e in added if isinstance(e, EcovacsLastJobEventEntity)
    } == {mower, vacuum}
