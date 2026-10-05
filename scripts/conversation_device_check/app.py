import asyncio
import json
import sys
import time
import wave
from pathlib import Path

from watcherobot.application import ApplicationContext

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from guess_animal.config import ROOT, Settings
from guess_animal.robot_game import RobotGame
from guess_animal.speech import QwenSpeech, RecognizedSpeech


class CheckedConversation(RobotGame):
    def __init__(self, app, settings):
        super().__init__(app, settings)
        self.heard = []
        self.spoken = []

    def receive_utterance(self, utterance: RecognizedSpeech) -> None:
        super().receive_utterance(utterance)
        self.heard.append({
            "text": utterance.text, "time": time.monotonic(),
            "phase": self._utterances._queue[-1][1].phase,
        })

    async def _speak_now(self, text, *, feedback=None):
        started = time.monotonic()
        await super()._speak_now(text, feedback=feedback)
        self.spoken.append({"text": text, "started": started, "finished": time.monotonic()})


async def until(condition, seconds=45):
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.05)


async def main():
    settings = Settings.from_environment()
    speech = QwenSpeech(settings)
    output = ROOT / ".artifacts" / "conversation_device_check"
    output.mkdir(exist_ok=True)
    paths = {}
    for name, text in (
        ("start", "我们来玩你画我猜。"),
        ("muted", "请告诉我月亮是什么。"),
        ("chat", "企鹅为什么不会飞？"),
        ("end", "结束游戏。"),
    ):
        pcm = await speech.synthesize(text)
        path = output / f"{name}.wav"
        with wave.open(str(path), "wb") as destination:
            destination.setnchannels(1)
            destination.setsampwidth(2)
            destination.setframerate(24000)
            destination.writeframes(pcm)
        paths[name] = path

    async def say(name):
        process = await asyncio.create_subprocess_exec("/usr/bin/afplay", "-v", "6", str(paths[name]))
        try:
            async with asyncio.timeout(15):
                if await process.wait() != 0:
                    raise RuntimeError("电脑扬声器播放失败")
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()

    report = {"complete": False}
    async with ApplicationContext.from_environment() as app:
        game = CheckedConversation(app, settings)
        try:
            async with asyncio.TaskGroup() as group:
                running = group.create_task(game.run())
                await until(lambda: len(game.spoken) == 1)
                await say("start")
                await until(lambda: game._generation is not None and game._speaking)
                await asyncio.sleep(1)
                await say("muted")
                await until(lambda: any(item["text"].startswith("正在生成第") for item in game.spoken))
                await say("chat")
                await until(lambda: len(game.conversation.history) >= 2)
                answer = game.conversation.history[-1]["content"]
                await until(lambda: any(item["text"] == answer for item in game.spoken))
                if any("月亮" in item["text"] for item in game.heard):
                    raise AssertionError("播报期间的讲话进入了用户输入")
                question = next(item for item in game.heard if "企鹅" in item["text"])
                if question["phase"] not in {"生成题目", "绘制动物"}:
                    raise AssertionError(f"提问没有发生在准备题目期间：{question}")
                if game.game.attempts or game.game.score:
                    raise AssertionError("普通聊天改变了游戏的答题次数或分数")
                for previous, current in zip(game.spoken, game.spoken[1:]):
                    if previous["finished"] > current["started"]:
                        raise AssertionError("多条语音发生重叠")
                await until(lambda: not game._speaking and game._speech_queue.empty())
                await say("end")
                await until(lambda: any(item["text"].startswith("游戏结束") for item in game.spoken))
                if game.game.active:
                    raise AssertionError("结束指令没有关闭游戏")
                report["complete"] = True
                report["microphone_bytes"] = game.input.received_pcm_bytes
                running.cancel()
        finally:
            report["heard"] = game.heard
            report["spoken"] = game.spoken
            report["chat"] = list(game.conversation.history)
            report["device_stats"] = app.rtc.snapshot()["stats"]
            await game.clear()
            (output / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )


if __name__ == "__main__":
    asyncio.run(main())
