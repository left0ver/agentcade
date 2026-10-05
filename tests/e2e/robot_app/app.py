import asyncio
import json
import sys
import time
from pathlib import Path

from watcherobot.application import ApplicationContext

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from guess_animal.animals import encode_vector
from guess_animal.audio_stream import RtcAudioPlayer, rtc_command
from guess_animal.config import ROOT, Settings
from guess_animal.expressions import expression_vector
from guess_animal.robot_game import RobotGame
from guess_animal.speech import QwenSpeech
from tests.e2e.audio import write_wav


class ObservedGame(RobotGame):
    # 记录公开接口的真实输入和完成结果，所有操作仍调用生产实现。
    def __init__(self, app, settings):
        super().__init__(app, settings)
        self.heard, self.spoken, self.requests, self.displays, self.voices = [], [], [], [], []
        self.animals = []
        self.pending_speeches = 0
        self.playing = False

    async def speak(self, text, *, feedback=None, revision=None):
        self.requests.append(text)
        self.pending_speeches += 1
        try:
            await super().speak(text, feedback=feedback, revision=revision)
            self.spoken.append({"text": text, "time": time.monotonic(), "feedback": feedback})
        finally:
            self.pending_speeches -= 1

    async def play_audio(self, chunks, *, on_start=None):
        voice = {}

        async def started():
            if on_start is not None:
                await on_start()
            assert not self.input.opened.is_set(), "播放开始时 SDK 麦克风仍然开启"
            self.playing = True
            voice["started"] = time.monotonic()

        try:
            await super().play_audio(chunks, on_start=started)
            voice["finished"] = time.monotonic()
            self.voices.append(voice)
        finally:
            self.playing = False

    async def draw(self, animal):
        await super().draw(animal)
        self.animals.append(animal)

    async def show_vector(self, path):
        await super().show_vector(path)
        self.displays.append({"path": path, "time": time.monotonic()})

    def receive_utterance(self, utterance):
        super().receive_utterance(utterance)
        self.heard.append({"text": utterance.text, "time": time.monotonic()})


