import asyncio
from collections import OrderedDict

from a2a.server.context import ServerCallContext
from a2a.server.owner_resolver import resolve_user_scope
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import Task, TaskState

TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_REJECTED,
}


class GameTaskStore(InMemoryTaskStore):
    def __init__(self, *, max_completed: int = 64):
        if max_completed < 1:
            raise ValueError("已结束任务的保留数量必须大于零")
        super().__init__()
        self.max_completed = max_completed
        self.completed: OrderedDict[tuple[str, str], ServerCallContext] = OrderedDict()
        self.retention_lock = asyncio.Lock()

    async def save(self, task: Task, context: ServerCallContext) -> None:
        async with self.retention_lock:
            await super().save(task, context)
            key = (resolve_user_scope(context), task.id)
            self.completed.pop(key, None)
            if task.status.state in TERMINAL_STATES:
                self.completed[key] = context
            # 只清理已结束任务，生成中及等待用户输入的任务继续保留。
            while len(self.completed) > self.max_completed:
                (_, task_id), owner_context = self.completed.popitem(last=False)
                await super().delete(task_id, owner_context)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        async with self.retention_lock:
            await super().delete(task_id, context)
            self.completed.pop((resolve_user_scope(context), task_id), None)
