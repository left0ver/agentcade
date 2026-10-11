import asyncio
import time
from collections import deque
from contextlib import aclosing

import observability
from robot_agents.audio_stream import CapturedPcm, PcmInputGain
from robot_agents.speech import QwenSpeech

MAX_RECONNECT_DELAY_SECONDS = 10
STABLE_SESSION_SECONDS = 60


class SpeechInput:
    def __init__(self, settings, agent, microphone, logger):
        self.speech = QwenSpeech(settings)
        self.agent, self.microphone, self.logger = agent, microphone, logger
        self.busy = False
        self.input_speaking = False
        self.input_gain = PcmInputGain(settings.input_gain)
        self.events = deque(maxlen=512)

    def activity(self, speaking):
        self.input_speaking = speaking and not self.busy

    async def run(self, ready):
        messages = asyncio.Queue(32)

        def receive(message):
            if self.busy:
                return
            self.busy = True
            self.input_speaking = False
            self.events.append({
                "type": "recognized", "time": time.monotonic(),
                "started_at": message.started_at, "text": message.text,
            })
            self.logger.info("ASR 用户原话：%s", message.text)
            turn_id = observability.user_input(
                message.text, message.started_at, message.ended_at, message.sent_until,
            )
            messages.put_nowait((message, turn_id))

        async def frames():
            async with aclosing(self.microphone.audio()) as audio:
                async for frame in audio:
                    muted = frame.muted or self.busy
                    pcm = bytes(len(frame.data)) if muted else self.input_gain.process(frame.data)
                    yield CapturedPcm(pcm, frame.captured_at, muted)

        async def process():
            while True:
                message, turn_id = await messages.get()
                try:
                    await self.microphone.pause(owner=self)
                    with observability.turn(turn_id):
                        completion = await self.agent.receive(message.text)
                    if completion is not None:
                        await asyncio.shield(completion)
                finally:
                    self.microphone.resume(owner=self)
                    self.busy = False
                    messages.task_done()

        async def recognize():
            failures = 0
            while True:
                started = time.monotonic()
                try:
                    # 每次连接使用新的音频流，上一次连接中断时旧的流已经关闭。
                    async with aclosing(frames()) as source:
                        await self.speech.transcribe(
                            source, receive, on_activity=self.activity, ready=ready,
                            hotwords=tuple(game.descriptor.name
                                           for game in self.agent.games.values()),
                        )
                    return
                except Exception as error:
                    # 启动时连不上识别服务属于配置或网络问题，直接报错；运行中断开则重新连接。
                    if not ready.is_set():
                        raise
                    stable = time.monotonic() - started >= STABLE_SESSION_SECONDS
                    failures = 1 if stable else failures + 1
                    delay = min(2 ** (failures - 1), MAX_RECONNECT_DELAY_SECONDS)
                    self.input_speaking = False
                    self.logger.warning("语音识别中断，%s 秒后重新连接（连续第 %s 次）：%s",
                                        delay, failures, observability.describe(error))
                    await asyncio.sleep(delay)

        async with asyncio.TaskGroup() as group:
            consumer = group.create_task(process())
            await recognize()
            await messages.join()
            consumer.cancel()
