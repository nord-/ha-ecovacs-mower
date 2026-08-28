"""Tests for the per-device command family (issue #42)."""

from __future__ import annotations

import logging

import pytest

from custom_components.ecovacs_mower.deebot_patch.families import (
    Family,
    attempted_family_name,
    commit,
    family_name,
    note_attempt,
    reset,
    selected,
)


def test_the_default_is_the_non_v2_family() -> None:
    # Today's behaviour, verified on firmware 1.11.x, 1.13.x and 1.17.x. An
    # unknown mower starts where the evidence is, not where the library is.
    assert selected("any-did") is Family.NON_V2


def test_each_family_knows_its_counterpart() -> None:
    assert Family.NON_V2.other() is Family.V2
    assert Family.V2.other() is Family.NON_V2


def test_a_committed_family_sticks() -> None:
    commit("did-a", Family.V2)
    assert selected("did-a") is Family.V2


def test_devices_are_independent() -> None:
    # The command instances are shared per device class — get_refresh_commands
    # hands out the stored objects — so the choice cannot live on the command.
    commit("did-a", Family.V2)
    assert selected("did-b") is Family.NON_V2


def test_committing_a_change_is_logged_once_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The one line a user should be able to find without debug logging.
    with caplog.at_level(logging.INFO):
        commit("did-a", Family.V2)
        commit("did-a", Family.V2)

    assert sum("did-a" in record.message for record in caplog.records) == 1


def test_a_reversal_is_logged_at_debug_not_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A blip-then-blip-back on a flaky connection should not read as two
    # dialect changes: only the first switch is INFO, the immediate reversal
    # is DEBUG.
    with caplog.at_level(logging.DEBUG):
        commit("did-a", Family.V2)
        commit("did-a", Family.NON_V2)

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(info_records) == 1
    assert len(debug_records) == 1
    assert selected("did-a") is Family.NON_V2


def test_family_name_is_readable_for_a_log_line() -> None:
    assert family_name("did-a") == "non-V2"
    commit("did-a", Family.V2)
    assert family_name("did-a") == "V2"


def test_attempted_family_name_falls_back_to_the_committed_one() -> None:
    # Nothing has called note_attempt yet, e.g. a command with no family at
    # all — the plain committed name is still correct.
    assert attempted_family_name("did-a") == "non-V2"


def test_attempted_family_name_reports_a_single_successful_attempt() -> None:
    note_attempt("did-a", Family.NON_V2)
    assert attempted_family_name("did-a") == "non-V2"


def test_attempted_family_name_names_both_on_a_double_failure() -> None:
    # Issue #42's diagnostic gap: a double failure commits nothing, so
    # selected()/family_name() alone would understate what was sent.
    note_attempt("did-a", Family.NON_V2, Family.V2)
    assert attempted_family_name("did-a") == "non-V2 and V2"
    assert selected("did-a") is Family.NON_V2


def test_reset_clears_the_last_attempt_too() -> None:
    note_attempt("did-a", Family.NON_V2, Family.V2)
    reset()
    assert attempted_family_name("did-a") == "non-V2"
