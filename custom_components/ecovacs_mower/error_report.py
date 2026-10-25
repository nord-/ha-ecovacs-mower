"""The running list of error pushes behind the unknown-code notification.

Issue #133. The error-code catalog (#37) grows only from pairings of a code with
the Ecovacs app's own wording, and the app's wording is readable only while the
alarm is on screen. A warning in a log nobody is reading cannot ask for it in
time, so the integration asks through a notification instead; this module holds
what that notification says.

Three properties come from what a mower was seen to send:

* **Codes arrive in bursts.** One action on an O1200 produced 622, then 627 on
  top of it, then 621 on top of both, all cleared 17 seconds after the first.
  One report per mower, rewritten as the burst goes on, rather than one per
  code. Each line carries the time and the whole list in the order it came,
  known codes included, because they are the context an unknown one needs.
* **The same list can be pushed again and again** for as long as a condition
  lasts. A repeat of the line just before it is counted on that line instead
  of added.
* **The report is opened by an unknown code**, not by any code. Once open it
  takes every fault push, since a known code beside an unknown one is part of
  the picture. It stays open until the owner dismisses the notification, and
  the next unknown code after that starts a fresh one.

Deliberately not Home Assistant-aware, for the reason ``fault.py`` gives: the
state machine is the part worth testing, and keeping the import out lets
``tests/test_error_report.py`` run on Windows instead of only in CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from .errors import REPORT_URL, known_description

# Enough for a long burst; past it the oldest lines give way so the notification
# stays readable, the same cap the area-parameter log uses (debug.py).
MAX_LINES: Final = 50


@dataclass
class _Line:
    """One push, or a run of identical pushes."""

    first: datetime
    last: datetime
    codes: tuple[int, ...]
    count: int = 1


class ErrorReport:
    """What the owner is told about the error pushes of one mower."""

    def __init__(self, label: str) -> None:
        """Start closed. *label* names the mower and its class in the text."""
        self._label = label
        self._lines: list[_Line] = []
        self._open = False

    @property
    def is_open(self) -> bool:
        """Whether a report is being kept, that is, a notification is showing."""
        return self._open

    def add(self, codes: tuple[int, ...], when: datetime) -> bool:
        """Take one push; True if the notification now needs writing.

        *codes* is the push's non-zero codes in the order sent, which is what
        ``MowerErrorCodesEvent`` carries. An empty one changes nothing: a
        heartbeat is not a report.
        """
        if not codes:
            return False
        if not self._open:
            if all(known_description(code) is not None for code in codes):
                return False
            self._open = True
            self._lines = []

        if self._lines and self._lines[-1].codes == codes:
            self._lines[-1].count += 1
            self._lines[-1].last = when
        else:
            self._lines.append(_Line(when, when, codes))
            del self._lines[:-MAX_LINES]
        return True

    def close(self) -> None:
        """The owner dismissed the notification; the next unknown code starts over."""
        self._open = False
        self._lines = []

    def render(self) -> str:
        """The notification text, newest line last."""
        lines = "\n".join(_render_line(line) for line in self._lines)
        return (
            f"**{self._label}** sent an error code that neither deebot-client "
            "nor this integration has a description for.\n\n"
            "**Open the Ecovacs app now** and read what the alarm says. The "
            "wording is only on screen while the alarm lasts. Then report it "
            f"together with the codes below at {REPORT_URL}, so it can be "
            "added.\n\n"
            "Codes in the order the mower sent them, oldest first:\n\n"
            f"{lines}"
        )


def _render_line(line: _Line) -> str:
    """``- 15:36:40 · 622 (GOAT tilted.), 627 (unknown)``, with the run if repeated."""
    when = line.first.strftime("%H:%M:%S")
    if line.count > 1:
        when += f"–{line.last.strftime('%H:%M:%S')} ×{line.count}"
    codes = ", ".join(
        f"{code} ({known_description(code) or 'unknown'})" for code in line.codes
    )
    return f"- {when} · {codes}"
