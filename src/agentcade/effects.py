"""常用的设备效果：点头、摇头、颜文字表情、逐笔画图和进度条。

这些是游戏端的辅助函数，把一个效果拼成几次机器人 SDK 调用，通过游戏的 self.robot 发出，
和直接调用 SDK 一样：用到动作要在 SKILL.md 里声明 motion，用到屏幕要声明 expression.runtime.v3。
"""
import asyncio
import logging
import math
import time

from agentcade.expressions import expression_vector, segmented_progress_vector
from agentcade.eyes import HIDDEN_EYES, STANDBY_EYES
from agentcade.robot_calls import RobotProxy

logger = logging.getLogger(__name__)

# 点头、摇头依次转到的（水平角, 俯仰角），从正前方出发再回到正前方。
GESTURES = {
    "nod": ((90, 115), (90, 105), (90, 115)),
    "shake_head": ((90, 115), (105, 115), (75, 115), (90, 115)),
}
# 颜文字表情的图案编号。
EXPRESSIONS = {"happy": "009", "sorry": "141"}
# 逐笔画图时每一步之间的间隔秒数。
DRAW_INTERVAL = 0.16


async def gesture(robot: RobotProxy, name: str) -> None:
    """点头（nod）或摇头（shake_head）。主 Agent 等动作做完再执行后面的指令和播报。"""
    for pan, tilt in GESTURES[name]:
        await robot.motion.move_to(pan_deg=pan, tilt_deg=tilt, duration_ms=350, profile="ease_in_out")


async def show(robot: RobotProxy, path: str) -> None:
    """屏幕只显示这个矢量图案，眼睛藏起来。"""
    await robot.expression_runtime.start("standby", **HIDDEN_EYES, tag="none", custom_vector_path=path)


async def standby(robot: RobotProxy) -> None:
    """屏幕回到待机的眼睛。"""
    await robot.expression_runtime.start("standby", **STANDBY_EYES)


async def react(robot: RobotProxy, expression: str, gesture_name: str, *, then: str | None = None) -> None:
    """对用户的回答做出反应：显示开心（happy）或抱歉（sorry）的颜文字，同时点头或摇头；
    做完后屏幕换回 then 这个图案，没有时回到待机的眼睛。"""
    await show(robot, expression_vector(EXPRESSIONS[expression]))
    await gesture(robot, gesture_name)
    await (show(robot, then) if then else standby(robot))


async def draw(robot: RobotProxy, steps: list[str], *, interval: float = DRAW_INTERVAL) -> None:
    """逐笔画出图案：每一项是一步的完整矢量路径，最后一步留在屏幕上。"""
    await show(robot, steps[0])
    for path in steps[1:]:
        await asyncio.sleep(interval)
        await robot.expression_runtime.update(custom_vector_path=path)


class Progress:
    """显示进度条，预计 seconds 秒走完，用在耗时的操作外面：

        async with Progress(self.robot, 30):
            ...

    操作正常完成时进度条走满；出错或者被取消时停在当前位置。
    """

    def __init__(self, robot: RobotProxy, seconds: float):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("预计秒数必须是有限正数")
        self.robot, self.seconds = robot, seconds
        self.task: asyncio.Task | None = None

    async def __aenter__(self):
        await show(self.robot, segmented_progress_vector(0))
        self.task = asyncio.create_task(self.animate())
        return self

    async def animate(self) -> None:
        started = time.monotonic()
        for step in range(1, 10):
            await asyncio.sleep(max(0.0, started + step * self.seconds / 10 - time.monotonic()))
            await show(self.robot, segmented_progress_vector(step))

    async def __aexit__(self, kind, error, traceback):
        self.task.cancel()
        # 用 wait 等它停下，不吞掉外面对游戏的取消。
        await asyncio.wait({self.task})
        if not self.task.cancelled() and self.task.exception() is not None:
            # 进度条只是提示，显示失败不影响正在做的事。
            logger.warning("进度条没有显示完整", exc_info=self.task.exception())
        if kind is None:
            await show(self.robot, segmented_progress_vector(10))
            await asyncio.sleep(0.25)
        return False
