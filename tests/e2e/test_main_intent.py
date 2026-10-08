import asyncio

import pytest

from robot_agents.config import ROOT, Settings
from robot_agents.main_agent import MainAgent
from robot_agents.transport import connect_games

pytestmark = pytest.mark.e2e


def test_main_calls_tools_and_preserves_game_during_chat():
    async def exercise():
        async with connect_games(ROOT / "games.json") as games:
            agent = MainAgent(Settings.from_environment(), games)
            try:
                await agent.receive("开始猜数字游戏。")
                await agent.output_done(await agent.outputs.get())
                assert agent.active.game_id == "guess_number"
                assert agent.active.expected_input.kind == "answer"
                before = agent.status()
                for text, tool in (
                    ("十为什么是偶数？", None),
                    ("企鹅为什么不会飞？", None),
                    ("给我一个提示。", "send_message"),
                    ("现在还有多少次机会？", "send_message"),
                    ("继续游戏。", "send_message"),
                    ("这个游戏怎么玩？", "send_message"),
                    ("有哪些游戏？", "list_games"),
                ):
                    await agent.receive(text)
                    await agent.output_done(await agent.outputs.get())
                    calls = agent.last_tool_calls
                    assert ([call["name"] for call in calls] == [tool] if tool else not calls), (text, calls)
                    if tool == "send_message":
                        assert calls[0]["arguments"] == {"text": text}
                        assert calls[0]["result"]["state"] == agent.active.state.model_dump()
                        assert calls[0]["result"]["expected_input"] == agent.active.expected_input.model_dump()
                    assert agent.status() == before
                await agent.receive("结束游戏。")
                assert agent.last_tool_calls[0]["name"] == "end_game"
                await agent.output_done(await agent.outputs.get())
                assert agent.active is None
            finally:
                await agent.close()
    asyncio.run(exercise())
