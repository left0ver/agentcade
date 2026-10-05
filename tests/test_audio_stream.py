import asyncio
import time

from av import CodecContext

from guess_animal.audio_stream import OpusOutputTrack, OutputAudioTrack


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
