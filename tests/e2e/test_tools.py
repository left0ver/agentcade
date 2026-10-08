import asyncio
import json
from dataclasses import replace

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from games.guess_number.agent import GameAgent
from games.guess_number.config import Settings
from robot_agents.config import LLMSettings
from robot_agents.game_tools import GameTurn
from robot_agents.protocol import GameRequest, GameResult
from robot_agents.tool_server import serve_tools

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("provider", ["openai", "claude_code", "codex"])
def test_real_provider_calls_number_game_tools(provider, run_directory):
    async def exercise():
        settings = Settings.from_environment()
        if provider != "openai":
            settings = replace(settings, llm=LLMSettings(provider=provider))
        assert settings.llm.provider == provider
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
            (run_directory / f"{provider}-calls.json").write_text(
                json.dumps(agent.last_tool_calls, ensure_ascii=False, indent=2),
            )
        finally:
            await agent.close()
    asyncio.run(exercise())


def test_mcp_exposes_live_game_schemas_and_executes_tools():
    async def exercise():
        before = asyncio.all_tasks()
        agent = GameAgent(Settings.from_environment())
        updates = []

        async def notify(result):
            updates.append(result)

        turn = GameTurn(GameRequest(action="start"), notify, agent.result)
        registry = agent.tools_for(turn)
        try:
            async with (
                serve_tools(registry) as url,
                streamable_http_client(url) as (reader, writer, _),
                ClientSession(reader, writer) as client,
            ):
                await client.initialize()
                definitions = await client.list_tools()
                assert {item.name: item.inputSchema for item in definitions.tools} == {
                    item["name"]: item["parameters"] for item in registry.definitions()
                }
                started = await client.call_tool("start_game", {})
                assert not started.isError
                assert agent.active and agent.target in range(1, 21)
                sent = await client.call_tool("send_message", {})
                assert not sent.isError and registry.completed.is_set()
            result = turn.finish()
            assert result.state.active and result.expected_input.kind == "answer"
            assert result.outputs[0].text
        finally:
            await agent.close()
        assert not [task for task in asyncio.all_tasks() - before if not task.done()]
    asyncio.run(exercise())
