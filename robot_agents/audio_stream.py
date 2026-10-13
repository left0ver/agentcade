import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from fractions import Fraction

from av import AudioFrame
from av.filter import Graph
from watcherobot.audio import PCMAudio
from watcherobot.robot import AudioDomain

PlaybackCallback = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class CapturedPcm:
    data: bytes
    captured_at: float
    muted: bool = False


class PcmInputGain:
    def __init__(self, gain):
        if not 1 <= gain <= 8:
            raise ValueError("麦克风输入增益必须在 1 至 8 之间")
        self.graph = Graph()
        source = self.graph.add_abuffer(sample_rate=16000, format="s16", layout="mono")
        volume = self.graph.add("volume", f"volume={gain}:precision=fixed")
        sink = self.graph.add("abuffersink")
        source.link_to(volume)
        volume.link_to(sink)
        self.graph.configure()
        self.samples = 0

    def process(self, pcm):
        if not pcm or len(pcm) % 2:
            raise ValueError("输入增益处理需要完整的 PCM 采样")
        frame = AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
        frame.sample_rate = 16000
        frame.time_base = Fraction(1, 16000)
        frame.pts = self.samples
        frame.planes[0].update(pcm)
        self.samples += frame.samples
        self.graph.push(frame)
        output = self.graph.pull()
        if output.samples != frame.samples or output.format.name != "s16":
            raise ValueError("输入增益处理改变了 PCM 格式或采样数量")
        return bytes(output.planes[0])[:output.samples * 2]


@dataclass(frozen=True)
class PlaybackMetrics:
    pcm_bytes: int
    playback_id: int
    started_seconds: float
    completed_seconds: float


async def sdk_command(function, *args, **kwargs):
    # SDK 指令在工作线程等待确认，取消时也要等待该指令结束。
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class PcmAudioPlayer:
    def __init__(self, audio: AudioDomain):
        self.audio = audio
        self.playing = False

    async def play(
        self, pcm: bytes, *, on_start: PlaybackCallback | None = None,
    ) -> PlaybackMetrics:
        if self.playing:
            raise RuntimeError("请按顺序提交语音播报")
        source = PCMAudio(pcm)
        self.playing = True
        playback = None
        epoch = time.monotonic()
        try:
            opening = asyncio.create_task(asyncio.to_thread(self.audio.play_pcm, source.data))
            try:
                playback = await asyncio.shield(opening)
            except asyncio.CancelledError:
                # 取得已启动的播放任务，确保取消期间仍能停止设备播放。
                playback = await opening
                raise
            started = time.monotonic() - epoch
            async with asyncio.timeout(playback.expected_duration_seconds + 15):
                if on_start is not None:
                    await on_start(playback.expected_duration_seconds)
                while not playback.state.terminal:
                    await asyncio.sleep(0.02)
                playback.wait(0)
            return PlaybackMetrics(
                pcm_bytes=len(source.data), playback_id=playback.id,
                started_seconds=started, completed_seconds=time.monotonic() - epoch,
            )
        finally:
            try:
                if playback is not None and not playback.state.terminal:
                    await sdk_command(playback.cancel)
            finally:
                self.playing = False
