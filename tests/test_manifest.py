"""The manifest must satisfy the integration's hard requirements."""

import json
from pathlib import Path

MANIFEST = (
    Path(__file__).parent.parent
    / "custom_components"
    / "ecovacs_mower"
    / "manifest.json"
)
REQUIREMENTS_TEST = Path(__file__).parent.parent / "requirements-test.txt"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_domain_is_ecovacs_mower() -> None:
    assert _manifest()["domain"] == "ecovacs_mower"


def test_version_present() -> None:
    # Custom components must have a version; core integrations must not.
    assert _manifest()["version"]


def test_deebot_client_floor_is_the_tested_version() -> None:
    # A minimum, not an exact pin: hassfest rejects `==` on a package Home
    # Assistant itself depends on. The floor is the version CI tests against,
    # which requirements-test.txt pins exactly.
    tested = next(
        line
        for line in REQUIREMENTS_TEST.read_text(encoding="utf-8").splitlines()
        if line.startswith("deebot-client==")
    )
    floor = tested.removeprefix("deebot-client==")
    assert _manifest()["requirements"] == [f"deebot-client>={floor}"]


def test_no_sucks_dependency() -> None:
    joined = " ".join(_manifest()["requirements"])
    assert "sucks" not in joined
