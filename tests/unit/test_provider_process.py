import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from guess_animal.config import ROOT
from guess_animal.providers import run_cli


@pytest.fixture
def work_directory():
    artifacts = ROOT / ".artifacts"
    artifacts.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="process-test-", dir=artifacts) as directory:
        yield Path(directory)


def test_process_receives_unicode_prompt_as_data(work_directory):
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
    ]
    prompt = "生成动物全身图案。参数包含空格、换行\n以及 `命令` 和 $(命令)。"
    output = asyncio.run(run_cli(command, prompt, work_directory, 10))
    assert output == prompt


def test_nonzero_exit_reports_failure_and_log_location(work_directory):
    command = [sys.executable, "-c", "raise SystemExit(7)"]
    with pytest.raises(RuntimeError, match="退出码为 7") as error:
        asyncio.run(run_cli(command, "", work_directory, 10))
    assert str(work_directory / "stderr.log") in str(error.value)


@pytest.mark.skipif(os.name != "posix", reason="使用 POSIX 进程查询确认清理结果")
def test_timeout_terminates_real_process(work_directory):
    command = [
        sys.executable,
        "-c",
        "import os,time; print(os.getpid(), flush=True); time.sleep(30)",
    ]
    with pytest.raises(TimeoutError):
        asyncio.run(run_cli(command, "", work_directory, 1))
    pid = int((work_directory / "stdout.log").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="使用 POSIX 进程查询确认清理结果")
def test_cancellation_terminates_real_process(work_directory):
    async def cancel_running_process():
        command = [
            sys.executable,
            "-c",
            "import os,time; print(os.getpid(), flush=True); time.sleep(30)",
        ]
        task = asyncio.create_task(run_cli(command, "", work_directory, 30))
        output_path = work_directory / "stdout.log"
        try:
            async with asyncio.timeout(5):
                while not output_path.exists() or not output_path.read_text():
                    await asyncio.sleep(0.01)
            return int(output_path.read_text())
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    pid = asyncio.run(cancel_running_process())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
