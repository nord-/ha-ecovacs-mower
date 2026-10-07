"""Tests for model-specific capability profiles."""

from custom_components.ecovacs_mower.deebot_patch.hardware import (
    SUPPORTED_CLASSES,
    profile_for_class,
)


def test_supported_classes_are_capability_profiles() -> None:
    """The supported-device registry carries capability metadata per class.

    Which classes it holds is test_hardware.py's to assert, not this file's.
    """
    assert all(
        profile.device_class == class_
        for class_, profile in SUPPORTED_CLASSES.items()
    )


def test_area_parameters_are_enabled_only_for_validated_class() -> None:
    """Area-parameter semantics are not inferred from protocol field names."""
    assert profile_for_class("e4gqia") is not None
    assert profile_for_class("e4gqia").area_parameters is True

    for class_ in SUPPORTED_CLASSES:
        if class_ != "e4gqia":
            assert profile_for_class(class_).area_parameters is False
