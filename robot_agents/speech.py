import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from watcherobot.audio import MAX_AUDIO_BYTES, PCMAudio
from websockets.asyncio.client import ClientConnection, connect

from robot_agents.audio_stream import CapturedPcm
from robot_agents.config import Settings


@dataclass(frozen=True)
class RecognizedSpeech:
    text: str
    started_at: float
    ended_at: float | None = None
    # 识别结果返回时，已经送入识别服务的最后一段音频的采集时间。
    sent_until: float | None = None


class SpeechTimeline:
    def __init__(self):
        self.frames = deque(maxlen=30000)
        self.sent_samples = 0

    def append(self, frame: CapturedPcm):
        if not frame.data or len(frame.data) % 2:
            raise ValueError("ASR 输入必须包含完整的 16-bit PCM 采样")
        self.frames.append((self.sent_samples / 16, frame.captured_at, frame.muted))
        self.sent_samples += len(frame.data) // 2

    def captured_at(self, position: float) -> float:
        offset, captured_at, _ = next(
            item for item in reversed(self.frames) if item[0] <= position
        )
        return captured_at + (position - offset) / 1000

    def started_at(self, begin: float, end: float) -> float | None:
        if not self.frames or begin < self.frames[0][0] or end < begin:
            raise ValueError("ASR 句子时间超出保留的音频记录")
        offset, captured_at, muted = next(
            item for item in reversed(self.frames) if item[0] <= begin
        )
        if muted or any(item[2] for item in self.frames if begin <= item[0] < end):
            return None
        return captured_at + (begin - offset) / 1000


@dataclass
class AudioTask:
    socket: ClientConnection
    task_id: str

    async def send(self, action: str, payload: dict) -> None:
        await self.socket.send(json.dumps({
            "header": {"action": action, "task_id": self.task_id, "streaming": "duplex"},
            "payload": payload,
        }, ensure_ascii=False))

    async def receive(self) -> bytes | dict:
        message = await self.socket.recv()
        if isinstance(message, bytes):
            return message
        event = json.loads(message)
        header = event["header"]
        if header["task_id"] != self.task_id:
            raise RuntimeError("语音服务返回了其他任务的数据")
        if header["event"] == "task-failed":
            raise RuntimeError(f"语音服务失败：{header['error_code']}，{header['error_message']}")
        return event


