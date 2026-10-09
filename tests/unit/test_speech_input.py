import pytest

from robot_agents.audio_stream import CapturedPcm
from robot_agents.speech import SpeechTimeline


def test_sentence_time_tracks_capture_across_microphone_restart():
    timeline = SpeechTimeline()
    timeline.append(CapturedPcm(bytes(640), 100.0, False))
    timeline.append(CapturedPcm(bytes(640), 100.02, False))
    timeline.append(CapturedPcm(bytes(640), 105.0, False))
    assert timeline.started_at(5, 30) == pytest.approx(100.005)
    assert timeline.started_at(45, 60) == pytest.approx(105.005)
    assert timeline.captured_at(30) == pytest.approx(100.03)
    assert timeline.captured_at(60) == pytest.approx(105.02)
    assert timeline.captured_at(timeline.sent_samples / 16) == pytest.approx(105.02)


@pytest.mark.parametrize("begin,end", [(5, 40), (20, 40), (25, 60)])
def test_sentence_overlapping_muted_audio_is_ignored(begin, end):
    timeline = SpeechTimeline()
    for index, muted in enumerate((False, True, False)):
        timeline.append(CapturedPcm(bytes(640), 100 + index * 0.02, muted))
    assert timeline.started_at(begin, end) is None
    assert timeline.started_at(0, 20) == 100
    assert timeline.started_at(40, 60) == pytest.approx(100.04)


def test_timeline_retention_is_bounded_and_old_sentence_is_rejected():
    timeline = SpeechTimeline()
    for index in range(30001):
        timeline.append(CapturedPcm(bytes(640), index * 0.02, False))
    assert len(timeline.frames) == 30000
    with pytest.raises(ValueError, match="时间"):
        timeline.started_at(0, 20)


@pytest.mark.parametrize("data", [b"", b"\x00"])
def test_incomplete_pcm_is_rejected(data):
    with pytest.raises(ValueError, match="PCM"):
        SpeechTimeline().append(CapturedPcm(data, 100, False))
