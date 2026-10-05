import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

from robot_agents.expressions import segmented_progress_vector


class ProgressDisplay:
    def __init__(self, show: Callable[[str], Awaitable[None]], lock: asyncio.Lock):
        self.show = show
        self.lock = lock
        self.task: asyncio.Task | None = None
        self.token = 0
        self.active = False
        self.step = 0

    async def frame(self, token: int, step: int) -> None:
        async with self.lock:
            if self.active and token == self.token:
                await self.show(segmented_progress_vector(step))
                self.step = step

    async def start(self) -> int:
        await self.cancel()
        self.active = True
        self.step = 0
        await self.frame(self.token, 0)
        return self.token

    def advance(self, token: int, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("预计生成时间必须是有限正数")
        if not self.active or token != self.token:
            return
        if self.task is not None:
            raise RuntimeError("当前进度任务已经开始计时")
        self.task = asyncio.create_task(self.animate(token, seconds))

    async def animate(self, token: int, seconds: float) -> None:
        started = time.monotonic()
        while self.active and token == self.token and self.step < 9:
            deadline = started + (self.step + 1) * seconds / 10
            await asyncio.sleep(max(0, deadline - time.monotonic()))
            step = min(9, int((time.monotonic() - started) * 10 / seconds))
            await self.frame(token, step)

    async def stop_timer(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def finish(self) -> None:
        await self.stop_timer()
        if self.active:
            await self.frame(self.token, 10)
            self.active = False
            self.token += 1
            await asyncio.sleep(0.25)

    async def cancel(self) -> None:
        self.active = False
        self.token += 1
        await self.stop_timer()
        async with self.lock:
            pass

    async def check(self) -> None:
        if self.task is not None and self.task.done():
            await self.task
