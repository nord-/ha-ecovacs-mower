"""Tests for model-specific capability profiles."""

from custom_components.ecovacs_mower.deebot_patch.hardware import (
    SUPPORTED_CLASSES,
    MowerProfile,
)


def test_supported_classes_are_capability_profiles() -> None:
    """The supported-device registry carries capability metadata per class.

    Which classes it holds is test_hardware.py's to assert, not this file's.
    """
    assert all(
        profile.device_class == class_
        for class_, profile in SUPPORTED_CLASSES.items()
    )


def test_area_parameters_are_enabled_only_for_validated_classes() -> None:
    """Area-parameter semantics are not inferred from protocol field names."""
    assert {
        class_
        for class_, profile in SUPPORTED_CLASSES.items()
        if profile.area_parameters
    } == {"e4gqia", "51rcxt"}


def test_zone_mowing_is_enabled_only_for_validated_class() -> None:
    """spotArea is confirmed on the A1600/A3000 LiDAR Pro, G1-800 and O1200."""
    assert {
        class_ for class_, profile in SUPPORTED_CLASSES.items() if profile.zone_mowing
    } == {"e4gqia", "77atlz", "2i0fns", "51rcxt"}


def test_profile_flags_default_to_off() -> None:
    """A newly supported class is patched without claiming any capability."""
    profile = MowerProfile("new000")

    assert profile.area_parameters is False
    assert profile.zone_mowing is False
    assert profile.border_mowing is False
