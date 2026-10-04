import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import aclosing

from watcherobot.application import ApplicationContext
from watcherobot.media import AudioFormat

from guess_animal.animals import COMMAND_HOTWORDS, Animal
from guess_animal.config import Settings
from guess_animal.game import Game, Reply
from guess_animal.generator import AnimalGenerator
from guess_animal.judge import JevJudge
from guess_animal.speech import QwenSpeech


class RobotGame:
    def __init__(self, app: ApplicationContext, settings: Settings):
        self.app = app
        self.robot = app.robot
        self.game = Game()
        self.speech = QwenSpeech(settings)
        self.judge = JevJudge(settings)
        self.generator = AnimalGenerator(settings)

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
            return await self.speech.recognize(
                audio, hotwords=COMMAND_HOTWORDS, timeout=seconds + 30
            )

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
            scale=1.0,
            scale_x=2.0,
            scale_y=2.0,
            left_upper_lid_y=-80,
            right_upper_lid_y=-80,
            left_lower_lid_y=80,
            right_lower_lid_y=80,
            left_upper_lid_rotation_deg=0,
            right_upper_lid_rotation_deg=0,
            left_lower_lid_rotation_deg=0,
            right_lower_lid_rotation_deg=0,
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
            scale=0.5,
            scale_x=1.0,
            scale_y=1.0,
            # 眼睑遮罩覆盖原有眼睛，自定义动物图案位于独立图层。
            left_upper_lid_y=0,
            right_upper_lid_y=0,
            left_lower_lid_y=0,
            right_lower_lid_y=0,
            left_upper_lid_rotation_deg=0,
            right_upper_lid_rotation_deg=0,
            left_lower_lid_rotation_deg=0,
            right_lower_lid_rotation_deg=0,
            color="#FFFFFF",
            transition_ms=0,
        )
        for path in steps[1:]:
            await asyncio.sleep(0.16)
            await asyncio.to_thread(self.robot.expression_runtime.update, custom_vector_path=path)

    async def present(self, reply: Reply) -> None:
        if reply.clear or reply.prepare:
            await self.clear()
        if reply.text:
            await self.speak(reply.text)
        if reply.prepare:
            await self.prepare_question()

    async def prepare_question(self) -> None:
        number = self.game.index + 1
        await self.speak(
            f"正在生成第{number}题，大约需要半分钟到一分钟。准备好了我会告诉你，请稍等。"
        )
        started = time.monotonic()
        self.app.logger.info("正在生成第 %s 题", number)
        (animal,) = await self.generator.generate(1)
        self.app.logger.info("单题生成完成，耗时 %.1f 秒", time.monotonic() - started)
        await self.draw(animal)
        await self.speak(f"第{number}题画好了！请看图，说出动物名称。")
        self.game.set_question(animal)
        self.app.logger.info("第 %s 题准备完成，耗时 %.1f 秒", number, time.monotonic() - started)

    async def play(self) -> None:
        await self.clear()
        await self.speak("想玩游戏，请说我们来玩你画我猜。")
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
            await self.play()
        finally:
            try:
                await self.generator.close()
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
