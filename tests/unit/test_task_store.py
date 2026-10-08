import asyncio

import pytest
from a2a.server.context import ServerCallContext
from a2a.types import ListTasksRequest, Task, TaskState, TaskStatus

from robot_agents.task_store import GameTaskStore


def test_finished_tasks_are_bounded_and_active_tasks_remain():
    async def exercise():
        store = GameTaskStore(max_completed=3)
        context = ServerCallContext()
        active = Task(id="active", context_id="current-game", status=TaskStatus(
            state=TaskState.TASK_STATE_WORKING,
        ))
        await store.save(active, context)
        states = (
            TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_CANCELED,
            TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED,
        )
        for index in range(12):
            task = Task(id=str(index), context_id=f"game-{index}", status=TaskStatus(
                state=states[index % len(states)],
            ))
            await store.save(task, context)
            page = await store.list(ListTasksRequest(), context)
            assert page.total_size <= 4
            assert await store.get("active", context) == active
        page = await store.list(ListTasksRequest(), context)
        assert {task.id for task in page.tasks} == {"active", "9", "10", "11"}
        assert await store.get("0", context) is None

    asyncio.run(exercise())


def test_task_updates_and_deletes_preserve_retention_order():
    async def exercise():
        store = GameTaskStore(max_completed=2)
        context = ServerCallContext()
        tasks = [Task(id=str(index), context_id="game", status=TaskStatus(
            state=TaskState.TASK_STATE_COMPLETED,
        )) for index in range(4)]
        await store.save(tasks[0], context)
        await store.save(tasks[1], context)
        await store.save(tasks[0], context)
        await store.save(tasks[2], context)
        assert await store.get("0", context) == tasks[0]
        assert await store.get("1", context) is None
        await store.delete("0", context)
        await store.save(tasks[3], context)
        page = await store.list(ListTasksRequest(), context)
        assert {task.id for task in page.tasks} == {"2", "3"}
        tasks[2].status.state = TaskState.TASK_STATE_WORKING
        await store.save(tasks[2], context)
        await store.save(tasks[0], context)
        await store.save(tasks[1], context)
        assert await store.get("2", context) == tasks[2]
        assert await store.get("3", context) is None

    asyncio.run(exercise())


@pytest.mark.parametrize("limit", [0, -1])
def test_task_retention_requires_positive_limit(limit):
    with pytest.raises(ValueError):
        GameTaskStore(max_completed=limit)