class VoiceScenario:
    def __init__(self, game, output, report, settings):
        self.game, self.output, self.report, self.settings = game, output, report, settings
        self.speech = QwenSpeech(settings)
        self.audio_files = {}

    async def until(self, condition, seconds=60):
        async with asyncio.timeout(seconds):
            while not condition():
                if self.game.app.shutdown_requested:
                    raise RuntimeError("设备测试收到停止请求")
                await asyncio.sleep(0.05)

    async def ready(self):
        await self.until(lambda: (
            self.game.pending_speeches == 0 and self.game.input.opened.is_set()
        ))

    async def audio_file(self, text):
        if text not in self.audio_files:
            path = self.output / f"input-{len(self.audio_files)}.wav"
            write_wav(path, await self.speech.synthesize(text))
            self.audio_files[text] = path
        return self.audio_files[text]

    async def say(self, text, *, during_playback=False):
        path = await self.audio_file(text)
        if not during_playback:
            await self.ready()
        self.game.app.logger.info("E2E 电脑播放：%s", text)
        process = await asyncio.create_subprocess_exec("/usr/bin/afplay", "-v", "6", str(path))
        try:
            async with asyncio.timeout(20):
                assert await process.wait() == 0, "电脑扬声器播放失败"
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()

    def spoken(self, text, since=0):
        return any(item["text"] == text for item in self.game.spoken[since:])

    async def question(self, number):
        await self.until(
            lambda: self.spoken(f"第{number}题画好了！请看图，说出动物名称。"),
            self.settings.llm.timeout_seconds + 90,
        )
        await self.ready()
        assert self.game.game.index == number - 1
        assert len(self.game.animals) == number
        assert self.game.game.attempts == self.game.game.hint_index == 0
        return self.game.game.animal

    def check(self, name):
        self.report["checks"].append(name)
        self.game.app.logger.info("E2E 通过：%s", name)

    async def expect(self, input_text, reply_text):
        before = len(self.game.spoken)
        await self.say(input_text)
        await self.until(lambda: self.spoken(reply_text, before))

    async def wrong_answer(self, animal, expected_hint=None):
        wrong = next(
            name for name in ("企鹅", "乌龟", "长颈鹿", "猫")
            if all(name not in alias and alias not in name for alias in (animal.name, *animal.aliases))
        )
        before_display, before_voice = len(self.game.displays), len(self.game.voices)
        text = f"这题的答案是{animal.name}。" if expected_hint is None else (
            "还没有答对。" + expected_hint.rstrip("。！？.!?") + "。"
        )
        await self.expect(f"我猜是{wrong}。", text)
        feedback, restored = self.game.displays[before_display:before_display + 2]
        assert feedback["path"] == expression_vector("141")
        assert restored["path"] == encode_vector(animal.strokes)
        assert 1.5 <= restored["time"] - feedback["time"] <= 3
        voice = self.game.voices[before_voice]
        assert feedback["time"] <= voice["started"] < restored["time"] < voice["finished"]

    async def correct_answer(self, animal):
        before_display, before_voice = len(self.game.displays), len(self.game.voices)
        await self.expect(f"我猜是{animal.name}。", f"答对了，是{animal.name}！")
        feedback = self.game.displays[before_display]
        assert feedback["path"] == expression_vector("009")
        assert feedback["time"] <= self.game.voices[before_voice]["started"]

    async def exercise(self):
        muted = "请告诉我月亮是什么。"
        for text in ("我们来玩你画我猜。", muted, "企鹅为什么不会飞？"):
            await self.audio_file(text)
        await self.until(lambda: bool(self.game.spoken))
        await self.say("我们来玩你画我猜。")
        await self.until(lambda: (
            any(text.startswith("正在生成第1题") for text in self.game.requests)
            and self.game.playing
        ))
        await self.say(muted, during_playback=True)
        await self.until(lambda: any(
            item["text"].startswith("正在生成第1题") for item in self.game.spoken
        ))
        assert self.game.game.animal is None
        await self.say("企鹅为什么不会飞？")
        await self.until(lambda: len(self.game.conversation.history) >= 2)
        answer = self.game.conversation.history[-1]["content"]
        await self.until(lambda: self.spoken(answer))
        assert not any("月亮" in item["text"] for item in self.game.heard)
        assert self.game.game.attempts == self.game.game.score == 0
        self.check("生成期间交流与播报期间暂停收音")

        animal = await self.question(1)
        assert expression_vector("111") in [item["path"] for item in self.game.displays]
        self.check("真实生成全身图案并完成显示")
        await self.expect("给我一点提示。", animal.hints[0].rstrip("。！？.!?") + "。")
        assert self.game.game.attempts == 0 and self.game.game.hint_index == 1
        self.check("主动提示不消耗回答次数")
        await self.wrong_answer(animal, animal.hints[1])
        assert self.game.game.attempts == 1 and self.game.game.hint_index == 2
        self.check("答错继续提示并恢复动物图案")
        await self.expect("再给我一个提示。", animal.hints[2].rstrip("。！？.!?") + "。")
        await self.expect("再给我一个提示。", "提示已经全部说完了，再看看图案吧。")
        assert self.game.game.attempts == 1
        self.check("提示用完后不重复线索")
        await self.correct_answer(animal)
        assert self.game.game.score == 1
        self.check("答对计分与答对表情")

        animal = await self.question(2)
        await self.wrong_answer(animal, animal.hints[0])
        await self.wrong_answer(animal)
        assert self.game.game.score == 1
        self.check("回答机会用完后公布答案")

        animal = await self.question(3)
        await self.expect("下一题。", f"这题的答案是{animal.name}。")
        assert self.game.game.score == 1
        self.check("跳过题目不计分")

        animal = await self.question(4)
        before = len(self.game.conversation.history)
        await self.say("请说出我们之前讨论的动物，再说说它怎样游泳。")
        await self.until(lambda: len(self.game.conversation.history) > before)
        answer = self.game.conversation.history[-1]["content"]
        assert "企鹅" in answer
        await self.until(lambda: self.spoken(answer))
        assert self.game.game.attempts == 0 and self.game.game.score == 1
        self.check("连续交流保留上下文且不影响答题")
        await self.correct_answer(animal)
        animal = await self.question(5)
        await self.correct_answer(animal)
        await self.until(lambda: any(
            item["text"].startswith("这一局完成啦！5题里，你答对了3题")
            for item in self.game.spoken
        ))
        assert not self.game.game.active and self.game.game.score == 3
        assert len({item.name for item in self.game.animals}) == 5
        self.report["score"], self.report["completed_rounds"] = 3, 5
        self.check("完整五题游戏结束并正确统计分数")

        before = len(self.game.heard)
        await self.say(muted)
        await self.until(lambda: any("月亮" in item["text"] for item in self.game.heard[before:]))
        await self.until(lambda: any("月亮" in item["text"] for item in self.game.spoken))
        await self.ready()
        before = len(self.game.spoken)
        await self.say("我们来玩你画我猜。")
        await self.until(lambda: any(
            item["text"].startswith("正在生成第1题") for item in self.game.spoken[before:]
        ))
        await self.expect("结束游戏。", "游戏结束，你答对了0题。想继续时，说我们来玩你画我猜。")
        assert not self.game.game.active
        assert len(self.game.animals) == 5
        for previous, current in zip(self.game.voices, self.game.voices[1:]):
            assert previous["finished"] <= current["started"]
        self.check("播报后恢复交流且生成期间能够结束游戏")


