import asyncio
import json
import sys
import time
from array import array
from contextlib import aclosing
from pathlib import Path

from watcherobot.application import ApplicationContext

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from guess_animal.config import ROOT, Settings
from guess_animal.game import Decision, Game
from guess_animal.robot_game import RobotGame


async def main() -> None:
    settings = Settings.from_environment()
    async with ApplicationContext.from_environment() as app:
        game = RobotGame(app, settings)
        game.game = Game(rounds=2)
        report = {"questions": []}
        try:
            reply = game.game.start()
            for index in range(2):
                if not reply.prepare or game.game.animal is not None:
                    raise AssertionError("当前必须处于等待生成单题的状态")
                if len(game.game.deck) != index:
                    raise AssertionError("下一题不能提前生成")
                start = time.monotonic()
                await game.present(reply)
                animal = game.game.animal
                if animal is None or len(game.game.deck) != index + 1:
                    raise AssertionError("生成和完成提示之后必须进入当前题目")
                report["questions"].append(
                    {"animal": animal.id, "seconds": round(time.monotonic() - start, 2)}
                )
                decision = await game.judge.decide(animal.name, animal)
                if decision is not Decision.CORRECT:
                    raise AssertionError("Jev 必须认可真实生成的答案")
                reply = game.game.handle(decision, game.game.revision)
                if not reply.advance:
                    raise AssertionError("正确回答之后必须结束当前题目")
                await game.present(reply)
                reply = game.game.advance()
            if reply.prepare or game.game.active or game.game.score != 2:
                raise AssertionError("两题结束之后必须完成游戏并停止出题")
            await game.present(reply)
            report["score"] = game.game.score
            await game.speak("准备检查麦克风。")
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
            try:
                await game.generator.close()
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
