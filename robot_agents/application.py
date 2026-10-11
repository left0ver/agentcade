import asyncio
import logging

from observability.server import serve
from robot_agents.game_client import connect_games
from robot_agents.main_agent import MainAgent
from robot_agents.microphone import MicrophoneInput
from robot_agents.output import OutputCoordinator
from robot_agents.protocol import Presentation
from robot_agents.speech_input import SpeechInput


class RobotApplication:
    def __init__(self, app, settings, games, microphone, output):
        self.app, self.settings = app, settings
        self.input = microphone
        self.agent = MainAgent(settings, games)
        self.output = output
        self.listener = None

    async def listen(self, ready):
        self.listener = SpeechInput(self.settings, self.agent, self.input, self.app.logger)
        await self.listener.run(ready)

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
                    f"你好呀，我是小懒！现在可以玩{game_names}。想玩哪个？也可以和我聊聊天。"
                    if game_names else "你好呀，我是小懒！现在还没有可玩的游戏，我们可以聊聊天。"
                )
                await self.agent.emit([Presentation(
                    text=greeting,
                )])
                while not self.app.shutdown_requested:
                    # 麦克风、识别和播报出错时各自恢复；通道断开后设备指令全部失效，只能退出。
                    if self.app.connection_lost:
                        raise RuntimeError("与机器人守护进程的连接已经断开，无法继续运行")
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


def route_logs(app):
    """各模块的告警和错误写入应用日志，和运行记录出现在同一个地方。"""
    for name in ("robot_agents", "games", "observability"):
        target = logging.getLogger(name)
        target.setLevel(logging.INFO)
        target.propagate = False
        target.handlers = list(app.logger.handlers)


async def run_application(app, settings):
    route_logs(app)
    microphone = MicrophoneInput(app.robot)
    output = OutputCoordinator(app, settings, microphone)
    async with (
        serve(settings.observability_port) as url,
        connect_games(settings.game_catalog, output.execute) as games,
    ):
        app.logger.info("可观测性页面：%s", url)
        await RobotApplication(app, settings, games, microphone, output).run()
