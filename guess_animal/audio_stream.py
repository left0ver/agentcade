import asyncio
import time
from array import array
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from fractions import Fraction

from aiortc import (
    AudioStreamTrack,
    RTCConfiguration,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp
from av import AudioFrame, AudioResampler, CodecContext, Packet
from watcherobot.application.rtc import ApplicationRtc
from watcherobot.audio import MAX_AUDIO_BYTES

PlaybackCallback = Callable[[], Awaitable[None]]
SAMPLE_RATE = 24000
FRAME_SAMPLES = 480
FRAME_BYTES = FRAME_SAMPLES * 2
TAIL_FRAMES = 15


@dataclass(frozen=True)
class CapturedPcm:
    data: bytes
    captured_at: float
    muted: bool = False


@dataclass
class PlaybackMetrics:
    pcm_bytes: int = 0
    source_pcm_peak: int = 0
    first_pcm_seconds: float | None = None
    first_frame_seconds: float | None = None
    first_device_audio_seconds: float | None = None
    input_finished_seconds: float | None = None
    completed_seconds: float | None = None
    packets_sent: int = 0
    device_decoded_frames: int = 0
    device_i2s_bytes: int = 0
    device_pcm_peak: int = 0


class PlaybackBuffer:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=25)
        self.ready = asyncio.Event()
        self.enabled = asyncio.Event()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.final_frame = 0
        self.epoch = time.monotonic()
        self.metrics = PlaybackMetrics()

    async def feed(self, chunks: AsyncIterator[bytes]) -> None:
        pending = bytearray()
        async for chunk in chunks:
            if not chunk or len(chunk) % 2:
                raise ValueError("播放输入必须是完整的 24 kHz、单声道、16-bit PCM 采样")
            self.metrics.pcm_bytes += len(chunk)
            self.metrics.source_pcm_peak = max(
                self.metrics.source_pcm_peak,
                max(abs(sample) for sample in array("h", chunk)),
            )
            if self.metrics.pcm_bytes > MAX_AUDIO_BYTES:
                raise ValueError("单次语音超过播放容量")
            if self.metrics.first_pcm_seconds is None:
                self.metrics.first_pcm_seconds = time.monotonic() - self.epoch
            pending.extend(chunk)
            while len(pending) >= FRAME_BYTES:
                await self.queue.put(bytes(pending[:FRAME_BYTES]))
                del pending[:FRAME_BYTES]
                self.ready.set()
        self.metrics.input_finished_seconds = time.monotonic() - self.epoch
        if not self.metrics.pcm_bytes:
            raise ValueError("播放音频为空")
        if pending:
            await self.queue.put(bytes(pending).ljust(FRAME_BYTES, b"\0"))
            self.ready.set()
        for _ in range(TAIL_FRAMES):
            await self.queue.put(bytes(FRAME_BYTES))
        await self.queue.put(None)


class OutputAudioTrack(AudioStreamTrack):
    def __init__(self) -> None:
        super().__init__()
        self.playback: PlaybackBuffer | None = None
        self.samples = 0
        self.next_frame_at = 0.0

    async def recv(self) -> AudioFrame:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0, self.next_frame_at - loop.time()))
        self.next_frame_at = loop.time() + 0.02
        data = bytes(FRAME_BYTES)
        playback = self.playback
        if playback and playback.enabled.is_set() and not playback.queue.empty():
            chunk = playback.queue.get_nowait()
            if chunk is None:
                playback.final_frame = self.samples // FRAME_SAMPLES
                playback.finished.set()
            else:
                data = chunk
                if not playback.started.is_set():
                    playback.metrics.first_frame_seconds = time.monotonic() - playback.epoch
                    playback.started.set()
        frame = AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.planes[0].update(data)
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = Fraction(1, SAMPLE_RATE)
        frame.pts = self.samples
        self.samples += FRAME_SAMPLES
        return frame


class OpusOutputTrack(OutputAudioTrack):
    def __init__(self) -> None:
        super().__init__()
        self.codec = CodecContext.create("libopus", "w")
        self.codec.bit_rate = 32000
        self.codec.format = "s16"
        self.codec.layout = "mono"
        self.codec.sample_rate = 16000
        self.codec.time_base = Fraction(1, 16000)
        self.codec.options = {"application": "voip", "frame_duration": "20"}
        self.resampler = AudioResampler(
            format="s16", layout="mono", rate=16000, frame_size=320
        )
        self.first_packet_pts: int | None = None

    async def recv(self) -> Packet:
        while True:
            frame = await super().recv()
            for converted in self.resampler.resample(frame):
                packets = self.codec.encode(converted)
                if not packets:
                    continue
                if len(packets) != 1:
                    raise RuntimeError("Opus 编码必须每次输出一个 20 毫秒音频包")
                packet = packets[0]
                if self.first_packet_pts is None:
                    self.first_packet_pts = packet.pts
                packet.pts -= self.first_packet_pts
                return packet


