import asyncio
import os
import time
from contextlib import aclosing
from pathlib import Path

from robot_agents.config import ROOT
from robot_agents.main_agent import MainAgent
from robot_agents.microphone import MicrophoneInput
from robot_agents.output import OutputCoordinator
from robot_agents.protocol import Presentation
from robot_agents.speech import QwenSpeech
from robot_agents.transport import connect_games


class RobotApplication:
    def __init__(self, app, settings, games):
        self.app, self.settings = app, settings
        self.input = MicrophoneInput(app.robot)
        self.speech = QwenSpeech(settings)
        self.agent = MainAgent(settings, games)
        self.output = OutputCoordinator(app, settings, self.input)
        self.utterances = asyncio.Queue(32)

    def receive(self, utterance):
        self.app.logger.info("用户：%s", utterance.text)
        self.agent.last_activity = time.monotonic()
        self.utterances.put_nowait(utterance)

    async def respond(self):
        while True:
            utterance = await self.utterances.get()
            try:
                await self.agent.receive(utterance.text, started_at=utterance.started_at)
            finally:
                self.utterances.task_done()

    async def listen(self, ready):
        hotwords = ["结束游戏", "继续游戏", "有哪些游戏", "提示", "下一题"]
        for game in self.agent.games.values():
            hotwords.extend([game.descriptor.name, *game.descriptor.aliases])
        async with aclosing(self.input.audio()) as audio:
            await self.speech.transcribe(
                audio, self.receive, ready=ready, hotwords=tuple(hotwords),
                on_activity=lambda: setattr(self.agent, "last_activity", time.monotonic()),
            )

    async def run(self):
        required = {"expression.runtime.v3", "microphone", "audio.stream"}
        for game in self.agent.games.values():
            required.update(game.descriptor.required_capabilities)
        missing = [name for name in required if not self.app.robot.supports(name)]
        if missing:
            raise RuntimeError("机器人未连接或缺少能力：" + ", ".join(sorted(missing)))
        try:
            await self.output.clear()
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(self.input.capture()),
                    group.create_task(self.respond()),
                    group.create_task(self.output.run(self.agent)),
                ]
                ready = asyncio.Event()
                tasks.append(group.create_task(self.listen(ready)))
                await ready.wait()
                await self.agent.emit([Presentation(
                    text="我说完话后，你可以和我聊天，也可以说有哪些游戏，选择想玩的游戏。",
                )])
                while not self.app.shutdown_requested:
                    if not self.output.speaking:
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
    catalog = Path(os.getenv("GAME_CATALOG", str(ROOT / "games.json")))
    async with connect_games(settings, catalog) as games:
        await RobotApplication(app, settings, games).run()
