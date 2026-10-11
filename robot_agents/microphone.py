import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import suppress

from watcherobot.errors import WatcheRobotError
from watcherobot.media import AudioFormat

from observability import describe
from robot_agents.audio_stream import CapturedPcm, sdk_command

logger = logging.getLogger(__name__)

READ_INTERVAL_SECONDS = 0.1
PCM_TIMEOUT_SECONDS = 10.0
REOPEN_DELAY_SECONDS = 1.0
MAX_REOPEN_DELAY_SECONDS = 5.0
KEEPALIVE_AFTER_SECONDS = 1.0
# 设备拒绝或没有应答指令、会话被设备关闭，这些情况重新打开麦克风就能恢复。
RECOVERABLE_ERRORS = (WatcheRobotError, TimeoutError, RuntimeError, OSError)


def _read_frame(session):
    # 短暂等待结束后交回采集流程，检查暂停请求和累计无数据时间。
    try:
        return session.read(timeout=READ_INTERVAL_SECONDS)
    except TimeoutError:
        return None


class MicrophoneInput:
    def __init__(self, robot):
        self.robot = robot
        self.enabled = asyncio.Event()
        self.enabled.set()
        self.released = asyncio.Event()
        self.released.set()
        self.opened = asyncio.Event()
        self.receiving = asyncio.Event()
        self.running = False
        self.frames: asyncio.Queue[CapturedPcm] = asyncio.Queue(128)
        self.received_pcm_bytes = 0
        self.pause_owners: set[object] = set()
        self.sessions = deque(maxlen=32)
        self.failures = 0
        self.stale = None

    async def pause(self, *, owner: object) -> None:
        self.pause_owners.add(owner)
        self.enabled.clear()
        await self.released.wait()
        while not self.frames.empty():
            self.frames.get_nowait()

    def resume(self, *, owner: object) -> None:
        self.pause_owners.remove(owner)
        if not self.pause_owners:
            self.enabled.set()

    async def wait_ready(self) -> None:
        if self.running:
            await self.receiving.wait()

    async def capture(self) -> None:
        self.running = True
        try:
            await self._capture()
        finally:
            self.running = False

    async def _capture(self) -> None:
        while True:
            await self.enabled.wait()
            if not self.enabled.is_set():
                continue
            self.released.clear()
            session = None
            try:
                try:
                    session = await self._open()
                except RECOVERABLE_ERRORS as error:
                    problem = f"打开麦克风失败：{describe(error)}"
                else:
                    if session.format != AudioFormat():
                        raise ValueError("麦克风需要提供 16 kHz、单声道、16-bit PCM")
                    problem = await self._stream(session)
            finally:
                self.receiving.clear()
                self.opened.clear()
                try:
                    if session is not None:
                        await self._close(session)
                finally:
                    self.released.set()
            if problem is None:
                continue
            # 设备指令超时、网络卡顿和会话被关闭都可以恢复，等一会儿重新打开麦克风继续听。
            self.failures += 1
            delay = min(REOPEN_DELAY_SECONDS * self.failures, MAX_REOPEN_DELAY_SECONDS)
            logger.warning("麦克风收音中断，%.0f 秒后重新打开（连续第 %s 次）：%s",
                           delay, self.failures, problem)
            await asyncio.sleep(delay)

    async def _open(self):
        stale, self.stale = self.stale, None
        if stale is not None:
            await self._close(stale)
        opening = asyncio.create_task(asyncio.to_thread(
            self.robot.microphone.open_pcm, queue_size=128
        ))
        try:
            return await asyncio.shield(opening)
        except asyncio.CancelledError:
            # 退出前等打开指令结束，并关闭已经建立的会话。
            with suppress(Exception):
                await self._close(await opening)
            raise

    async def _close(self, session) -> None:
        try:
            await sdk_command(session.close)
        except RECOVERABLE_ERRORS as error:
            # 关闭指令没有送达时设备仍然占用麦克风，下次打开之前再关闭一次。
            self.stale = session
            logger.warning("关闭麦克风会话 %s 失败：%s", session.id, describe(error))

    async def _stream(self, session) -> str | None:
        """持续读取一个会话的音频。暂停收音时返回 None，收音中断时返回原因。"""
        self.opened.set()
        metrics = {"id": session.id, "opened_at": time.monotonic(), "pcm_bytes": 0,
                   "last_frame_at": None, "last_sequence": None}
        self.sessions.append(metrics)
        lost = 0
        while self.enabled.is_set():
            try:
                frame = await sdk_command(_read_frame, session)
            except RECOVERABLE_ERRORS as error:
                return f"会话 {session.id} 无法读取：{describe(error)}"
            if not self.enabled.is_set():
                metrics["paused_at"] = time.monotonic()
                break
            if frame is None:
                now = time.monotonic()
                last_received = metrics["last_frame_at"]
                if last_received is None:
                    last_received = metrics["opened_at"]
                idle_seconds = now - last_received
                if idle_seconds < PCM_TIMEOUT_SECONDS:
                    continue
                metrics["timeout_at"] = now
                metrics["idle_seconds"] = idle_seconds
                return (
                    f"连续 {idle_seconds:.2f} 秒没有 PCM：session={session.id}, "
                    f"session_closed={session.closed}, "
                    f"pcm_bytes={metrics['pcm_bytes']}, "
                    f"last_sequence={metrics['last_sequence']}, "
                    f"dropped_frames={session.dropped_frames}, "
                    f"decode_failures={session.decode_failures}"
                )
            if session.dropped_frames + session.decode_failures != lost:
                lost = session.dropped_frames + session.decode_failures
                logger.warning("麦克风音频丢失或解码失败：session=%s, dropped_frames=%s, "
                               "decode_failures=%s", session.id, session.dropped_frames,
                               session.decode_failures)
            self.received_pcm_bytes += len(frame.data)
            metrics.update(
                pcm_bytes=metrics["pcm_bytes"] + len(frame.data),
                last_frame_at=time.monotonic(), last_sequence=frame.sequence,
            )
            if self.failures:
                logger.info("麦克风恢复收音：session=%s", session.id)
                self.failures = 0
            if self.enabled.is_set():
                if self.frames.full():
                    # 识别服务重连期间没有人取走音频，保留最新的部分。
                    self.frames.get_nowait()
                self.frames.put_nowait(CapturedPcm(
                    frame.data, time.monotonic() - len(frame.data) / 32000
                ))
                self.receiving.set()
        return None

    async def audio(self) -> AsyncIterator[CapturedPcm]:
        idle_since = None
        while True:
            if not self.enabled.is_set() or self.released.is_set():
                idle_since = None
                await asyncio.sleep(0.02)
                # 关闭麦克风和切换音频会话期间，以静音维持 ASR 会话。
                yield CapturedPcm(bytes(640), time.monotonic() - 0.02, True)
                continue
            try:
                async with asyncio.timeout(READ_INTERVAL_SECONDS):
                    frame = await self.frames.get()
            except TimeoutError:
                now = time.monotonic()
                if idle_since is None:
                    idle_since = now
                # 设备音频迟迟不到时同样发送静音，识别服务不会因为收不到数据而断开。
                if now - idle_since >= KEEPALIVE_AFTER_SECONDS:
                    yield CapturedPcm(bytes(3200), now - READ_INTERVAL_SECONDS, True)
                continue
            idle_since = None
            yield frame