async def rtc_command(function, *args, **kwargs):
    # SDK 指令在工作线程等待确认，取消时也要等待该指令结束再释放会话。
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class RtcAudioPlayer:
    def __init__(self, rtc: ApplicationRtc):
        self.rtc = rtc
        self._peer: RTCPeerConnection | None = None
        self._output: OutputAudioTrack | None = None
        self._playing = False
        self.decoded_frames = 0
        self.i2s_bytes = 0

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        if self._peer is not None or self.rtc.snapshot()["active"]:
            raise RuntimeError("RTC 音频会话已经被占用")
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        output = OpusOutputTrack()
        self._peer, self._output = peer, output
        self.decoded_frames = self.i2s_bytes = 0
        transceiver = peer.addTransceiver(output, direction="sendonly")
        transceiver.setCodecPreferences([
            codec for codec in RTCRtpSender.getCapabilities("audio").codecs
            if codec.mimeType.lower() == "audio/opus"
        ])

        try:
            async with asyncio.TaskGroup() as group:
                await rtc_command(self.rtc.start, mode="audio")
                monitor = group.create_task(self._monitor(peer, output))
                async with asyncio.timeout(10):
                    await peer.setLocalDescription(await peer.createOffer())
                    await rtc_command(self.rtc.send_offer, peer.localDescription.sdp)
                    while peer.connectionState != "connected":
                        await asyncio.sleep(0.02)
                try:
                    yield
                finally:
                    monitor.cancel()
        finally:
            output.stop()
            try:
                await peer.close()
            finally:
                try:
                    await rtc_command(self.rtc.stop)
                finally:
                    self._peer = self._output = None

    async def play(
        self, chunks: AsyncIterator[bytes], *, on_start: PlaybackCallback | None = None
    ) -> PlaybackMetrics:
        if self._playing:
            raise RuntimeError("请按顺序提交语音播报")
        self._playing = True
        try:
            async with aclosing(chunks):
                if self._peer is None:
                    async with self.session():
                        return await self._play(chunks, on_start)
                return await self._play(chunks, on_start)
        finally:
            self._playing = False

    async def _play(
        self, chunks: AsyncIterator[bytes], on_start: PlaybackCallback | None
    ) -> PlaybackMetrics:
        playback = PlaybackBuffer()
        output = self._output
        if output is None:
            raise RuntimeError("RTC 会话没有启动")
        baseline_frames, baseline_bytes = self.decoded_frames, self.i2s_bytes
        baseline_packets = await self._sent_packets()
        output.playback = playback
        try:
            async with asyncio.timeout(120), asyncio.TaskGroup() as group:
                group.create_task(playback.feed(chunks))
                await playback.ready.wait()
                if on_start is not None:
                    await on_start()
                playback.enabled.set()
                await playback.finished.wait()
                async with asyncio.timeout(5):
                    # 末尾静音维持连续传输，并等待设备完成当前语音对应的帧。
                    while self.decoded_frames < playback.final_frame - TAIL_FRAMES:
                        await asyncio.sleep(0.02)
                playback.metrics.completed_seconds = time.monotonic() - playback.epoch
                playback.metrics.packets_sent = await self._sent_packets() - baseline_packets
                playback.metrics.device_decoded_frames = self.decoded_frames - baseline_frames
                playback.metrics.device_i2s_bytes = self.i2s_bytes - baseline_bytes
        finally:
            output.playback = None
        return playback.metrics

    async def _sent_packets(self) -> int:
        if self._peer is None:
            raise RuntimeError("RTC 会话没有启动")
        reports = await self._peer.getStats()
        return sum(
            report.packetsSent for report in reports.values()
            if report.type == "outbound-rtp" and report.kind == "audio"
        )

    async def _monitor(self, peer: RTCPeerConnection, output: OutputAudioTrack) -> None:
        cursor = 0
        candidates = []
        heartbeat_at = 0.0
        while True:
            if peer.connectionState in {"failed", "closed"} or output.readyState == "ended":
                raise RuntimeError(f"RTC 音频连接终止：{peer.connectionState}")
            for event in self.rtc.events(after=cursor):
                cursor = event["id"]
                message = event["message"]
                data = message.get("data", {})
                if message["type"] == "sys.nack":
                    raise RuntimeError(f"设备拒绝 RTC 指令：{data}")
                if message["type"] == "evt.rtc.state" and data["state"] in {"failed", "stopped"}:
                    raise RuntimeError(f"设备 RTC 会话终止：{data}")
                if message["type"] == "evt.rtc.signal":
                    if data["kind"] == "answer":
                        await peer.setRemoteDescription(RTCSessionDescription(data["sdp"], "answer"))
                    elif data["kind"] == "candidate":
                        candidate = candidate_from_sdp(data["candidate"].removeprefix("candidate:"))
                        candidate.sdpMid = data["sdp_mid"]
                        candidate.sdpMLineIndex = data["sdp_mline_index"]
                        candidates.append(candidate)
                    if peer.remoteDescription:
                        for candidate in candidates:
                            await peer.addIceCandidate(candidate)
                        candidates.clear()
                if message["type"] == "evt.rtc.stats":
                    reports = await peer.getStats()
                    sent = sum(
                        report.packetsSent for report in reports.values()
                        if report.type == "outbound-rtp" and report.kind == "audio"
                    )
                    if not sent or data.get("audio_packets", 0) > sent:
                        continue
                    if data.get("audio_render_errors") or data.get("audio_queue_dropped"):
                        raise RuntimeError(f"设备音频播放出现错误：{data}")
                    self.decoded_frames = data.get("audio_decoded_frames", 0)
                    self.i2s_bytes = data.get("audio_i2s_bytes", 0)
                    playback = output.playback
                    if playback and playback.started.is_set():
                        metrics = playback.metrics
                        metrics.device_pcm_peak = max(
                            metrics.device_pcm_peak, data.get("audio_pcm_peak", 0)
                        )
                        if metrics.first_device_audio_seconds is None and metrics.device_pcm_peak:
                            metrics.first_device_audio_seconds = time.monotonic() - playback.epoch
            if time.monotonic() >= heartbeat_at:
                await rtc_command(self.rtc.clock_ping, time.time_ns() // 1000)
                heartbeat_at = time.monotonic() + 1.5
            await asyncio.sleep(0.02)
