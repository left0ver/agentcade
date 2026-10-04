import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import aclosing

from watcherobot.application import ApplicationContext
from watcherobot.media import AudioFormat

from guess_animal.animals import HOTWORDS, Animal
from guess_animal.config import Settings
from guess_animal.game import Game, Reply
from guess_animal.judge import JevJudge
from guess_animal.speech import QwenSpeech


class RobotGame:
    def __init__(self, app: ApplicationContext, settings: Settings):
        self.app = app
        self.robot = app.robot
        self.game = Game()
        self.speech = QwenSpeech(settings)
        self.judge = JevJudge(settings)

    async def microphone(self, seconds: float) -> AsyncIterator[bytes]:
        session = await asyncio.to_thread(self.robot.microphone.open_pcm, queue_size=128)
        try:
            if session.format != AudioFormat():
                raise ValueError("机器人麦克风必须提供 16 kHz、单声道、16-bit PCM")
            deadline = time.monotonic() + seconds
            self.app.logger.info("正在接收语音")
            while time.monotonic() < deadline:
                frame = await asyncio.to_thread(session.read, timeout=2.0)
                if session.dropped_frames or session.decode_failures:
                    raise RuntimeError("机器人麦克风出现音频丢失或解码失败")
                yield frame.data
        finally:
            await asyncio.to_thread(session.close)

    async def listen(self) -> str | None:
        seconds = 20 if self.game.active else 60
        async with aclosing(self.microphone(seconds)) as audio:
            return await self.speech.recognize(audio, hotwords=HOTWORDS, timeout=seconds + 30)

    async def speak(self, text: str) -> None:
        self.app.logger.info("机器人：%s", text)
        pcm = await self.speech.synthesize(text)
        playback = await asyncio.to_thread(self.robot.audio.play_pcm, pcm)
        try:
            await asyncio.to_thread(playback.wait, timeout=playback.expected_duration_seconds + 10)
        finally:
            if not playback.state.terminal:
                await asyncio.to_thread(playback.cancel)

    async def clear(self) -> None:
        await asyncio.to_thread(
            self.robot.expression_runtime.start,
            "standby",
            accessory="none",
            auto_blink=True,
        )

    async def draw(self, animal: Animal) -> None:
        steps = animal.drawing_steps()
        await asyncio.to_thread(
            self.robot.expression_runtime.start,
            "standby",
            accessory="custom_vector",
            custom_vector_path=steps[0],
            custom_accessory_layer="front",
            accessory_scale=1.0,
            accessory_x=0.0,
            accessory_y=0.0,
            auto_blink=False,
            gaze_x=0.0,
            gaze_y=0.0,
            scale=0.7,
            color="#FFFFFF",
            transition_ms=0,
        )
        for path in steps[1:]:
            await asyncio.sleep(0.16)
            await asyncio.to_thread(self.robot.expression_runtime.update, custom_vector_path=path)

    async def present(self, reply: Reply) -> None:
        if reply.clear or reply.draw:
            await self.clear()
        if reply.text:
            await self.speak(reply.text)
        if reply.draw:
            animal = self.game.animal
            assert animal is not None
            await self.draw(animal)

    async def run(self) -> None:
        try:
            required = (
                "expression.runtime.v3",
                "expression.vector_accessory.v1",
                "audio.stream",
                "microphone",
            )
            missing = [capability for capability in required if not self.robot.supports(capability)]
            if missing:
                raise RuntimeError("机器人未连接或缺少所需能力：" + ", ".join(missing))
            await self.clear()
            await self.speak("我准备好了。想玩游戏，请说我们来玩你画我猜。")
            while not self.app.shutdown_requested:
                revision = self.game.revision
                text = await self.listen()
                if text is None:
                    await self.present(self.game.silence())
                    continue
                self.app.logger.info("用户：%s", text)
                decision = await self.judge.decide(text, self.game.animal)
                self.app.logger.info("Jev 判定：%s", decision.value)
                reply = self.game.handle(decision, revision)
                await self.present(reply)
                if reply.advance:
                    await self.present(self.game.advance())
        finally:
            await self.judge.close()


async def run_application(app: ApplicationContext, settings: Settings) -> None:
    async with asyncio.TaskGroup() as group:
        game_task = group.create_task(RobotGame(app, settings).run())
        while not game_task.done():
            if app.shutdown_requested:
                game_task.cancel()
                break
            await asyncio.sleep(0.2)
