import wave
from pathlib import Path

import pytest

from robot_agents.audio_stream import PcmInputGain


def test_gain_processes_recorded_pcm_without_changing_sample_count():
    with wave.open(str(Path(__file__).parents[1] / "fixtures/start.wav")) as recording:
        pcm = recording.readframes(recording.getnframes())
    unchanged, amplified = PcmInputGain(1), PcmInputGain(4)
    original, louder = bytearray(), bytearray()
    for index in range(0, len(pcm), 640):
        frame = pcm[index:index + 640]
        original.extend(unchanged.process(frame))
        louder.extend(amplified.process(frame))
    assert original == pcm
    assert len(louder) == len(pcm)
    assert louder != pcm
    assert unchanged.samples == amplified.samples == len(pcm) // 2


@pytest.mark.parametrize("gain", [0, -1, 9])
def test_gain_rejects_invalid_values(gain):
    with pytest.raises(ValueError):
        PcmInputGain(gain)
