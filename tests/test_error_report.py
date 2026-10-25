"""Tests for the unknown-error report (issue #133).

No Home Assistant import anywhere in this file, and none in ``error_report.py``
either, so these run locally on Windows alongside ``tests/deebot_patch/``:

    python -m pytest tests/test_error_report.py -p no:homeassistant -v
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.ecovacs_mower.error_report import MAX_LINES, ErrorReport
from custom_components.ecovacs_mower.errors import REPORT_URL

# 622 is in MOWER_ERROR_CODES; 998 and 999 are in neither table.
_KNOWN = 622
_UNKNOWN = 999
_OTHER_UNKNOWN = 998

_T0 = datetime(2026, 10, 25, 15, 36, 30)


def _at(seconds: int) -> datetime:
    return _T0 + timedelta(seconds=seconds)


def _report() -> ErrorReport:
    return ErrorReport("Garden (2i0fns)")


def test_a_heartbeat_is_not_a_report() -> None:
    report = _report()

    assert report.add((), _at(0)) is False
    assert report.is_open is False


def test_known_codes_alone_do_not_open_a_report() -> None:
    report = _report()

    assert report.add((_KNOWN,), _at(0)) is False
    assert report.is_open is False


def test_an_unknown_code_opens_it_and_the_text_asks_for_the_apps_wording() -> None:
    report = _report()

    assert report.add((_UNKNOWN,), _at(0)) is True

    text = report.render()
    assert report.is_open is True
    assert "Garden (2i0fns)" in text
    assert "Open the Ecovacs app now" in text
    assert REPORT_URL in text
    assert "- 15:36:30 · 999 (unknown)" in text


def test_a_burst_is_one_report_with_the_whole_list_in_the_order_sent() -> None:
    # The shape from the issue: each push has one more code on top of the last.
    report = _report()

    report.add((_KNOWN,), _at(0))
    report.add((_UNKNOWN, _KNOWN), _at(10))
    report.add((_KNOWN, _UNKNOWN, _OTHER_UNKNOWN), _at(10))

    lines = report.render().splitlines()
    assert [line for line in lines if line.startswith("- ")] == [
        "- 15:36:40 · 999 (unknown), 622 (GOAT tilted.)",
        "- 15:36:40 · 622 (GOAT tilted.), 999 (unknown), 998 (unknown)",
    ]


def test_once_open_a_known_code_is_listed_too() -> None:
    # It is the context the unknown one needs.
    report = _report()
    report.add((_UNKNOWN,), _at(0))

    assert report.add((_KNOWN,), _at(5)) is True

    assert "- 15:36:35 · 622 (GOAT tilted.)" in report.render()


def test_a_repeat_of_the_last_line_is_counted_on_it() -> None:
    report = _report()
    report.add((_UNKNOWN,), _at(0))

    for second in (1, 2, 3):
        assert report.add((_UNKNOWN,), _at(second)) is True

    body = [line for line in report.render().splitlines() if line.startswith("- ")]
    assert body == ["- 15:36:30–15:36:33 ×4 · 999 (unknown)"]


def test_a_repeat_after_a_different_line_is_a_new_line() -> None:
    report = _report()
    report.add((_UNKNOWN,), _at(0))
    report.add((_OTHER_UNKNOWN,), _at(1))
    report.add((_UNKNOWN,), _at(2))

    body = [line for line in report.render().splitlines() if line.startswith("- ")]
    assert len(body) == 3


def test_the_oldest_lines_give_way_past_the_cap() -> None:
    report = _report()
    for number in range(MAX_LINES + 10):
        # Distinct lists, so nothing collapses.
        report.add((_UNKNOWN + number,), _at(number))

    body = [line for line in report.render().splitlines() if line.startswith("- ")]
    assert len(body) == MAX_LINES
    assert f"{_UNKNOWN + MAX_LINES + 9} (unknown)" in body[-1]
    assert f" {_UNKNOWN} (unknown)" not in "\n".join(body)


def test_dismissing_closes_it_and_the_next_unknown_code_starts_a_fresh_one() -> None:
    report = _report()
    report.add((_UNKNOWN,), _at(0))
    report.add((_KNOWN,), _at(1))

    report.close()

    assert report.is_open is False
    assert report.add((_KNOWN,), _at(2)) is False
    assert report.add((_OTHER_UNKNOWN,), _at(3)) is True
    body = [line for line in report.render().splitlines() if line.startswith("- ")]
    assert body == ["- 15:36:33 · 998 (unknown)"]
