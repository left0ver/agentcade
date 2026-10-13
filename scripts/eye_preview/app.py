import asyncio
import sys
import time
from pathlib import Path

from watcherobot.application import ApplicationContext
from watcherobot.inputs import RollerEvent

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agentcade.eyes import HIDDEN_EYES, LOOKS, STANDBY_EYES
from agentcade.vector import Stroke, encode_vector

AUTO_SECONDS = 8
TOTAL_SECONDS = 600
LEFT, RIGHT, TOP, MIDDLE, BOTTOM = 150, 262, 106, 206, 306
BAR = ((LEFT, MIDDLE), (RIGHT, MIDDLE))
DIGITS = (
    (((176, 136), (206, TOP), (206, BOTTOM)),),
    (
        (
            (LEFT, TOP),
            (RIGHT, TOP),
            (RIGHT, MIDDLE),
            (LEFT, MIDDLE),
            (LEFT, BOTTOM),
            (RIGHT, BOTTOM),
        ),
    ),
    (((LEFT, TOP), (RIGHT, TOP), (RIGHT, BOTTOM), (LEFT, BOTTOM)), BAR),
    (((LEFT, TOP), (LEFT, MIDDLE), (RIGHT, MIDDLE)), ((RIGHT, TOP), (RIGHT, BOTTOM))),
    (
        (
            (RIGHT, TOP),
            (LEFT, TOP),
            (LEFT, MIDDLE),
            (RIGHT, MIDDLE),
            (RIGHT, BOTTOM),
            (LEFT, BOTTOM),
        ),
    ),
    (
        (
            (RIGHT, TOP),
            (LEFT, TOP),
            (LEFT, BOTTOM),
            (RIGHT, BOTTOM),
            (RIGHT, MIDDLE),
            (LEFT, MIDDLE),
        ),
    ),
    (((LEFT, TOP), (RIGHT, TOP), (206, BOTTOM)),),
    (((LEFT, TOP), (RIGHT, TOP), (RIGHT, BOTTOM), (LEFT, BOTTOM), (LEFT, TOP)), BAR),
    (
        (
            (RIGHT, MIDDLE),
            (LEFT, MIDDLE),
            (LEFT, TOP),
            (RIGHT, TOP),
            (RIGHT, BOTTOM),
            (LEFT, BOTTOM),
        ),
    ),
)


async def main() -> None:
    async with ApplicationContext.from_environment() as app:
        runtime = app.robot.expression_runtime
        inputs = app.robot.inputs
        deadline = time.monotonic() + TOTAL_SECONDS
        index = 0
        automatic = True

        def finished() -> bool:
            return app.shutdown_requested or time.monotonic() >= deadline

        app.logger.info("自动轮播 %s 种眼型；转动滚轮或点击屏幕可手动切换", len(LOOKS))
        while not finished():
            name, look = LOOKS[index]
            app.logger.info("眼型 %s：%s", index + 1, name)
            number = encode_vector(tuple(Stroke(points, 14) for points in DIGITS[index]))
            await asyncio.to_thread(
                runtime.start, "standby", **HIDDEN_EYES, custom_vector_path=number
            )
            await asyncio.sleep(1.2)
            inputs.clear()
            await asyncio.to_thread(runtime.start, "standby", **look)
            started = time.monotonic()
            step = 0
            while not step and not finished():
                if automatic and time.monotonic() - started >= AUTO_SECONDS:
                    step = 1
                    break
                try:
                    event = await asyncio.to_thread(inputs.wait, 1.0)
                except TimeoutError:
                    continue
                # 手动切换后停止自动轮播，方便停在一种眼型上细看。
                automatic = False
                step = -1 if isinstance(event, RollerEvent) and event.delta < 0 else 1
            index = (index + step) % len(LOOKS)
        await asyncio.to_thread(runtime.start, "standby", **STANDBY_EYES)


if __name__ == "__main__":
    asyncio.run(main())
