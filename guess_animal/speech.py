import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from watcherobot.audio import MAX_AUDIO_BYTES, PCMAudio
from websockets.asyncio.client import ClientConnection, connect

from guess_animal.config import Settings


@dataclass
class AudioTask:
    socket: ClientConnection
    task_id: str

    async def send(self, action: str, payload: dict) -> None:
        await self.socket.send(
            json.dumps(
                {
                    "header": {"action": action, "task_id": self.task_id, "streaming": "duplex"},
                    "payload": payload,
                },
                ensure_ascii=False,
            )
        )

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
            open_timeout=10,
            close_timeout=3,
            max_size=MAX_AUDIO_BYTES,
        ) as socket:
            task = AudioTask(socket, str(uuid4()))
            await task.send(
                "run-task",
                {
                    "task_group": "audio",
                    "task": kind,
                    "function": "SpeechSynthesizer" if kind == "tts" else "recognition",
                    "model": model,
                    "parameters": parameters,
                    "input": {},
                },
            )
            async with asyncio.timeout(15):
                event = await task.receive()
            if isinstance(event, bytes) or event["header"]["event"] != "task-started":
                raise RuntimeError("语音服务没有确认任务启动")
            yield task

    async def synthesize(self, text: str) -> bytes:
        if not text.strip() or len(text) > 500:
            raise ValueError("每次语音反馈必须包含 1 至 500 个字符")
        parameters = {
            "text_type": "PlainText",
            "voice": self.settings.tts_voice,
            "format": "pcm",
            "sample_rate": 24000,
            "language_hints": ["zh"],
            "volume": 10,
            "rate": 1.0,
        }
        async with asyncio.timeout(60):
            async with self.task("tts", self.settings.tts_model, parameters) as task:
                await task.send("continue-task", {"input": {"text": text}})
                await task.send("finish-task", {"input": {}})
                audio = bytearray()
                while True:
                    event = await task.receive()
                    if isinstance(event, bytes):
                        audio.extend(event)
                        if len(audio) > MAX_AUDIO_BYTES:
                            raise ValueError("合成音频超过机器人单次播放容量")
                    elif event["header"]["event"] == "task-finished":
                        return PCMAudio(bytes(audio)).data

    async def recognize(
        self,
        audio: AsyncIterator[bytes],
        *,
        sample_rate: int = 16000,
        hotwords: tuple[str, ...] = (),
        timeout: float = 75,
    ) -> str | None:
        parameters = {
            "format": "pcm",
            "sample_rate": sample_rate,
            "language_hints": ["zh"],
            "vad_model": "far_field_meeting_16k",
            "semantic_punctuation_enabled": False,
            "max_sentence_silence": 800,
            "heartbeat": True,
            "vocabulary": dict.fromkeys(hotwords, 2),
        }
        async with asyncio.timeout(timeout):
            async with self.task("asr", self.settings.asr_model, parameters) as task:
                complete = asyncio.Event()
                text: str | None = None

                async def send_audio() -> None:
                    async for frame in audio:
                        if complete.is_set():
                            break
                        if not frame or len(frame) % 2:
                            raise ValueError("ASR 输入必须是完整的 16-bit PCM 采样")
                        await task.socket.send(frame)
                    await task.send("finish-task", {"input": {}})

                async def receive_text() -> None:
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
                            if sentence.get("heartbeat"):
                                continue
                            if (
                                sentence.get("sentence_end")
                                and sentence["text"].strip()
                                and text is None
                            ):
                                text = sentence["text"].strip()
                                complete.set()

                async with asyncio.TaskGroup() as group:
                    group.create_task(send_audio())
                    group.create_task(receive_text())
                return text
