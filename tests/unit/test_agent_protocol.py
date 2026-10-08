import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from games.guess_number.agent import GuessInput
from robot_agents.main_agent import StartGameInput
from robot_agents.protocol import GameRequest, GameResult, Presentation, PublicState
from robot_agents.tools import original_text_input
from robot_agents.transport import Catalog


@pytest.mark.parametrize("data", [
    {"action": "input", "text": "企鹅"},
    {"action": "input", "revision": 1},
    {"action": "switch"},
    {"action": "end", "score": 100},
    {"action": "timeout"},
    {"action": "prepare"},
    {"action": "advance"},
    {"action": "start", "protocol": "desktop-game/1"},
    {"action": "start", "protocol": "desktop-game/2"},
])
def test_game_request_rejects_missing_context_and_unknown_operations(data):
    with pytest.raises(ValidationError):
        GameRequest.model_validate(data)


def test_game_result_contains_only_state_and_presentation():
    with pytest.raises(ValidationError):
        GameResult(state=PublicState(active=False), next_action="prepare")


@pytest.mark.parametrize("arguments", [
    {}, {"game_id": ""}, {"game_id": "guess_number", "context_id": "invented"},
])
def test_main_tool_input_requires_registered_game_id_only(arguments):
    with pytest.raises(ValidationError):
        StartGameInput.model_validate(arguments)


@pytest.mark.parametrize("number", [0, 21])
def test_number_agent_rejects_out_of_range_guesses(number):
    with pytest.raises(ValidationError):
        GuessInput(number=number)


def test_game_result_requires_next_input_description():
    with pytest.raises(ValidationError):
        GameResult(state=PublicState())
    result = GameResult(state=PublicState(), expected_input={
        "kind": "answer", "description": "请猜一个整数", "examples": ["10"],
    })
    assert GameResult.model_validate_json(result.model_dump_json()) == result


def test_user_message_is_preserved_through_tool_and_protocol_validation():
    text = " 我猜是企鹅。\n"
    inputs = original_text_input(text)
    assert inputs.model_json_schema()["properties"]["text"]["const"] == text
    assert inputs(text=text).text == text
    with pytest.raises(ValidationError):
        inputs(text="企鹅")
    request = GameRequest(action="input", text=text, revision=1)
    assert GameRequest.model_validate_json(request.model_dump_json()).text == text


def test_public_state_accepts_nested_game_data():
    state = PublicState(details={"players": [{"name": "玩家", "score": 1}],
                                "board": [[1, None], [0, 2]], "ratio": 0.5})
    assert PublicState.model_validate_json(state.model_dump_json()) == state


def test_presentation_rejects_unknown_device_commands():
    with pytest.raises(ValidationError):
        Presentation(gesture="unregistered_motion")


def test_catalog_supports_local_and_remote_games():
    entries = Catalog.validate_python([
        {"kind": "local", "id": "guess_number", "factory": "games.guess_number.agent:GameAgent"},
        {"kind": "remote", "id": "guess_animal", "url": "http://127.0.0.1:8000"},
    ])
    assert [item.kind for item in entries] == ["local", "remote"]


def test_main_agent_has_no_concrete_game_imports_or_ids():
    source = Path(__file__).parents[2] / "robot_agents"
    prohibited = ("games", "guess_animal")
    for path in source.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(node.module == name or node.module.startswith(name + ".")
                               for name in prohibited), path
            elif isinstance(node, ast.Import):
                assert not any(alias.name == name or alias.name.startswith(name + ".")
                               for alias in node.names for name in prohibited), path
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in {"guess_number", "guess_animal"}, path
