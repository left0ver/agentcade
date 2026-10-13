import asyncio

from robot_agents.application import run_application
from robot_agents.device import DeviceContext


async def main() -> None:
    async with DeviceContext.from_environment() as app:
        await run_application(app)


if __name__ == "__main__":
    asyncio.run(main())
