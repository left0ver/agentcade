import json
import os
import shutil
import signal
import subprocess
import sys

import pytest

from guess_animal.config import ROOT, Settings

pytestmark = [pytest.mark.e2e, pytest.mark.robot]


def test_complete_voice_game_on_connected_robot(run_directory):
    report = run_robot(run_directory, "game")
    assert report["score"] == 3
    assert report["completed_rounds"] == 5
    assert len(report["checks"]) == 12, report["checks"]


def test_expression_progress_during_streaming_audio(run_directory):
    report = run_robot(run_directory, "expressions")
    assert report["progress"] == list(range(101))


def run_robot(run_directory, scenario):
    if sys.platform != "darwin":
        pytest.fail("语音 E2E 使用 macOS afplay，请在连接机器人的 Mac 上执行")
    settings = Settings.from_environment()
    request = {"run_id": run_directory.name, "output": str(run_directory), "scenario": scenario}
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
    report = json.loads((run_directory / "report.json").read_text(encoding="utf-8"))
    assert report["run_id"] == request["run_id"]
    assert report["complete"], report
    assert not report["rtc_active_after_shutdown"]
    return report
