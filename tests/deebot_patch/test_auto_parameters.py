"""The mower-wide (Auto mode) settings: their commands and events.

The followed app writes are driven through the library's own
``MqttClient._handle_p2p``, so what is tested is what the MQTT client really
does with the registered commands. Topics and payloads are the A1600 RTK's
from issue #23.
"""

from types import SimpleNamespace
from unittest.mock import Mock

from deebot_client.commands import COMMANDS_WITH_MQTT_P2P_HANDLING
from deebot_client.commands.json.border_switch import SetBorderSwitch
from deebot_client.commands.json.cut_direction import GetCutDirection, SetCutDirection
from deebot_client.const import DataType
from deebot_client.events import CutDirectionEvent
from deebot_client.message import HandlingState
from deebot_client.mqtt_client import MqttClient
import pytest

from custom_components.ecovacs_mower.deebot_patch import apply
from custom_components.ecovacs_mower.deebot_patch.areas import AREA_PARAMETER_KEYS
from custom_components.ecovacs_mower.deebot_patch.commands import (
    AUTO_PARAMETERS,
    AUTO_WRITE_COMMANDS,
    CutEfficiencyEvent,
    CutHeightEvent,
    GetCutEfficiency,
    GetCutHeight,
    GetObstacleHeight,
    ObstacleHeightEvent,
    SetCutEfficiency,
    SetCutHeight,
    SetObstacleHeight,
)

_APP = "HelperMQClientId-sts-ngiot-mqserver-eco1-34/ecosys/1234"
_MOWER = "did/xmp9ds/res"
_OK = (
    b'{"header":{"tzm":180,"ts":"1792573628910452821","fwVer":"1.17.11"},'
    b'"body":{"code":0,"msg":"ok"}}'
)
_REFUSED = b'{"header":{"fwVer":"1.17.11"},"body":{"code":3,"msg":"fail"}}'


def _set(data: str) -> bytes:
    return (
        b'{"body":{"data":' + data.encode() + b'},"header":{"channel":"Android",'
        b'"m":"request","pri":2,"reqid":"eabfme","ts":"1792573628262","ver":"0.0.22"}}'
    )


def _client(events: Mock) -> SimpleNamespace:
    """What _handle_p2p reads from the client: its pending requests and subscriptions."""
    return SimpleNamespace(
        _received_p2p_commands={},
        _subscriptions={"did": SimpleNamespace(events=events)},
    )


def _exchange(client, command: str, request: bytes, answer: bytes) -> None:
    topic = f"iot/p2p/{command}/{_APP}/{_MOWER}/q/b33n/j".split("/")
    MqttClient._handle_p2p(client, topic, request)
    topic = f"iot/p2p/{command}/{_MOWER}/{_APP}/p/b33n/j".split("/")
    MqttClient._handle_p2p(client, topic, answer)


def _published(events: Mock) -> list:
    return [call.args[0] for call in events.notify.call_args_list]


def test_the_parameters_follow_the_area_keys_with_their_commands() -> None:
    assert [
        (p.key, p.read.NAME, p.write.NAME, p.event, p.field) for p in AUTO_PARAMETERS
    ] == [
        ("mowHeightLevel", "getCutHeight", "setCutHeight", CutHeightEvent, "level"),
        ("cutMode", "getCutEfficiency", "setCutEfficiency", CutEfficiencyEvent, "level"),
        (
            "obstacleHeight",
            "getObstacleHeight",
            "setObstacleHeight",
            ObstacleHeightEvent,
            "level",
        ),
        ("angle", "getCutDirection", "setCutDirection", CutDirectionEvent, "angle"),
    ]
    assert tuple(p.key for p in AUTO_PARAMETERS) == AREA_PARAMETER_KEYS
    assert [p.write.get_command for p in AUTO_PARAMETERS] == [
        p.read for p in AUTO_PARAMETERS
    ]
    assert AUTO_WRITE_COMMANDS == tuple(p.write for p in AUTO_PARAMETERS)


@pytest.mark.parametrize(
    ("command", "data", "event"),
    [
        (GetCutHeight, {"level": 6}, CutHeightEvent(6)),
        (GetCutEfficiency, {"level": 2}, CutEfficiencyEvent(2)),
        (GetObstacleHeight, {"level": 3}, ObstacleHeightEvent(3)),
        (GetCutDirection, {"angle": 179}, CutDirectionEvent(179)),
    ],
)
def test_an_answer_publishes_its_own_event(command, data, event) -> None:
    events = Mock()

    result = command.handle_set_args(events, data)

    assert result.state is HandlingState.SUCCESS
    assert _published(events) == [event]


@pytest.mark.parametrize("data", [{}, {"level": "6"}, {"level": True}, {"lvl": 6}])
def test_an_unusable_answer_publishes_nothing(data) -> None:
    events = Mock()

    result = GetCutHeight.handle_set_args(events, data)

    assert result.state is HandlingState.ANALYSE
    events.notify.assert_not_called()


def test_apply_registers_the_writes_and_keeps_the_librarys_entries() -> None:
    apply()
    apply()
    table = COMMANDS_WITH_MQTT_P2P_HANDLING[DataType.JSON]

    assert table["setCutHeight"] is SetCutHeight
    assert table["setCutEfficiency"] is SetCutEfficiency
    assert table["setObstacleHeight"] is SetObstacleHeight
    assert table["setCutDirection"] is SetCutDirection
    assert table["setBorderSwitch"] is SetBorderSwitch


@pytest.mark.parametrize(
    ("command", "data", "event"),
    [
        ("setCutHeight", '{"level":7}', CutHeightEvent(7)),
        ("setCutEfficiency", '{"level":2}', CutEfficiencyEvent(2)),
        ("setObstacleHeight", '{"level":3}', ObstacleHeightEvent(3)),
        ("setCutDirection", '{"angle":90}', CutDirectionEvent(90)),
    ],
)
def test_an_accepted_app_write_is_followed(command, data, event) -> None:
    apply()
    events = Mock()

    _exchange(_client(events), command, _set(data), _OK)

    assert event in _published(events)


def test_a_refused_app_write_is_not_followed() -> None:
    apply()
    events = Mock()

    _exchange(_client(events), "setCutHeight", _set('{"level":7}'), _REFUSED)

    assert not any(isinstance(e, CutHeightEvent) for e in _published(events))