async def check_audio_cleanup(app, settings, report):
    player, speech = RtcAudioPlayer(app.rtc), QwenSpeech(settings)
    started = asyncio.Event()

    async def on_start():
        started.set()

    async with asyncio.TaskGroup() as group:
        playing = group.create_task(player.play(
            speech.synthesize_stream("现在检查关闭应用时的资源释放。这段语音会在播放过程中停止。"),
            on_start=on_start,
        ))
        await started.wait()
        await asyncio.sleep(1)
        playing.cancel()
    assert playing.cancelled() and not app.rtc.snapshot()["active"]
    metrics = await player.play(speech.synthesize_stream("语音播放已经恢复，测试即将结束。"))
    assert metrics.first_frame_seconds < metrics.input_finished_seconds
    assert metrics.source_pcm_peak and metrics.device_i2s_bytes
    assert not app.rtc.snapshot()["active"]
    microphone = await asyncio.to_thread(app.robot.microphone.open_pcm)
    try:
        frame = await asyncio.to_thread(microphone.read, timeout=2)
        assert frame.data and not microphone.dropped_frames and not microphone.decode_failures
    finally:
        await asyncio.to_thread(microphone.close)
    report["checks"].append("流式播放、取消、再次播放和麦克风释放")


async def check_expression_progress(game, report):
    report["progress"] = []
    await game.show_vector(expression_vector("111"))
    report["progress"].append(0)
    async with asyncio.TaskGroup() as group:
        playing = group.create_task(game.audio.play(game.speech.synthesize_stream(
            "我正在准备一道新的动物题目。你可以观察屏幕上的进度条，也可以想一想，"
            "你认识哪些生活在森林里的动物，哪些动物喜欢游泳，哪些动物会在天空中飞翔。"
        )))
        for progress in range(1, 101):
            report["requested_progress"] = progress
            await rtc_command(
                game.robot.expression_runtime.update,
                custom_vector_path=expression_vector("111", progress),
            )
            report["progress"].append(progress)
            await asyncio.sleep(0.2)
        metrics = await playing
        assert metrics.first_frame_seconds < metrics.input_finished_seconds


async def main():
    request = json.loads((ROOT / ".artifacts/tests/robot-request.json").read_text())
    output = Path(request["output"])
    report = {"run_id": request["run_id"], "complete": False, "checks": []}
    settings = Settings.from_environment()
    async with ApplicationContext.from_environment() as app:
        game = ObservedGame(app, settings)
        try:
            if request["scenario"] == "expressions":
                try:
                    await check_expression_progress(game, report)
                finally:
                    await game.generator.close()
                    await game.conversation.close()
                    await game.judge.close()
            else:
                async with asyncio.TaskGroup() as group:
                    running = group.create_task(game.run())
                    await VoiceScenario(game, output, report, settings).exercise()
                    running.cancel()
            assert not app.rtc.snapshot()["active"]
            if request["scenario"] == "game":
                await check_audio_cleanup(app, settings, report)
            report["complete"] = True
        finally:
            report.update(
                heard=game.heard, spoken=game.spoken, displays=game.displays, voices=game.voices,
                microphone_bytes=game.input.received_pcm_bytes,
                rtc_active_after_shutdown=app.rtc.snapshot()["active"],
            )
            try:
                await game.clear()
            finally:
                (output / "report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )


if __name__ == "__main__":
    asyncio.run(main())
