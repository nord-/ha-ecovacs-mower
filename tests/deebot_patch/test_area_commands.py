"""Tests for the area protocol commands: get/set area parameters and area set."""

import base64
import lzma
import struct
from unittest.mock import Mock, patch

import pytest
from deebot_client.message import HandlingState

from custom_components.ecovacs_mower.deebot_patch.areas import (
    MowerArea,
    _areas_for,
    fragments_for,
)
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


def _buffer_returning(blob: bytes | None):
    """Stub the fragment buffer, for tests about what happens after it."""
    buffer = Mock()
    buffer.add.return_value = blob
    return patch(
        "custom_components.ecovacs_mower.deebot_patch.commands.fragments_for",
        return_value=buffer,
    )


def test_get_area_set_command_shape() -> None:
    assert GetAreaSet.NAME == "getAreaSet"
    assert GetAreaSet("7")._args == {"mid": "7", "aid": "0", "type": "ar"}


def _device_info() -> dict:
    return {"did": "did", "class": "e4gqia"}


async def test_get_area_set_asks_about_the_map_in_use() -> None:
    from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
        register,
    )

    event_bus = Mock()
    register(event_bus).note_map("5")

    with patch.object(
        GetAreaSet,
        "_execute_api_request",
        autospec=True,
        return_value={"ret": "fail", "errno": 4200},
    ) as request:
        await GetAreaSet()._execute(Mock(), _device_info(), event_bus)

    sent = request.call_args[0][0]
    assert sent._args == {"mid": "5", "aid": "0", "type": "ar"}


async def test_get_area_set_sends_nothing_while_the_map_is_unknown() -> None:
    from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
        register,
    )

    event_bus = Mock()
    register(event_bus)

    with patch.object(GetAreaSet, "_execute_api_request") as request:
        result, response = await GetAreaSet()._execute(
            Mock(), _device_info(), event_bus
        )

    request.assert_not_called()
    assert result.state is HandlingState.FAILED
    assert response == {}


def test_get_area_set_waits_for_the_stream_to_complete() -> None:
    """A single fragment is normal, not an error: the buffer just isn't full yet."""
    event_bus = Mock()
    command = GetAreaSet()

    with _buffer_returning(None) as fragments_for:
        result = command._handle_response(event_bus, _multipart_ok_response())

    fragments_for.assert_called_once_with(event_bus)
    fragments_for.return_value.add.assert_called_once_with(
        "b1", 0, "fragment-payload", 498
    )
    assert result.state is HandlingState.SUCCESS
    event_bus.notify.assert_not_called()


def test_get_area_set_applies_names_once_the_stream_completes() -> None:
    event_bus = Mock()
    _areas_for(event_bus)["12"] = MowerArea(
        "12", mow_height_level=7, cut_mode=4, obstacle_height=15, angle=90
    )
    command = GetAreaSet()
    decoded = [[1, 12, "North lawn"], [1, 13, "South lawn"]]

    with _buffer_returning(b"decoded-blob"):
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

    with _buffer_returning(b"blob"):
        with patch(
            "custom_components.ecovacs_mower.deebot_patch.commands.orjson.loads",
            return_value=decoded,
        ):
            command._handle_response(event_bus, _multipart_ok_response())

    assert set(_areas_for(event_bus)) == {"12"}


def test_get_area_set_rejects_malformed_json_from_the_buffer() -> None:
    event_bus = Mock()
    command = GetAreaSet()

    with _buffer_returning(b"not json"):
        with patch(
            "custom_components.ecovacs_mower.deebot_patch.commands.orjson.loads",
            side_effect=TypeError,
        ):
            result = command._handle_response(event_bus, _multipart_ok_response())

    assert result.state is HandlingState.ANALYSE
    event_bus.notify.assert_not_called()


