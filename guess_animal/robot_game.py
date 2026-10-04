import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Literal

from watcherobot.application import ApplicationContext
from watcherobot.media import AudioFormat

from guess_animal.animals import COMMAND_HOTWORDS, Animal, encode_vector
from guess_animal.config import Settings
from guess_animal.expressions import expression_vector, generation_progress
from guess_animal.eyes import HIDDEN_EYES, STANDBY_EYES, Look
from guess_animal.game import Game, Reply
from guess_animal.generator import AnimalGenerator
from guess_animal.judge import JevJudge
from guess_animal.speech import QwenSpeech


class RobotGame:
    def __init__(self, app: ApplicationContext, settings: Settings):
        self.app = app
        self.robot = app.robot
        self.game = Game()
        self.generation_estimate = settings.drawing_estimate_seconds
        self.speech = QwenSpeech(settings)
        self.generator = AnimalGenerator(settings)
        self.judge = JevJudge(settings)
        self.app.logger.info("绘图 provider：%s", settings.drawing_provider)

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

    async def speak(
        self, text: str, *, feedback: Literal["correct", "incorrect"] | None = None
    ) -> None:
        animal_path = None
        if feedback == "incorrect":
            if self.game.animal is None:
                raise ValueError("答错反馈需要当前动物图案")
            animal_path = encode_vector(self.game.animal.strokes)
        self.app.logger.info("机器人：%s", text)
        pcm = await self.speech.synthesize(text)
        if feedback:
            await self.show_vector(expression_vector("009" if feedback == "correct" else "141"))
        async with asyncio.TaskGroup() as group:
            group.create_task(self.play_audio(pcm))
            if animal_path is not None:
                group.create_task(self.restore_animal(animal_path))

    async def restore_animal(self, path: str) -> None:
        await asyncio.sleep(1.5)
        await self.show_vector(path)

    async def play_audio(self, pcm: bytes) -> None:
        playback = await asyncio.to_thread(self.robot.audio.play_pcm, pcm)
        try:
            await asyncio.to_thread(playback.wait, timeout=playback.expected_duration_seconds + 10)
        finally:
            if not playback.state.terminal:
                await asyncio.to_thread(playback.cancel)

    async def clear(self, eyes: Look = STANDBY_EYES) -> None:
        await asyncio.to_thread(self.robot.expression_runtime.start, "standby", **eyes)

    async def draw(self, animal: Animal) -> None:
        steps = animal.drawing_steps()
        await self.show_vector(steps[0])
        for path in steps[1:]:
            await asyncio.sleep(0.16)
            await asyncio.to_thread(self.robot.expression_runtime.update, custom_vector_path=path)

    async def show_vector(self, path: str) -> None:
        await asyncio.to_thread(
            self.robot.expression_runtime.start,
            "standby",
            **HIDDEN_EYES,
            tag="none",
            custom_vector_path=path,
        )

    async def present(self, reply: Reply) -> None:
        if reply.clear:
            await self.clear()
        if reply.text:
            await self.speak(reply.text, feedback=reply.feedback)
        if reply.prepare:
            await self.prepare_question()

    async def animate_generation(self, started: float, completed: asyncio.Event) -> None:
        previous = 0
        while not completed.is_set():
            progress = generation_progress(time.monotonic() - started, self.generation_estimate)
            if progress != previous:
                await asyncio.to_thread(
                    self.robot.expression_runtime.update,
                    custom_vector_path=expression_vector("111", progress),
                )
                previous = progress
            await asyncio.sleep(0.4)

    async def prepare_question(self) -> None:
        number = self.game.index + 1
        await self.show_vector(expression_vector("111"))
        await self.speak(
            f"正在生成第{number}题，大约需要{self.generation_estimate}秒。准备好了我会告诉你，请稍等。"
        )
        started = time.monotonic()
        self.app.logger.info("正在生成第 %s 题", number)
        completed = asyncio.Event()
        async with asyncio.TaskGroup() as group:
            group.create_task(self.animate_generation(started, completed))
            try:
                (animal,) = await self.generator.generate(1)
            finally:
                completed.set()
        self.app.logger.info("单题生成完成，耗时 %.1f 秒", time.monotonic() - started)
        await asyncio.to_thread(
            self.robot.expression_runtime.update, custom_vector_path=expression_vector("111", 100)
        )
        await asyncio.sleep(0.25)
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
