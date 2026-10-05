import asyncio
import json
import sys
import time
from array import array
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path

from watcherobot.application import ApplicationContext

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from guess_animal.animals import encode_vector
from guess_animal.audio_stream import PlaybackCallback
from guess_animal.config import ROOT, Settings
from guess_animal.expressions import expression_vector
from guess_animal.game import Decision, Game
from guess_animal.robot_game import RobotGame


class CheckedRobotGame(RobotGame):
    def __init__(self, app: ApplicationContext, settings: Settings):
        super().__init__(app, settings)
        self.displayed: list[str] = []
        self.display_times: list[float] = []
        self.playback_times: list[tuple[float, float]] = []

    async def show_vector(self, path: str) -> None:
        await super().show_vector(path)
        # 仅记录真实设备指令成功返回之后的图案。
        self.displayed.append(path)
        self.display_times.append(time.monotonic())

    async def play_audio(
        self, chunks: AsyncIterator[bytes], *, on_start: PlaybackCallback | None = None
    ) -> None:
        started = None

        async def record_start() -> None:
            nonlocal started
            if on_start is not None:
                await on_start()
            started = time.monotonic()

        await super().play_audio(chunks, on_start=record_start)
        if started is None:
            raise AssertionError("语音没有开始播放")
        self.playback_times.append((started, time.monotonic()))


async def main() -> None:
    settings = Settings.from_environment()
    async with ApplicationContext.from_environment() as app:
        game = CheckedRobotGame(app, settings)
        game.game = Game(rounds=2)
        report = {
            "complete": False,
            "questions": [],
            "expressions": [],
            "answers": [],
            "hints": [],
            "tts_voice": settings.tts_voice,
        }
        output = ROOT / ".artifacts"
        output.mkdir(exist_ok=True)
        try:
            for code in ("009", "141"):
                await game.show_vector(expression_vector(code))
                app.logger.info("表情 %s 已发送至机器人", code)
                report["expressions"].append(code)
                await asyncio.sleep(2)
            await game.show_vector(expression_vector("111"))
            for progress in range(1, 101):
                await asyncio.to_thread(
                    game.robot.expression_runtime.update,
                    custom_vector_path=expression_vector("111", progress),
                )
                await asyncio.sleep(0.04)
            report["progress_frames_acknowledged"] = 101
            report["expressions"].append("111")
            app.logger.info("111 的 101 个进度帧均已获得设备确认")
            await game.clear()
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
                    {
                        "animal": animal.id,
                        "hints": animal.hints,
                        "seconds": round(time.monotonic() - start, 2),
                    }
                )
                if game.game.hint_index != 0 or game.game.attempts != 0:
                    raise AssertionError("首次猜测前不能提供提示或消耗回答次数")
                wrong_name = next(
                    name
                    for name in ("长颈鹿", "企鹅", "乌龟", "鳄鱼")
                    if name not in (animal.name, *animal.aliases)
                )
                answers = (
                    (wrong_name, Decision.INCORRECT),
                    (animal.name, Decision.CORRECT)
                    if index == 0
                    else (wrong_name, Decision.INCORRECT),
                )
                for attempt, (answer, expected) in enumerate(answers, start=1):
                    decision = await game.judge.decide(answer, animal)
                    if decision is not expected:
                        raise AssertionError(f"Jev 返回 {decision}，预期为 {expected}")
                    reply = game.game.handle(decision, game.game.revision)
                    if reply.advance != (attempt == 2):
                        raise AssertionError("题目结束状态与回答次数不符")
                    if attempt == 1:
                        expected_hint = animal.hints[0].rstrip("。！？.!?") + "。"
                        if reply.text != "还没有答对。" + expected_hint:
                            raise AssertionError("首次答错必须提供第一条提示")
                        if game.game.hint_index != 1:
                            raise AssertionError("答错提示必须推进提示顺序")
                    before = len(game.displayed)
                    await game.present(reply)
                    expected_paths = [
                        expression_vector("009" if expected is Decision.CORRECT else "141")
                    ]
                    if expected is Decision.INCORRECT:
                        expected_paths.append(encode_vector(animal.strokes))
                    if game.displayed[before:] != expected_paths:
                        raise AssertionError("表情切换或动物图案恢复顺序不符")
                    shown = game.display_times[before]
                    playback_started, playback_finished = game.playback_times[-1]
                    if shown > playback_started:
                        raise AssertionError("播放回答语音之前必须显示反馈表情")
                    feedback_seconds = None
                    if expected is Decision.INCORRECT:
                        restored = game.display_times[before + 1]
                        feedback_seconds = restored - shown
                        if not 1.5 <= feedback_seconds <= 3:
                            raise AssertionError("答错表情显示时间超出 1.5 秒及设备通信容差")
                        if not playback_started < restored < playback_finished:
                            raise AssertionError("答错语音播放期间必须恢复动物图案")
                    report["answers"].append(
                        {
                            "question": index + 1,
                            "attempt": attempt,
                            "answer": answer,
                            "decision": decision.value,
                            "expression": "009" if expected is Decision.CORRECT else "141",
                            "animal_restored": expected is Decision.INCORRECT,
                            "feedback_seconds": feedback_seconds,
                            "text": reply.text,
                        }
                    )
                    app.logger.info(
                        "第 %s 题第 %s 次回答：表情及图案恢复检查通过", index + 1, attempt
                    )
                    if attempt == 1:
                        report["hints"].append(
                            {"question": index + 1, "hint_index": 0, "text": reply.text}
                        )
                reply = game.game.advance()
            if reply.prepare or game.game.active or game.game.score != 1:
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
            report["complete"] = True
        finally:
            try:
                await game.clear()
            finally:
                try:
                    await game.generator.close()
                finally:
                    await game.judge.close()
                    await game.conversation.close()
                    (output / "device_check.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )


if __name__ == "__main__":
    asyncio.run(main())
