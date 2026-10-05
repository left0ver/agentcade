import asyncio
import time
from collections.abc import AsyncIterator

from watcherobot.media import AudioFormat

from guess_animal.audio_stream import CapturedPcm, rtc_command


class MicrophoneInput:
    def __init__(self, robot):
        self.robot = robot
        self.enabled = asyncio.Event()
        self.enabled.set()
        self.released = asyncio.Event()
        self.released.set()
        self.opened = asyncio.Event()
        self.running = False
        self.frames: asyncio.Queue[CapturedPcm] = asyncio.Queue(128)
        self.received_pcm_bytes = 0

    async def pause(self) -> None:
        self.enabled.clear()
        await self.released.wait()
        while not self.frames.empty():
            self.frames.get_nowait()

    def resume(self) -> None:
        self.enabled.set()

    async def wait_ready(self) -> None:
        if self.running:
            await self.opened.wait()

    async def capture(self) -> None:
        self.running = True
        try:
            await self._capture()
        finally:
            self.running = False

    async def _capture(self) -> None:
        while True:
            await self.enabled.wait()
            self.released.clear()
            session = None
            try:
                opening = asyncio.create_task(asyncio.to_thread(
                    self.robot.microphone.open_pcm, queue_size=128
                ))
                try:
                    session = await asyncio.shield(opening)
                except asyncio.CancelledError:
                    session = await opening
                    raise
                if session.format != AudioFormat():
                    raise ValueError("麦克风需要提供 16 kHz、单声道、16-bit PCM")
                self.opened.set()
                while self.enabled.is_set():
                    frame = await rtc_command(session.read, timeout=2)
                    if session.dropped_frames or session.decode_failures:
                        raise RuntimeError("麦克风音频丢失或解码失败")
                    self.received_pcm_bytes += len(frame.data)
                    if self.enabled.is_set():
                        self.frames.put_nowait(CapturedPcm(
                            frame.data, time.monotonic() - len(frame.data) / 32000
                        ))
            finally:
                self.opened.clear()
                try:
                    if session is not None:
                        await rtc_command(session.close)
                finally:
                    self.released.set()

    async def audio(self) -> AsyncIterator[CapturedPcm]:
        while True:
            if not self.enabled.is_set() or self.released.is_set():
                await asyncio.sleep(0.02)
                # 关闭麦克风和切换音频会话期间，以静音维持 ASR 连接。
                yield CapturedPcm(bytes(640), time.monotonic() - 0.02, True)
                continue
            try:
                async with asyncio.timeout(0.1):
                    frame = await self.frames.get()
            except TimeoutError:
                continue
            yield frame
