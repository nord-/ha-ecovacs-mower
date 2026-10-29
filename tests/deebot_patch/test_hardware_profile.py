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
    } == {"e4gqia", "51rcxt", "2i0fns"}


def test_zone_mowing_is_enabled_only_for_validated_class() -> None:
    """spotArea is confirmed on the A1600/A3000 LiDAR Pro, G1-800, O1200
    O800 RTK and O1200 RTK."""
    assert {
        class_ for class_, profile in SUPPORTED_CLASSES.items() if profile.zone_mowing
    } == {"e4gqia", "77atlz", "2i0fns", "51rcxt", "9bts2s", "qhq6i0"}


def test_area_names_are_enabled_only_for_the_validated_class() -> None:
    """The ar row's shape is not inferred from another class's."""
    assert {
        class_ for class_, profile in SUPPORTED_CLASSES.items() if profile.area_names
    } == {"9bts2s"}


def test_area_nogo_is_enabled_only_for_the_validated_class() -> None:
    """Section 2 means what a capture on that very class says it means.

    The O800 RTK's three-area onArI puts a section-2 ring in exactly the
    area its owner has a no-go zone in (issue #41). The O1200 LiDAR Pro's
    puts two rings inside one of the two rectangles its own
    onSpecialContour reports and none in the other, so reading section 2
    as the no-go set there would lose a zone (PR #136 review).
    """
    assert {
        class_ for class_, profile in SUPPORTED_CLASSES.items() if profile.area_nogo
    } == {"9bts2s"}


def test_remaining_plan_is_enabled_only_for_the_validated_class() -> None:
    """One job, watched end to end, on one mower.

    A row's reported length falling to zero at mowing speed and then
    disappearing is what separates the remainder of a plan from coverage,
    and that was measured on the O800 RTK alone (firmware 1.13.8). No
    other class has had a job watched that way, so every other one keeps
    reading onMapTrack as coverage — and is not asked for the whole plan
    either, since nothing would read the answer.
    """
    assert {
        class_
        for class_, profile in SUPPORTED_CLASSES.items()
        if profile.remaining_plan
    } == {"9bts2s"}


def test_app_ping_is_enabled_only_for_the_validated_class() -> None:
    """The gate the ping opens was measured on the O800 RTK alone.

    It is also the only class whose remaining_plan reading starves without
    onMapTrack, so no other mower would gain anything from a ping every two
    minutes it has not been shown to need.
    """
    assert {
        class_ for class_, profile in SUPPORTED_CLASSES.items() if profile.app_ping
    } == {"9bts2s"}


def test_profile_flags_default_to_off() -> None:
    """A newly supported class is patched without claiming any capability."""
    profile = MowerProfile("new000")

    assert profile.area_parameters is False
    assert profile.area_names is False
    assert profile.area_nogo is False
    assert profile.remaining_plan is False
    assert profile.app_ping is False
    assert profile.zone_mowing is False
    assert profile.border_mowing is False
