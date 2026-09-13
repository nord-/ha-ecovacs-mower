"""Tests for the area protocol commands: get/set area parameters and area set."""

from unittest.mock import Mock, patch

from deebot_client.message import HandlingState

from custom_components.ecovacs_mower.deebot_patch.areas import MowerArea, _areas_for
from custom_components.ecovacs_mower.deebot_patch.commands import (
    GetAreaParameter,
    GetAreaSet,
    SetAreaParameter,
)


def _ok_area_parameter_response(*parameters: dict) -> dict:
    return {
        "ret": "ok",
        "resp": {"body": {"data": {"areaParameters": list(parameters)}}},
    }


def test_get_area_parameter_command_shape() -> None:
    assert GetAreaParameter.NAME == "getAreaParameter"
    assert GetAreaParameter()._args == {}


def test_get_area_parameter_stores_one_areas_values() -> None:
    event_bus = Mock()
    response = _ok_area_parameter_response(
        {
            "areaID": 12,
            "mowHeightLevel": 7,
            "cutMode": 4,
            "obstacleHeight": 15,
            "angle": 90,
        }
    )

    result = GetAreaParameter()._handle_response(event_bus, response)

    area = _areas_for(event_bus)["12"]
    assert area.mow_height_level == 7
    assert area.cut_mode == 4
    assert area.obstacle_height == 15
    assert area.angle == 90
    assert result.state is HandlingState.SUCCESS
    event_bus.notify.assert_called_once()


def test_get_area_parameter_preserves_the_name_already_known_from_getAreaSet() -> None:
    event_bus = Mock()
    _areas_for(event_bus)["12"] = MowerArea("12", name="North lawn")
    response = _ok_area_parameter_response(
        {
            "areaID": 12,
            "mowHeightLevel": 7,
            "cutMode": 4,
            "obstacleHeight": 15,
            "angle": 90,
        }
    )

    GetAreaParameter()._handle_response(event_bus, response)

    assert _areas_for(event_bus)["12"].name == "North lawn"


def test_get_area_parameter_skips_a_row_without_an_area_id() -> None:
    event_bus = Mock()
    response = _ok_area_parameter_response({"mowHeightLevel": 7})

    result = GetAreaParameter()._handle_response(event_bus, response)

    assert _areas_for(event_bus) == {}
    assert result.state is HandlingState.SUCCESS


def test_get_area_parameter_rejects_a_missing_areaParameters_key() -> None:
    event_bus = Mock()
    response = {"ret": "ok", "resp": {"body": {"data": {}}}}

    result = GetAreaParameter()._handle_response(event_bus, response)

    assert result.state is HandlingState.ANALYSE
    event_bus.notify.assert_not_called()


def test_get_area_parameter_rejects_a_non_list_areaParameters() -> None:
    event_bus = Mock()
    response = {
        "ret": "ok",
        "resp": {"body": {"data": {"areaParameters": "not-a-list"}}},
    }

    result = GetAreaParameter()._handle_response(event_bus, response)

    assert result.state is HandlingState.ANALYSE
    event_bus.notify.assert_not_called()


def test_get_area_parameter_does_not_notify_on_a_failed_response() -> None:
    event_bus = Mock()

    GetAreaParameter()._handle_response(event_bus, {"ret": "fail", "errno": 4200})

    assert _areas_for(event_bus) == {}
    event_bus.notify.assert_not_called()


def _multipart_ok_response(*, batid: str = "b1", index: int = 0) -> dict:
    return {
        "ret": "ok",
        "resp": {
            "body": {
                "data": {
                    "batid": batid,
                    "index": index,
                    "infoSize": 498,
                    "subsets": "fragment-payload",
                }
            }
        },
    }


def test_get_area_set_command_shape() -> None:
    assert GetAreaSet.NAME == "getAreaSet"
    assert GetAreaSet()._args == {"mid": "1", "aid": "0", "type": "ar"}


def test_get_area_set_waits_for_the_stream_to_complete() -> None:
    """A single fragment is normal, not an error: the buffer just isn't full yet."""
    event_bus = Mock()
    command = GetAreaSet()

    with patch.object(command._buffer, "add", return_value=None) as add:
        result = command._handle_response(event_bus, _multipart_ok_response())

    add.assert_called_once_with("b1", 0, "fragment-payload", 498)
    assert result.state is HandlingState.SUCCESS
    event_bus.notify.assert_not_called()


