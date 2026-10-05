import wave
from pathlib import Path

from av import AudioFrame, AudioResampler


def write_wav(path: Path, pcm: bytes, rate: int = 24000) -> None:
    with wave.open(str(path), "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(rate)
        destination.writeframes(pcm)


def asr_frames(pcm: bytes) -> list[bytes]:
    frame = AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
    frame.sample_rate = 24000
    frame.planes[0].update(pcm)
    resampler = AudioResampler(format="s16", layout="mono", rate=16000, frame_size=320)
    frames = resampler.resample(frame) + resampler.resample(None)
    return [bytes(item.planes[0])[:item.samples * 2] for item in frames]
