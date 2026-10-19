import asyncio
import logging

from agentcade import settings_page
from agentcade.game_client import GameHub
from agentcade.main_agent import MainAgent
from agentcade.microphone import MicrophoneInput
from agentcade.output import OutputCoordinator
from agentcade.speech_input import SpeechInput
from observability.server import serve


def ordinal(number: int) -> str:
    """开机问候里的序号，十以内念成中文数字。"""
    return "一二三四五六七八九十"[number - 1] if 1 <= number <= 10 else str(number)


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
        # 游戏需要的能力在接入游戏时逐个检查，机器人不支持的游戏只是不接入。
        required = {"expression.runtime.v3", "microphone", "audio.stream"}
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
                games = "；".join(
                    f"第{ordinal(index)}个游戏是{game.descriptor.name}，它的玩法是{game.descriptor.summary}"
                    for index, game in enumerate(self.agent.games.values(), 1))
                greeting = (
                    f"你好呀，我是小懒！现在可以玩这些游戏：{games}。你可以跟我说你想玩的游戏，我也可以跟你聊天。"
                    if games else "你好呀，我是小懒！现在还没有可玩的游戏，我们可以聊聊天。"
                )
                await self.agent.reply(greeting)
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
    for name in ("agentcade", "games", "observability"):
        target = logging.getLogger(name)
        target.setLevel(logging.INFO)
        target.propagate = False
        target.handlers = list(app.logger.handlers)


async def run_application(app):
    route_logs(app)
    hub = GameHub()
    hub.supports = app.robot.supports
    # 页面先启动：服务配置还没填好时，用户要在它的设置页面里填写，也可以先管理游戏。
    async with serve(settings_page.port(), routes=settings_page.routes(hub)) as url:
        app.logger.info("对话页面：%s", url)
        settings = await settings_page.ready(app, url)
        if settings is None:
            return
        microphone = MicrophoneInput(app.robot)
        output = OutputCoordinator(app, settings, microphone)
        robot = RobotApplication(app, settings, hub.games, microphone, output)
        hub.in_use = lambda game_id: (robot.agent.active is not None
                                      and robot.agent.active.game_id == game_id)
        try:
            await hub.start()
            await robot.run()
        finally:
            await hub.close()
