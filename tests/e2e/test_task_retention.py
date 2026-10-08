import asyncio
import json
from uuid import uuid4

import pytest
from a2a.types import ListTasksRequest, TaskState

from games.guess_animal.agent import GameAgent
from robot_agents.protocol import GameRequest
from robot_agents.transport import connect_games, serve_game

pytestmark = pytest.mark.e2e


def test_real_game_tasks_are_evicted_across_game_sessions(run_directory):
    async def exercise():
        settings = GameAgent.load_settings()
        observations = []
        async with serve_game(GameAgent, settings, task_history_limit=1) as url:
            catalog = run_directory / "games.json"
            catalog.write_text(json.dumps([
                {"kind": "remote", "id": "guess_animal", "url": url},
            ]), encoding="utf-8")
            async with connect_games(catalog) as games:
                connection = games["guess_animal"]

                async def start(context_id, canceled):
                    request = GameRequest(action="start")
                    if canceled:
                        with pytest.raises(RuntimeError, match="TASK_STATE_CANCELED"):
                            await connection.send(context_id, request)
                        return None
                    return await connection.send(context_id, request)

                previous = None
                for index in range(3):
                    context_id = uuid4().hex
                    async with asyncio.TaskGroup() as group:
                        preparing = group.create_task(start(context_id, index == 2))
                        async with asyncio.timeout(15):
                            while True:
                                page = await connection.client.list_tasks(ListTasksRequest())
                                current = [task for task in page.tasks
                                           if task.context_id == context_id]
                                if current:
                                    assert current[0].status.state == TaskState.TASK_STATE_WORKING
                                    break
                                if preparing.done():
                                    await preparing
                                    raise AssertionError("生成任务没有进入执行状态")
                                await asyncio.sleep(0.05)
                        assert page.total_size == (1 if previous is None else 2)
                        if previous is not None:
                            assert previous in {task.id for task in page.tasks}
                        if index < 2:
                            result = await preparing
                            assert result.state.active and result.outputs[0].paths
                            ended = await connection.send(context_id, GameRequest(action="end"))
                            assert not ended.state.active
                        else:
                            ended = await connection.send(context_id, GameRequest(action="end"))
                            assert not ended.state.active
                            await preparing
                    page = await connection.client.list_tasks(ListTasksRequest(
                        include_artifacts=True,
                    ))
                    assert page.total_size == 1
                    assert page.tasks[0].context_id == context_id
                    expected = TaskState.TASK_STATE_COMPLETED
                    assert page.tasks[0].status.state == expected
                    assert len(page.tasks[0].artifacts) == 1
                    if previous is not None:
                        assert page.tasks[0].id != previous
                    previous = page.tasks[0].id
                    observations.append({
                        "context_id": context_id, "total_tasks": page.total_size,
                        "state": TaskState.Name(expected),
                        "artifact_bytes": sum(item.ByteSize() for item in page.tasks[0].artifacts),
                    })
        (run_directory / "retention.json").write_text(
            json.dumps(observations, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    asyncio.run(exercise())
