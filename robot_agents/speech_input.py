import asyncio
import time
from collections import deque
from contextlib import aclosing

import observability
from robot_agents.audio_stream import CapturedPcm, PcmInputGain
from robot_agents.speech import QwenSpeech


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

    async def run(self, audio, ready):
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

        async with aclosing(frames()) as source, asyncio.TaskGroup() as group:
            consumer = group.create_task(process())
            await self.speech.transcribe(
                source, receive, on_activity=self.activity, ready=ready,
                hotwords=tuple(game.descriptor.name for game in self.agent.games.values()),
            )
            await messages.join()
            consumer.cancel()
