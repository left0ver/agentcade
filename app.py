import asyncio

from watcherobot.application import ApplicationContext

from guess_animal.config import Settings
from guess_animal.robot_game import run_application


async def main() -> None:
    settings = Settings.from_environment()
    async with ApplicationContext.from_environment() as app:
        await run_application(app, settings)


if __name__ == "__main__":
    asyncio.run(main())