class QwenSpeech:
    def __init__(self, settings: Settings):
        self.settings = settings

    @asynccontextmanager
    async def task(self, kind: str, model: str, parameters: dict) -> AsyncIterator[AudioTask]:
        async with connect(
            self.settings.websocket_url,
            additional_headers={"Authorization": f"Bearer {self.settings.dashscope_api_key}"},
            open_timeout=10, close_timeout=0.5, max_size=MAX_AUDIO_BYTES,
        ) as socket:
            task = AudioTask(socket, str(uuid4()))
            await task.send("run-task", {
                "task_group": "audio", "task": kind,
                "function": "SpeechSynthesizer" if kind == "tts" else "recognition",
                "model": model, "parameters": parameters, "input": {},
            })
            async with asyncio.timeout(15):
                event = await task.receive()
            if isinstance(event, bytes) or event["header"]["event"] != "task-started":
                raise RuntimeError("语音服务没有确认任务启动")
            yield task

    async def synthesize(self, text: str) -> bytes:
        audio = bytearray()
        async with aclosing(self.synthesize_stream(text)) as chunks:
            async for chunk in chunks:
                audio.extend(chunk)
        return bytes(audio)

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip() or len(text) > 500:
            raise ValueError("每次语音反馈必须包含 1 至 500 个字符")
        parameters = {
            "text_type": "PlainText", "voice": self.settings.tts_voice,
            "format": "pcm", "sample_rate": 24000,
            "language_hints": ["zh"], "volume": 10, "rate": 1.0,
        }
        async with asyncio.timeout(self.settings.speech_timeout_seconds):
            async with self.task("tts", self.settings.tts_model, parameters) as task:
                await task.send("continue-task", {"input": {"text": text}})
                await task.send("finish-task", {"input": {}})
                total_bytes = 0
                pending = bytearray()
                while True:
                    event = await task.receive()
                    if isinstance(event, bytes):
                        total_bytes += len(event)
                        if total_bytes > MAX_AUDIO_BYTES:
                            raise ValueError("合成音频超过机器人单次播放容量")
                        pending.extend(event)
                        complete_bytes = len(pending) // 2 * 2
                        if complete_bytes:
                            chunk = bytes(pending[:complete_bytes])
                            del pending[:complete_bytes]
                            yield PCMAudio(chunk).data
                    elif event["header"]["event"] == "task-finished":
                        if not total_bytes or pending:
                            raise ValueError("合成音频为空或包含不完整的 PCM 采样")
                        return

    def asr_parameters(self, sample_rate=16000, hotwords=()):
        return {
            "format": "pcm", "sample_rate": sample_rate, "language_hints": ["zh"],
            "vad_model": "far_field_meeting_16k", "semantic_punctuation_enabled": False,
            "max_sentence_silence": self.settings.asr_silence_ms, "heartbeat": True,
            "vocabulary": dict.fromkeys(hotwords, 2),
        }

    async def recognize(
        self, audio: AsyncIterator[bytes], *, sample_rate=16000, hotwords=(), timeout=75,
    ) -> str | None:
        async with asyncio.timeout(timeout), self.task(
            "asr", self.settings.asr_model, self.asr_parameters(sample_rate, hotwords),
        ) as task:
            complete = asyncio.Event()
            text = None

            async def send_audio():
                async for frame in audio:
                    if complete.is_set():
                        break
                    if not frame or len(frame) % 2:
                        raise ValueError("ASR 输入必须是完整的 16-bit PCM 采样")
                    await task.socket.send(frame)
                await task.send("finish-task", {"input": {}})

            async def receive_text():
                nonlocal text
                while True:
                    event = await task.receive()
                    if isinstance(event, bytes):
                        raise TypeError("ASR 返回了非预期的音频数据")
                    kind = event["header"]["event"]
                    if kind == "task-finished":
                        complete.set()
                        return
                    if kind == "result-generated":
                        sentence = event["payload"]["output"]["sentence"]
                        if (not sentence.get("heartbeat") and sentence.get("sentence_end")
                                and sentence["text"].strip() and text is None):
                            text = sentence["text"].strip()
                            complete.set()

            async with asyncio.TaskGroup() as group:
                group.create_task(send_audio())
                group.create_task(receive_text())
            return text

    async def transcribe(
        self, audio: AsyncIterator[CapturedPcm], on_result: Callable[[RecognizedSpeech], None],
        *, on_activity: Callable[[bool], None], ready: asyncio.Event, hotwords=(),
    ) -> None:
        timeline = SpeechTimeline()
        input_finished = False
        async with (
            self.task("asr", self.settings.asr_model, self.asr_parameters(hotwords=hotwords)) as task,
            asyncio.timeout(None) as completion,
        ):
            ready.set()

            async def send_audio():
                nonlocal input_finished
                async for frame in audio:
                    timeline.append(frame)
                    async with asyncio.timeout(10):
                        await task.socket.send(frame.data)
                input_finished = True
                completion.reschedule(asyncio.get_running_loop().time() + 30)
                await task.send("finish-task", {"input": {}})

            async def receive_text():
                last_sentence = -1
                while True:
                    event = await task.receive()
                    if isinstance(event, bytes):
                        raise TypeError("ASR 返回了非预期的音频数据")
                    kind = event["header"]["event"]
                    if kind == "task-finished":
                        if not input_finished:
                            raise RuntimeError("持续 ASR 在音频输入结束前终止")
                        return
                    if kind != "result-generated":
                        continue
                    sentence = event["payload"]["output"]["sentence"]
                    if sentence.get("heartbeat"):
                        continue
                    on_activity(not sentence.get("sentence_end", False))
                    if not sentence.get("sentence_end") or sentence["sentence_id"] <= last_sentence:
                        continue
                    last_sentence = sentence["sentence_id"]
                    text = sentence["text"].strip()
                    if not text:
                        continue
                    started_at = timeline.started_at(sentence["begin_time"], sentence["end_time"])
                    if started_at is not None:
                        on_result(RecognizedSpeech(
                            text, started_at, timeline.captured_at(sentence["end_time"]),
                            timeline.captured_at(timeline.sent_samples / 16),
                        ))

            async with asyncio.TaskGroup() as group:
                group.create_task(send_audio())
                group.create_task(receive_text())
