import asyncio
import json
from uuid import uuid4

import pytest

from robot_agents.protocol import GameRequest
from robot_agents.transport import connect_games

pytestmark = pytest.mark.e2e


def test_local_game_requests_across_server_idle_timeout(run_directory):
    catalog = run_directory / "games.json"
    catalog.write_text(json.dumps([
        {"kind": "local", "id": "guess_number", "factory": "games.guess_number.agent:GameAgent"},
    ]), encoding="utf-8")

    async def worker(index):
        async with connect_games(catalog) as games:
            connection = games["guess_number"]
            # 在真实服务的五秒空闲超时附近继续请求，覆盖连接关闭时的竞争条件。
            delays = (4.95, 4.99, 4.999, 5.0, 5.001, 5.01)
            for iteration in range(len(delays)):
                context_id = uuid4().hex
                request = GameRequest(action="start")
                started = await connection.send(context_id, request)
                assert started.state.active
                await asyncio.sleep(delays[(index + iteration) % len(delays)])
                # 相同请求仍然返回相同结果，游戏会话能够跨 HTTP 连接继续。
                assert await connection.send(context_id, request) == started
                ended = await connection.send(context_id, GameRequest(action="end"))
                assert not ended.state.active

    async def exercise():
        async with asyncio.timeout(90):
            async with asyncio.TaskGroup() as group:
                for index in range(12):
                    group.create_task(worker(index))

    asyncio.run(exercise())


def test_game_services_release_background_tasks(run_directory, caplog):
    catalog = run_directory / "games.json"
    catalog.write_text(json.dumps([
        {"kind": "local", "id": "guess_number", "factory": "games.guess_number.agent:GameAgent"},
    ]), encoding="utf-8")

    async def exercise():
        before = asyncio.all_tasks()
        async with connect_games(catalog) as games:
            for _ in range(3):
                context_id = uuid4().hex
                await games["guess_number"].send(context_id, GameRequest(action="start"))
                await games["guess_number"].send(context_id, GameRequest(action="end"))
        pending = [task for task in asyncio.all_tasks() - before if not task.done()]
        assert not pending, pending

    asyncio.run(exercise())
    assert "Dispatcher task is not running" not in caplog.text
