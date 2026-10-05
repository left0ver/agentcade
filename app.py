import asyncio

from watcherobot.application import ApplicationContext

from robot_agents.application import run_application
from robot_agents.config import Settings


async def main() -> None:
    settings = Settings.from_environment()
    async with ApplicationContext.from_environment() as app:
        await run_application(app, settings)


if __name__ == "__main__":
    asyncio.run(main())
