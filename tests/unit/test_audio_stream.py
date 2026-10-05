import asyncio
import time
import wave
from pathlib import Path

import pytest
from av import CodecContext

from guess_animal.audio_stream import OpusOutputTrack, OutputAudioTrack, PlaybackBuffer


def test_audio_frames_keep_their_interval_after_processing_delay():
    async def receive():
        track = OutputAudioTrack()
        try:
            first = await track.recv()
            await asyncio.sleep(0.05)
            second = await track.recv()
            resumed = time.monotonic()
            third = await track.recv()
            assert time.monotonic() - resumed >= 0.018
            assert second.pts - first.pts == third.pts - second.pts == 480
            assert third.sample_rate == 24000
        finally:
            track.stop()

    asyncio.run(receive())


def test_opus_packets_decode_as_mono_with_continuous_timestamps():
    async def receive():
        track = OpusOutputTrack()
        decoder = CodecContext.create("libopus", "r")
        try:
            packets = [await track.recv() for _ in range(3)]
            assert [packet.pts for packet in packets] == [0, 320, 640]
            decoder.extradata = track.codec.extradata
            decoded = [frame for packet in packets for frame in decoder.decode(packet)]
            assert len(decoded) == 3
            assert all(len(frame.layout.channels) == 1 for frame in decoded)
            # Opus 首帧按编码器头信息去除预热采样，后续帧保持 20 毫秒。
            assert 0 < decoded[0].samples / decoded[0].sample_rate <= 0.02
            assert all(frame.samples / frame.sample_rate == 0.02 for frame in decoded[1:])
        finally:
            track.stop()

    asyncio.run(receive())


def test_streamed_pcm_preserves_chunk_boundaries_and_waits_for_playback_permission():
    with wave.open(str(Path(__file__).parents[1] / "fixtures" / "start.wav")) as source:
        source.setpos(12000)
        pcm = source.readframes(2801)
    assert any(pcm)

    async def receive():
        async def chunks():
            for offset in range(0, len(pcm), 202):
                yield pcm[offset:offset + 202]

        track = OutputAudioTrack()
        playback = PlaybackBuffer()
        track.playback = playback
        output = bytearray()
        try:
            async with asyncio.timeout(3), asyncio.TaskGroup() as group:
                group.create_task(playback.feed(chunks()))
                await playback.ready.wait()
                assert not any(bytes((await track.recv()).planes[0]))
                assert not playback.started.is_set()
                playback.enabled.set()
                while not playback.finished.is_set():
                    output.extend(bytes((await track.recv()).planes[0]))
            assert output[:len(pcm)] == pcm
            assert not any(output[len(pcm):])
            assert playback.metrics.pcm_bytes == len(pcm)
            assert playback.metrics.source_pcm_peak > 0
        finally:
            track.stop()

    asyncio.run(receive())


@pytest.mark.parametrize("chunks", [[], [b""], [b"\0"], [b"\0\0", b"\0"]])
def test_invalid_pcm_fails_before_successful_completion(chunks):
    async def receive():
        async def source():
            for chunk in chunks:
                yield chunk

        playback = PlaybackBuffer()
        with pytest.raises(ValueError):
            await playback.feed(source())
        assert not playback.finished.is_set()

    asyncio.run(receive())
