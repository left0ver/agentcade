import json
import os
import shutil
import signal
import subprocess
import sys
from itertools import pairwise

import pytest

from games.guess_animal.config import Settings as AnimalSettings
from robot_agents.config import ROOT

pytestmark = [pytest.mark.e2e, pytest.mark.robot]


def test_complete_voice_game_on_connected_robot(run_directory):
    report = run_robot(run_directory, "game")
    assert report["score"] == 3
    assert report["completed_rounds"] == 5
    assert len(report["checks"]) == 13, report["checks"]


def test_pcm_playback_cancellation_and_microphone_recovery(run_directory):
    report = run_robot(run_directory, "audio")
    assert report["checks"] == ["取消、再次播放和麦克风释放"]
    assert not report["rtc_events"]


def test_microphone_pause_owners_during_real_playback(run_directory):
    report = run_robot(run_directory, "microphone")
    assert report["checks"] == ["多个流程共同暂停收音并按顺序恢复"]


def test_repeated_audio_cancellation_and_microphone_transitions(run_directory):
    report = run_robot(run_directory, "audio_transitions")
    transitions = report["audio_transitions"]
    assert len(transitions) == 12
    assert all(after["microphone_bytes"] > before["microphone_bytes"]
               for before, after in pairwise(transitions))


def test_complete_number_game_on_connected_robot(run_directory):
    report = run_robot(run_directory, "number")
    assert report["number_won"]
    assert 1 <= len(report["number_guesses"]) <= 5
    assert len(report["checks"]) == 6, report["checks"]


def test_expression_progress_during_audio_playback(run_directory):
    report = run_robot(run_directory, "expressions")
    assert report["progress_cycles"] == [list(range(11))] * 3
    assert report["progress_cancelled"]


def test_answer_feedback_moves_head_with_speech(run_directory):
    report = run_robot(run_directory, "feedback")
    assert report["feedback_motions"] == ["incorrect", "correct", "incorrect", "incorrect"]
    assert report["motion_cancelled"]


def run_robot(run_directory, scenario):
    if sys.platform != "darwin":
        pytest.fail("语音 E2E 使用 MacBook 扬声器，请在连接机器人的 Mac 上执行")
    settings = AnimalSettings.from_environment()
    request = {"run_id": run_directory.name, "output": str(run_directory), "scenario": scenario}
    request["continue_on_error"] = os.environ.get("E2E_CONTINUE_ON_ERROR") == "1"
    if device := os.environ.get("E2E_OUTPUT_DEVICE"):
        request["output_device"] = device
    (ROOT / ".artifacts/tests/robot-request.json").write_text(json.dumps(request))
    environment = os.environ | {"PYTHONUNBUFFERED": "1"}
    executable = shutil.which("watcherobot")
    assert executable is not None, "当前 Python 环境未安装 watcherobot 命令"
    with (run_directory / "application.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [executable, "app", "run", "tests/e2e/robot_app"],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=environment,
        )
        try:
            code = process.wait(timeout=settings.llm.timeout_seconds * 6 + 900)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
    assert code == 0, f"真机运行失败，日志：{run_directory / 'application.log'}"
    log_text = (run_directory / "application.log").read_text(encoding="utf-8")
    for warning in ("Task exception was never retrieved", "Dispatcher task is not running"):
        assert warning not in log_text, f"SDK 后台任务异常：{warning}"
    report = json.loads((run_directory / "report.json").read_text(encoding="utf-8"))
    assert report["run_id"] == request["run_id"]
    assert report["complete"], report
    assert not report["rtc_active_after_shutdown"]
    assert not report["rtc_events"]
    assert not report["issues"], f"游戏流程已完成，但记录了异常：{report['issues']}"
    assert not any("超时" in item["text"] for item in report["spoken"]), (
        "游戏播报包含超时消息", report["spoken"],
    )
    return report