def test_get_area_set_applies_names_once_the_stream_completes() -> None:
    event_bus = Mock()
    _areas_for(event_bus)["12"] = MowerArea(
        "12", mow_height_level=7, cut_mode=4, obstacle_height=15, angle=90
    )
    command = GetAreaSet()
    decoded = [[1, 12, "North lawn"], [1, 13, "South lawn"]]

    with patch.object(command._buffer, "add", return_value=b"decoded-blob"):
        with patch(
            "custom_components.ecovacs_mower.deebot_patch.commands.orjson.loads",
            return_value=decoded,
        ):
            result = command._handle_response(event_bus, _multipart_ok_response())

    areas = _areas_for(event_bus)
    assert areas["12"].name == "North lawn"
    # Raw parameter values already known for area 12 are untouched by a
    # getAreaSet answer: it only carries names, never the four parameters.
    assert areas["12"].mow_height_level == 7
    assert areas["13"].name == "South lawn"
    assert result.state is HandlingState.SUCCESS
    event_bus.notify.assert_called_once()


def test_get_area_set_removes_an_area_no_longer_reported() -> None:
    event_bus = Mock()
    _areas_for(event_bus)["12"] = MowerArea("12", name="North lawn")
    _areas_for(event_bus)["99"] = MowerArea("99", name="Removed")
    command = GetAreaSet()
    decoded = [[1, 12, "North lawn"]]

    with patch.object(command._buffer, "add", return_value=b"blob"):
        with patch(
            "custom_components.ecovacs_mower.deebot_patch.commands.orjson.loads",
            return_value=decoded,
        ):
            command._handle_response(event_bus, _multipart_ok_response())

    assert set(_areas_for(event_bus)) == {"12"}


def test_get_area_set_rejects_malformed_json_from_the_buffer() -> None:
    event_bus = Mock()
    command = GetAreaSet()

    with patch.object(command._buffer, "add", return_value=b"not json"):
        with patch(
            "custom_components.ecovacs_mower.deebot_patch.commands.orjson.loads",
            side_effect=TypeError,
        ):
            result = command._handle_response(event_bus, _multipart_ok_response())

    assert result.state is HandlingState.ANALYSE
    event_bus.notify.assert_not_called()


def test_get_area_set_rejects_a_decoded_payload_with_no_recognizable_rows() -> None:
    """Non-empty but every row is malformed: analyse, do not silently wipe areas."""
    event_bus = Mock()
    _areas_for(event_bus)["12"] = MowerArea("12", name="North lawn")
    command = GetAreaSet()

    with patch.object(command._buffer, "add", return_value=b"blob"):
        with patch(
            "custom_components.ecovacs_mower.deebot_patch.commands.orjson.loads",
            return_value=[["too-short"]],
        ):
            result = command._handle_response(event_bus, _multipart_ok_response())

    assert result.state is HandlingState.ANALYSE
    assert "12" in _areas_for(event_bus)
    event_bus.notify.assert_not_called()


def test_get_area_set_rejects_a_missing_subsets_key() -> None:
    event_bus = Mock()
    response = {"ret": "ok", "resp": {"body": {"data": {}}}}

    result = GetAreaSet()._handle_response(event_bus, response)

    assert result.state is HandlingState.ANALYSE
    event_bus.notify.assert_not_called()


def test_get_area_set_rejects_a_non_integer_index() -> None:
    event_bus = Mock()
    response = _multipart_ok_response()
    response["resp"]["body"]["data"]["index"] = "not-a-number"

    result = GetAreaSet()._handle_response(event_bus, response)

    assert result.state is HandlingState.ANALYSE
    event_bus.notify.assert_not_called()


def test_get_area_set_does_not_notify_on_a_failed_response() -> None:
    event_bus = Mock()

    GetAreaSet()._handle_response(event_bus, {"ret": "fail", "errno": 4200})

    event_bus.notify.assert_not_called()


def test_set_area_parameter_sends_the_complete_raw_payload() -> None:
    command = SetAreaParameter(
        area_id="12", mow_height_level=4, cut_mode=3, obstacle_height=15, angle=270
    )

    assert command.NAME == "setAreaParameter"
    assert command._args == {
        "areaID": "12",
        "mowHeightLevel": 4,
        "cutMode": 3,
        "obstacleHeight": 15,
        "angle": 270,
    }


def test_on_area_parameter_command_shape() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import OnAreaParameter

    assert OnAreaParameter.NAME == "onAreaParameter"


def test_on_area_parameter_applies_the_push_the_same_way_as_the_get_answer() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import OnAreaParameter

    event_bus = Mock()
    data = {
        "areaParameters": [
            {
                "areaID": "3",
                "mowHeightLevel": 6,
                "cutMode": 7,
                "obstacleHeight": 3,
                "angle": 227,
            }
        ]
    }

    result = OnAreaParameter._handle_body_data_dict(event_bus, data)

    assert _areas_for(event_bus)["3"].obstacle_height == 3
    assert result.state is HandlingState.SUCCESS
    event_bus.notify.assert_called_once()


def test_on_area_parameter_rejects_a_missing_areaParameters_key() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import OnAreaParameter

    event_bus = Mock()

    result = OnAreaParameter._handle_body_data_dict(event_bus, {})

    assert result.state is HandlingState.ANALYSE
    event_bus.notify.assert_not_called()
