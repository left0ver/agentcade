import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from games.guess_number.agent import NumberDecision
from robot_agents.main_agent import AgentDecision
from robot_agents.protocol import GameRequest, GameResult, Presentation, PublicState
from robot_agents.transport import Catalog


@pytest.mark.parametrize("data", [
    {"action": "input", "text": "企鹅"},
    {"action": "input", "revision": 1},
    {"action": "switch"},
    {"action": "end", "score": 100},
])
def test_game_request_rejects_missing_context_and_unknown_operations(data):
    with pytest.raises(ValidationError):
        GameRequest.model_validate(data)


def test_finished_game_cannot_schedule_more_work():
    with pytest.raises(ValidationError):
        GameResult(state=PublicState(active=False), next_action="prepare")


@pytest.mark.parametrize("decision", [
    {"action": "start"},
    {"action": "reply", "text": ""},
    {"action": "send", "text": "修改后的答案"},
    {"action": "switch", "game_id": "guess_number"},
])
def test_main_decision_schema_limits_available_actions(decision):
    with pytest.raises(ValidationError):
        AgentDecision(decision=decision)


@pytest.mark.parametrize("number", [0, 21])
def test_number_agent_rejects_out_of_range_guesses(number):
    with pytest.raises(ValidationError):
        NumberDecision(decision={"action": "guess", "number": number})


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
