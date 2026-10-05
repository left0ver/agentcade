import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from dataclasses import dataclass
from typing import Literal

from watcherobot.application import ApplicationContext
from watcherobot.media import AudioFormat

from guess_animal.animals import COMMAND_HOTWORDS, Animal, encode_vector
from guess_animal.audio_stream import PlaybackCallback, RtcAudioPlayer, rtc_command
from guess_animal.config import Settings
from guess_animal.conversation import Conversation, ConversationState
from guess_animal.expressions import expression_vector, generation_progress
from guess_animal.eyes import HIDDEN_EYES, STANDBY_EYES, Look
from guess_animal.game import Decision, Game, Reply
from guess_animal.generator import AnimalGenerator
from guess_animal.judge import JevJudge
from guess_animal.microphone import MicrophoneInput
from guess_animal.speech import QwenSpeech, RecognizedSpeech


@dataclass
class SpeechRequest:
    text: str
    feedback: Literal["correct", "incorrect"] | None
    revision: int | None
    completed: asyncio.Future


class RobotGame:
    def __init__(self, app: ApplicationContext, settings: Settings):
        self.app = app
        self.robot = app.robot
        self.game = Game()
        self.generation_estimate = settings.drawing_estimate_seconds
        self.speech = QwenSpeech(settings)
        self.audio = RtcAudioPlayer(app.rtc)
        self.input = MicrophoneInput(self.robot)
        self.generator = AnimalGenerator(settings)
        self.judge = JevJudge(settings)
        self.conversation = Conversation(settings)
        self._tasks: asyncio.TaskGroup | None = None
        self._generation: asyncio.Task | None = None
        self._speech_queue: asyncio.Queue[SpeechRequest] | None = None
        self._utterances: asyncio.Queue[tuple[RecognizedSpeech, ConversationState]] = asyncio.Queue(32)
        self._states: deque[ConversationState] = deque(maxlen=256)
        self._speaking = False
        self._last_activity = time.monotonic()
        self._publish_state("等待交流")
        self.app.logger.info(
            "LLM provider：%s，模型：%s",
            settings.llm.provider, settings.llm.model_name or "CLI 默认模型",
        )

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
        self, text: str, *, feedback: Literal["correct", "incorrect"] | None = None,
        revision: int | None = None,
    ) -> None:
        if self._speech_queue is None:
            await self._speak_now(text, feedback=feedback)
            return
        completed = asyncio.get_running_loop().create_future()
        self._speech_queue.put_nowait(SpeechRequest(text, feedback, revision, completed))
        # 游戏任务取消时，正在播报的语音仍由独立的播报任务完整播放。
        await asyncio.shield(completed)

    async def _speak_now(
        self, text: str, *, feedback: Literal["correct", "incorrect"] | None = None
    ) -> None:
        animal_path = None
        if feedback == "incorrect":
            if self.game.animal is None:
                raise ValueError("答错反馈需要当前动物图案")
            animal_path = encode_vector(self.game.animal.strokes)
        self.app.logger.info("机器人：%s", text)
        await self.input.pause()
        async with asyncio.TaskGroup() as group:
            async def on_start() -> None:
                if feedback:
                    await self.show_vector(
                        expression_vector("009" if feedback == "correct" else "141")
                    )
                    group.create_task(self.perform_feedback_motion(feedback))
                if animal_path is not None:
                    group.create_task(self.restore_animal(animal_path))

            await self.play_audio(self.speech.synthesize_stream(text), on_start=on_start)
        self.input.resume()
        await self.input.wait_ready()

    async def perform_feedback_motion(self, feedback: Literal["correct", "incorrect"]) -> None:
        positions = {
            "correct": ((90, 115), (90, 105), (90, 115)),
            "incorrect": ((90, 115), (105, 115), (75, 115), (90, 115)),
        }[feedback]
        completed = False
        try:
            for pan, tilt in positions:
                job = await rtc_command(
                    self.robot.motion.move_to,
                    pan_deg=pan, tilt_deg=tilt, duration_ms=350, profile="ease_in_out",
                )
                await rtc_command(job.wait, 3.0)
                self.app.logger.info("反馈动作完成：%s，pan=%s，tilt=%s", feedback, pan, tilt)
            completed = True
        finally:
            if not completed:
                await rtc_command(self.robot.motion.stop)

    async def restore_animal(self, path: str) -> None:
        await asyncio.sleep(1.5)
        await self.show_vector(path)

    async def play_audio(
        self, chunks: AsyncIterator[bytes], *, on_start: PlaybackCallback | None = None
    ) -> None:
        metrics = await self.audio.play(chunks, on_start=on_start)
        self.app.logger.info(
            "流式语音：首段 PCM %.2f 秒，开始发送 %.2f 秒，播放完成 %.2f 秒，%s 字节",
            metrics.first_pcm_seconds, metrics.first_frame_seconds,
            metrics.completed_seconds, metrics.pcm_bytes,
        )

    async def clear(self, eyes: Look = STANDBY_EYES) -> None:
        await asyncio.to_thread(self.robot.expression_runtime.start, "standby", **eyes)

    async def draw(self, animal: Animal) -> None:
        steps = animal.drawing_steps()
        await self.show_vector(steps[0])
        for path in steps[1:]:
            await asyncio.sleep(0.16)
            await rtc_command(self.robot.expression_runtime.update, custom_vector_path=path)

    async def show_vector(self, path: str) -> None:
        await rtc_command(
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
            await self.speak(reply.text, feedback=reply.feedback, revision=self.game.revision)
        if reply.prepare:
            if self._tasks is None:
                await self.prepare_question()
            else:
                if self._generation is not None and not self._generation.done():
                    raise RuntimeError("已经存在题目生成任务")
                self._generation = self._tasks.create_task(self.prepare_question())

    async def animate_generation(self, started: float, completed: asyncio.Event) -> None:
        previous = 0
        while not completed.is_set():
            progress = generation_progress(time.monotonic() - started, self.generation_estimate)
            if progress != previous:
                await rtc_command(
                    self.robot.expression_runtime.update,
                    custom_vector_path=expression_vector("111", progress),
                )
                previous = progress
            await asyncio.sleep(0.4)

    async def prepare_question(self) -> None:
        number = self.game.index + 1
        revision = self.game.revision
        self._publish_state("生成题目")
        await self.show_vector(expression_vector("111"))
        await self.speak(
            f"正在生成第{number}题，大约需要{self.generation_estimate}秒。准备好了我会告诉你，请稍等。",
            revision=revision,
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
        await rtc_command(
            self.robot.expression_runtime.update, custom_vector_path=expression_vector("111", 100)
        )
        await asyncio.sleep(0.25)
        self._publish_state("绘制动物")
        await self.draw(animal)
        self.game.set_question(animal)
        self._publish_state("等待猜测")
        await self.speak(
            f"第{number}题画好了！请看图，说出动物名称。", revision=self.game.revision
        )
        self.app.logger.info("第 %s 题准备完成，耗时 %.1f 秒", number, time.monotonic() - started)

    def _publish_state(self, phase: str) -> None:
        self._states.append(ConversationState(
            time.monotonic(), self.game.revision,
            self.game.animal if not self.game.resolved else None, self.game.active, phase,
        ))
        self._touch_activity()

    def _touch_activity(self) -> None:
        self._last_activity = time.monotonic()

    def receive_utterance(self, utterance: RecognizedSpeech) -> None:
        state = next(
            (item for item in reversed(self._states) if item.changed_at <= utterance.started_at),
            self._states[0],
        )
        self._utterances.put_nowait((utterance, state))
        self._touch_activity()
        self.app.logger.info("接收到用户语音：%s（%s）", utterance.text, state.phase)

    async def _speak_pending(self) -> None:
        while True:
            request = await self._speech_queue.get()
            try:
                if request.revision is None or request.revision == self.game.revision:
                    self._speaking = True
                    await self._speak_now(request.text, feedback=request.feedback)
                request.completed.set_result(None)
            finally:
                self._speaking = False
                self._touch_activity()
                if not request.completed.done():
                    request.completed.cancel()
                self._speech_queue.task_done()

    async def _respond(self, utterance: RecognizedSpeech, state: ConversationState) -> None:
        decision = await self.judge.decide(
            utterance.text, state.animal, game_active=state.active
        )
        self.app.logger.info("Jev 判定：%s", decision.value)
        if decision is Decision.CHAT:
            self.game.silence_count = 0
            text = await self.conversation.answer(utterance.text, phase=self._states[-1].phase)
            await self.speak(text)
            return
        if decision is Decision.IGNORE:
            return
        revision = self.game.revision if decision in {Decision.START, Decision.END} else state.revision
        if revision != self.game.revision:
            self.app.logger.info("语音属于已经结束的题目：%s", utterance.text)
            await self.speak("刚才的题目已经结束，请看当前图案再回答。")
            return
        reply = self.game.handle(decision, revision)
        if decision is Decision.END and self._generation and not self._generation.done():
            self._generation.cancel()
            with suppress(asyncio.CancelledError):
                await self._generation
        if not self.game.active:
            self._publish_state("等待交流")
        elif self.game.resolved:
            self._publish_state("播报答案")
        elif self.game.animal is None:
            self._publish_state("生成题目")
            if not reply.text and not reply.prepare:
                reply = Reply("题目还在准备，你可以继续和我聊天。")
        await self.present(reply)
        if reply.advance:
            next_reply = self.game.advance()
            self._publish_state("生成题目" if self.game.active else "等待交流")
            await self.present(next_reply)

    async def _respond_pending(self) -> None:
        while True:
            try:
                async with asyncio.timeout(0.5):
                    utterance, state = await self._utterances.get()
            except TimeoutError:
                if (
                    self.game.animal is not None and not self.game.resolved
                    and not self._speaking and self._speech_queue.empty()
                    and time.monotonic() - self._last_activity >= 20
                ):
                    reply = self.game.silence()
                    if not self.game.active:
                        self._publish_state("等待交流")
                    await self.present(reply)
                continue
            try:
                await self._respond(utterance, state)
            finally:
                self._utterances.task_done()
                self._touch_activity()

    async def play(self) -> None:
        await self.clear()
        async with asyncio.TaskGroup() as group:
            self._tasks = group
            self._speech_queue = asyncio.Queue(32)
            ready = asyncio.Event()
            group.create_task(self.input.capture())
            group.create_task(self._listen_continuously(ready))
            group.create_task(self._speak_pending())
            group.create_task(self._respond_pending())
            await ready.wait()
            await self.speak("我说完话后，你可以和我聊天。想玩游戏，请说我们来玩你画我猜。")
            await asyncio.Event().wait()

    async def _listen_continuously(self, ready: asyncio.Event) -> None:
        async with aclosing(self.input.audio()) as microphone:
            await self.speech.transcribe(
                microphone, self.receive_utterance, on_activity=self._touch_activity,
                ready=ready, hotwords=COMMAND_HOTWORDS,
            )

    async def run(self) -> None:
        try:
            required = (
                "expression.runtime.v3",
                "expression.vector_accessory.v1",
                "rtc.audio.full_duplex.v1",
                "microphone",
                "motion",
            )
            missing = [capability for capability in required if not self.robot.supports(capability)]
            if missing:
                raise RuntimeError("机器人未连接或缺少所需能力：" + ", ".join(missing))
            await self.play()
        finally:
            try:
                await self.generator.close()
            finally:
                try:
                    await self.judge.close()
                finally:
                    await self.conversation.close()


async def run_application(app: ApplicationContext, settings: Settings) -> None:
    async with asyncio.TaskGroup() as group:
        game_task = group.create_task(RobotGame(app, settings).run())
        while not game_task.done():
            if app.shutdown_requested:
                game_task.cancel()
                break
            await asyncio.sleep(0.2)
