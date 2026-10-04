import asyncio
import json
import sys
import time
from array import array
from contextlib import aclosing
from pathlib import Path

from watcherobot.application import ApplicationContext

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from guess_animal.animals import ANIMALS
from guess_animal.config import ROOT, Settings
from guess_animal.robot_game import RobotGame


async def main() -> None:
    settings = Settings.from_environment()
    async with ApplicationContext.from_environment() as app:
        game = RobotGame(app, settings)
        report = {"drawings": [], "playback_seconds": []}
        try:
            for animal in ANIMALS:
                await game.draw(animal)
                report["drawings"].append(animal.id)
                app.logger.info("设备接受全部绘图指令：%s", animal.name)
            for text in ("图案检查完成。", "正在检查语音播放。", "准备检查麦克风。"):
                start = time.monotonic()
                await game.speak(text)
                report["playback_seconds"].append(round(time.monotonic() - start, 2))
                app.logger.info("设备确认播放完成")
            audio_bytes = 0
            peak = 0
            async with aclosing(game.microphone(3)) as microphone:
                async for frame in microphone:
                    audio_bytes += len(frame)
                    peak = max(peak, max(abs(value) for value in array("h", frame)))
            if audio_bytes < 16000 * 2:
                raise AssertionError("麦克风有效音频不足一秒")
            report["microphone_bytes"] = audio_bytes
            report["microphone_peak"] = peak
            app.logger.info("麦克风检查通过：%s 字节 PCM，峰值 %s", audio_bytes, peak)
            await game.clear()
        finally:
            await game.judge.close()
        output = ROOT / ".artifacts"
        output.mkdir(exist_ok=True)
        (output / "device_check.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    asyncio.run(main())
