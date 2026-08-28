"""Which command family a given mower answers on (issue #42).

This integration exists because GOAT firmware ignores ``clean_V2`` and answers
``getCleanInfo_V2`` with nothing, so the non-V2 pair is substituted in. On
firmware 1.36.208 that is inverted: the non-V2 pair times out with ``errno
500`` on every attempt while the library's stock V2 pair is answered.

The class string cannot decide it — ``77atlz`` is one class on both branches —
and the firmware version is not knowable before ``get_devices()`` freezes the
capabilities: ``fwVer`` arrives in a command response header, and neither
``GetDeviceList`` nor ``GetGlobalDeviceList`` carries a version field. So the
choice is made at runtime, from whether the mower answers, and kept here.

Keyed by ``did`` rather than held on the command object, for two reasons that
point the same way: ``Capabilities.get_refresh_commands()`` hands out the
stored command instances, so every device of a class shares one, and
``capabilities.clean.action.command`` is the opposite case — a class,
instantiated afresh on every button press.
"""

from __future__ import annotations

from enum import StrEnum
import logging

_LOGGER = logging.getLogger(__name__)


class Family(StrEnum):
    """A pair of commands that stand or fall together.

    Named for what they are on the wire rather than "legacy" and "current":
    V2 is the library's default for these classes, and the non-V2 pair is the
    deviation this integration introduces.
    """

    NON_V2 = "non-V2"
    V2 = "V2"

    def other(self) -> Family:
        """The family to try when this one is not answered."""
        return Family.V2 if self is Family.NON_V2 else Family.NON_V2


_FAMILIES: dict[str, Family] = {}

# The family a did was on immediately before its current one, so a switch back
# to it can be told apart from a switch to genuinely new evidence.
_PREVIOUS: dict[str, Family] = {}


def selected(did: str) -> Family:
    """The family in use for *did*.

    Defaults to ``NON_V2``: that is what every confirmed mower answers on, so
    an unknown device starts where the evidence is rather than where the
    library's own definition points.
    """
    return _FAMILIES.get(did, Family.NON_V2)


def commit(did: str, family: Family) -> None:
    """Record that *did* answers on *family*.

    Logged at ``INFO`` — not debug — the first time it changes anything. It is
    the moment the integration decided this mower speaks the other dialect,
    and a user reading their log without debug enabled should be able to find
    it. A repeat is silent, so a mower that switches once does not narrate it
    every five minutes.

    A switch straight back to the family this ``did`` was on just before is
    logged at ``DEBUG`` instead: nothing rules out a firmware that answers
    *both* families — only answers-one/times-out-on-the-other has been
    observed — so an immediate reversal reads as a network blip on whichever
    family is currently committed, not as a second dialect change, and an INFO
    line for it would narrate a "switch" for every blip on a flaky
    connection.
    """
    current = selected(did)
    if current is family:
        return

    is_reversal = _PREVIOUS.get(did) is family
    _PREVIOUS[did] = current
    _FAMILIES[did] = family

    log = _LOGGER.debug if is_reversal else _LOGGER.info
    log(
        "Mower %s does not answer the %s commands but does answer the %s ones; "
        "using those from now on. See %s if the controls still do not work",
        did,
        family.other(),
        family,
        "https://github.com/nord-/ha-ecovacs-mower/issues",
    )


def family_name(did: str) -> str:
    """The family in use for *did*, for a log line."""
    return str(selected(did))


def reset() -> None:
    """Forget every choice. Tests only.

    Only this store — the per-bus registry in ``state_precedence`` has its own
    ``reset()``. ``tests/conftest.py`` clears both on every test so the two
    independent lifetimes never leak into each other.
    """
    _FAMILIES.clear()
    _PREVIOUS.clear()
