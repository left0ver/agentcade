import asyncio
import sys
from pathlib import Path

# 源码在 src 下，SDK 直接运行这个文件，先把 src 加进查找路径。
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from agentcade.application import run_application
from agentcade.device import DeviceContext


async def main() -> None:
    async with DeviceContext.from_environment() as app:
        await run_application(app)


if __name__ == "__main__":
    asyncio.run(main())
