"""strings.json and translations/en.json must be kept identical.

Only ``translations/en.json`` exists (see CLAUDE.md: never an ``sv.json``).
``strings.json`` is the source developers edit; ``translations/en.json`` is what
HA's frontend actually loads for English. Nothing in this integration's toolchain
syncs the files automatically — otherwise it happens by discipline alone. This
test is the guard against them drifting apart.

No HA import is required (plain JSON reading), so the test runs locally on Windows
too, without ``requires_ha``.
"""

import json
from pathlib import Path

ROOT = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"


def test_strings_and_translations_are_identical() -> None:
    """Compare parsed JSON, not raw bytes: formatting must not be able to fail the test."""
    strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    translations = json.loads(
        (ROOT / "translations" / "en.json").read_text(encoding="utf-8")
    )

    assert strings == translations