@pytest.mark.parametrize(
    "decoded",
    [
        pytest.param([["too-short"]], id="every-row-malformed"),
        # What the mower answers for a map it does not have.
        pytest.param([], id="empty"),
    ],
)
def test_get_area_set_without_area_ids_does_not_wipe_the_areas(decoded) -> None:
    event_bus = Mock()
    _areas_for(event_bus)["12"] = MowerArea("12", name="North lawn")
    command = GetAreaSet()

    with _buffer_returning(b"blob"):
        with patch(
            "custom_components.ecovacs_mower.deebot_patch.commands.orjson.loads",
            return_value=decoded,
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


def _ecovacs_lzma(payload: bytes) -> str:
    """Encode *payload* the way the mower sends ``subsets``.

    Base64 of an LZMA-alone stream whose 8-byte size field is cut to 4 bytes,
    which is what deebot-client's ``decompress_base64_data`` reads.
    """
    alone = lzma.compress(payload, format=lzma.FORMAT_ALONE)
    return base64.b64encode(
        alone[:5] + struct.pack("<I", len(payload)) + alone[13:]
    ).decode()


def _fragment_response(*, batid: str, index: int, subsets: str) -> dict:
    response = _multipart_ok_response(batid=batid, index=index)
    response["resp"]["body"]["data"]["subsets"] = subsets
    return response


def test_fragments_are_joined_by_index_whatever_order_they_arrive_in() -> None:
    """Completion is a successful decode, not ``infoSize`` (498 vs 196 bytes)."""
    event_bus = Mock()
    _areas_for(event_bus)["12"] = MowerArea("12", mow_height_level=7)
    command = GetAreaSet()
    blob = _ecovacs_lzma(b'[[1,12,"North lawn"],[1,13,"South lawn"]]')
    third = len(blob) // 3
    fragments = (blob[:third], blob[third : 2 * third], blob[2 * third :])

    results = [
        command._handle_response(
            event_bus,
            _fragment_response(batid="b7", index=index, subsets=fragments[index]),
        )
        for index in (2, 0)
    ]
    event_bus.notify.assert_not_called()
    results.append(
        command._handle_response(
            event_bus, _fragment_response(batid="b7", index=1, subsets=fragments[1])
        )
    )

    assert all(result.state is HandlingState.SUCCESS for result in results)
    assert _areas_for(event_bus) == {
        "12": MowerArea("12", name="North lawn", mow_height_level=7),
        "13": MowerArea("13", name="South lawn"),
    }
    event_bus.notify.assert_called_once()
    # A completed transfer leaves nothing behind for a later one to join.
    assert fragments_for(event_bus)._batches == {}


def test_two_mowers_reusing_a_batid_do_not_share_fragments() -> None:
    """The refresh command is built once per class and shared by its devices."""
    bus_a, bus_b = Mock(), Mock()
    command = GetAreaSet()
    blob_a = _ecovacs_lzma(b'[[1,12,"North lawn"]]')
    blob_b = _ecovacs_lzma(b'[[1,40,"Back yard"]]')
    half_a, half_b = len(blob_a) // 2, len(blob_b) // 2

    command._handle_response(
        bus_a, _fragment_response(batid="b1", index=0, subsets=blob_a[:half_a])
    )
    command._handle_response(
        bus_b, _fragment_response(batid="b1", index=0, subsets=blob_b[:half_b])
    )
    command._handle_response(
        bus_a, _fragment_response(batid="b1", index=1, subsets=blob_a[half_a:])
    )
    command._handle_response(
        bus_b, _fragment_response(batid="b1", index=1, subsets=blob_b[half_b:])
    )

    assert _areas_for(bus_a) == {"12": MowerArea("12", name="North lawn")}
    assert _areas_for(bus_b) == {"40": MowerArea("40", name="Back yard")}


def test_a_new_transfer_is_not_joined_onto_an_abandoned_one() -> None:
    event_bus = Mock()
    command = GetAreaSet()
    abandoned = _ecovacs_lzma(b'[[1,99,"Gone"]]')
    blob = _ecovacs_lzma(b'[[1,12,"North lawn"]]')

    for index, subsets in enumerate((abandoned[:12], abandoned[12:24])):
        command._handle_response(
            event_bus, _fragment_response(batid="b1", index=index, subsets=subsets)
        )
    command._handle_response(
        event_bus, _fragment_response(batid="b1", index=0, subsets=blob)
    )

    assert _areas_for(event_bus) == {"12": MowerArea("12", name="North lawn")}


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


@pytest.mark.parametrize(
    ("code", "state"),
    [(0, HandlingState.SUCCESS), (30005, HandlingState.FAILED)],
)
def test_set_area_parameter_reads_the_reply_code(code, state) -> None:
    # The transport says "ok" either way; only body.code says whether the
    # mower took the values.
    command = SetAreaParameter(
        area_id="12", mow_height_level=4, cut_mode=3, obstacle_height=15, angle=270
    )
    response = {"ret": "ok", "resp": {"header": {}, "body": {"code": code}}}

    assert command._handle_response(Mock(), response).state is state


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


def test_a_partial_push_keeps_the_fields_it_does_not_carry() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import OnAreaParameter

    event_bus = Mock()
    _areas_for(event_bus)["3"] = MowerArea(
        "3", mow_height_level=6, cut_mode=7, obstacle_height=3, angle=227
    )
    data = {"areaParameters": [{"areaID": "3", "angle": 90, "cutMode": "bad"}]}

    OnAreaParameter._handle_body_data_dict(event_bus, data)

    assert _areas_for(event_bus)["3"] == MowerArea(
        "3", mow_height_level=6, cut_mode=7, obstacle_height=3, angle=90
    )


def test_on_area_parameter_rejects_a_missing_areaParameters_key() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import OnAreaParameter

    event_bus = Mock()

    result = OnAreaParameter._handle_body_data_dict(event_bus, {})

    assert result.state is HandlingState.ANALYSE
    event_bus.notify.assert_not_called()
