import asyncio
import json

import pytest

from games.guess_number.agent import GameAgent
from games.guess_number.config import Settings
from robot_agents.protocol import GameRequest, GameResult

pytestmark = pytest.mark.e2e


def test_openai_calls_number_game_tools(run_directory):
    async def exercise():
        settings = Settings.from_environment()
        agent = GameAgent(settings)
        updates = []

        async def notify(result):
            updates.append(result)

        try:
            result = await agent.handle(GameRequest(action="start"), notify)
            assert [item["name"] for item in agent.last_tool_calls] == ["start_game", "send_message"]
            assert agent.target in range(1, 21)
            assert result.expected_input.kind == "answer"
            assert result.state.details["remaining_attempts"] == 5
            result = await agent.handle(GameRequest(action="input", text="给我提示",
                                                   revision=result.state.revision), notify)
            assert [item["name"] for item in agent.last_tool_calls] == ["get_hint", "send_message"]
            assert ("偶数" if agent.target % 2 == 0 else "奇数") in result.outputs[0].text
            result = await agent.handle(GameRequest(action="input", text=f"我猜{agent.target}",
                                                   revision=result.state.revision), notify)
            assert [item["name"] for item in agent.last_tool_calls] == ["submit_guess", "send_message"]
            assert not result.state.active
            assert result.expected_input.kind == "none"
            assert result.state.details["attempts"] == 1
            assert GameResult.model_validate_json(result.model_dump_json()) == result
            (run_directory / "calls.json").write_text(
                json.dumps(agent.last_tool_calls, ensure_ascii=False, indent=2),
            )
        finally:
            await agent.close()
    asyncio.run(exercise())
