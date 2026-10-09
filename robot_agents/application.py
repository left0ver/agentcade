import asyncio
from contextlib import aclosing

from observability.server import serve
from robot_agents.main_agent import MainAgent
from robot_agents.microphone import MicrophoneInput
from robot_agents.output import OutputCoordinator
from robot_agents.protocol import Presentation
from robot_agents.speech_input import SpeechInput
from robot_agents.transport import connect_games


class RobotApplication:
    def __init__(self, app, settings, games):
        self.app, self.settings = app, settings
        self.input = MicrophoneInput(app.robot)
        self.agent = MainAgent(settings, games)
        self.output = OutputCoordinator(app, settings, self.input)
        self.listener = None

    async def listen(self, ready):
        self.listener = SpeechInput(self.settings, self.agent, self.input, self.app.logger)
        async with aclosing(self.input.audio()) as audio:
            await self.listener.run(audio, ready)

    async def run(self):
        required = {"expression.runtime.v3", "microphone", "audio.stream"}
        for game in self.agent.games.values():
            required.update(game.descriptor.required_capabilities)
        try:
            missing = [name for name in required if not self.app.robot.supports(name)]
            if missing:
                raise RuntimeError("机器人未连接或缺少能力：" + ", ".join(sorted(missing)))
            await self.output.clear()
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(self.input.capture()),
                    group.create_task(self.output.run(self.agent)),
                ]
                ready = asyncio.Event()
                tasks.append(group.create_task(self.listen(ready)))
                await ready.wait()
                game_names = "、".join(game.descriptor.name for game in self.agent.games.values())
                greeting = (
                    f"你好呀！现在可以玩{game_names}。想玩哪个？也可以和我聊聊天。"
                    if game_names else "你好呀！现在还没有可玩的游戏，我们可以聊聊天。"
                )
                await self.agent.emit([Presentation(
                    text=greeting,
                )])
                while not self.app.shutdown_requested:
                    if (not self.output.speaking and not self.listener.busy
                            and not self.listener.input_speaking):
                        await self.agent.tick()
                    await self.output.progress.check()
                    await asyncio.sleep(0.1)
                for task in tasks:
                    task.cancel()
        finally:
            try:
                await self.agent.close()
            finally:
                await self.output.close()


async def run_application(app, settings):
    async with (
        serve(settings.observability_port) as url,
        connect_games(settings.game_catalog) as games,
    ):
        app.logger.info("可观测性页面：%s", url)
        await RobotApplication(app, settings, games).run()
