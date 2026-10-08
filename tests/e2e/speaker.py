import wave
from threading import Event

import sounddevice

from robot_agents.audio_stream import PcmInputGain, sdk_command
from tests.e2e.audio import asr_frames


class HostSpeaker:
    def __init__(self, name=None):
        if name is None:
            devices = [device for device in sounddevice.query_devices()
                       if "MacBook" in device["name"] and device["max_output_channels"] > 0]
            if len(devices) != 1:
                raise RuntimeError("请通过 E2E_OUTPUT_DEVICE 指定测试扬声器名称")
            self.device = devices[0]
        else:
            self.device = sounddevice.query_devices(name, kind="output")
        sounddevice.check_output_settings(
            device=self.device["index"], samplerate=16000, channels=1, dtype="int16",
        )

    def _play(self, path):
        with wave.open(str(path), "rb") as recording:
            if (recording.getframerate(), recording.getnchannels(), recording.getsampwidth()) != (
                24000, 1, 2,
            ):
                raise ValueError("测试语音需要 24 kHz 单声道 16-bit PCM")
            pcm = b"".join(asr_frames(recording.readframes(recording.getnframes())))
        pcm = PcmInputGain(6).process(pcm)
        block_bytes = 2048
        blocks = [pcm[offset:offset + block_bytes].ljust(block_bytes, b"\0")
                  for offset in range(0, len(pcm), block_bytes)]
        finished = Event()
        position = 0
        underflow = False

        def callback(output, frames, timing, status):
            nonlocal position, underflow
            output[:] = blocks[position]
            position += 1
            if status.output_underflow:
                underflow = True
                raise sounddevice.CallbackAbort
            if position == len(blocks):
                raise sounddevice.CallbackStop

        with sounddevice.RawOutputStream(
            device=self.device["index"], samplerate=16000, channels=1, dtype="int16",
            blocksize=1024, latency=0.1, callback=callback, finished_callback=finished.set,
            prime_output_buffers_using_stream_callback=True,
        ):
            if not finished.wait(len(pcm) / 32000 + 10):
                raise TimeoutError("测试扬声器播放超时")
            if underflow:
                raise RuntimeError("测试扬声器播放发生音频缓冲不足")
            if position != len(blocks):
                raise RuntimeError("测试扬声器没有播放完整音频")

    async def play(self, path):
        await sdk_command(self._play, path)
