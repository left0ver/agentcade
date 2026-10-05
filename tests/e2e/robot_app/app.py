import asyncio
import json
import sys
import time
import wave
from contextlib import aclosing
from pathlib import Path

from watcherobot.application import ApplicationContext

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from games.guess_animal.agent import GameAgent as GuessAnimalAgent
from games.guess_animal.generator import GeneratedRound
from robot_agents.application import RobotApplication
from robot_agents.audio_stream import PcmAudioPlayer
from robot_agents.config import ROOT, Settings
from robot_agents.expressions import expression_vector, segmented_progress_vector
from robot_agents.microphone import MicrophoneInput
from robot_agents.output import OutputCoordinator
from robot_agents.protocol import GameRequest, Presentation
from robot_agents.speech import QwenSpeech
from robot_agents.transport import connect_games
from robot_agents.vector import encode_vector
from tests.e2e.audio import write_wav
from tests.samples import recorded_animals


class RecordedInput(MicrophoneInput):
    def __init__(self, robot, destination):
        super().__init__(robot)
        self.destination = destination

    async def audio(self):
        with wave.open(str(self.destination), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(16000)
            async with aclosing(super().audio()) as source:
                async for frame in source:
                    recording.writeframesraw(frame.data)
                    yield frame


class ObservedOutput(OutputCoordinator):
    def __init__(self, app, settings, microphone, report):
        super().__init__(app, settings, microphone)
        self.report = report

    async def present(self, output):
        self.report["requested"].append(output.model_dump())
        await super().present(output)
        self.report["spoken"].append(output.model_dump())

    async def _show_vector(self, path):
        await super()._show_vector(path)
        self.report["displays"].append({"path": path, "time": time.monotonic()})

    async def gesture(self, name):
        assert not self.input.opened.is_set()
        await super().gesture(name)
        assert not self.input.opened.is_set()
        self.report["gestures"].append(name)


class VoiceScenario:
    def __init__(self, application, output, report):
        self.application, self.directory, self.report = application, output, report
        self.audio_files = {}

    async def until(self, condition, seconds=90):
        async with asyncio.timeout(seconds):
            while not condition():
                if self.application.app.shutdown_requested:
                    raise RuntimeError("设备测试收到停止请求")
                await asyncio.sleep(0.05)

    async def ready(self):
        await self.until(lambda: (
            self.application.input.opened.is_set()
            and not self.application.output.speaking
            and self.application.agent.outputs.empty()
        ))

    async def audio_file(self, text):
        if text not in self.audio_files:
            path = self.directory / f"input-{len(self.audio_files)}.wav"
            write_wav(path, await self.application.speech.synthesize(text))
            self.audio_files[text] = path
        return self.audio_files[text]

    async def say(self, text, *, during_playback=False):
        path = await self.audio_file(text)
        if not during_playback:
            await self.ready()
        self.application.app.logger.info("E2E 电脑播放：%s", text)
        process = await asyncio.create_subprocess_exec("/usr/bin/afplay", "-v", "6", str(path))
        try:
            async with asyncio.timeout(20):
                assert await process.wait() == 0
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()

    async def expect(self, text, reply):
        before = len(self.report["spoken"])
        await self.say(text)
        await self.until(lambda: any(
            reply in item["text"] for item in self.report["spoken"][before:]
        ))

    async def question(self, number):
        await self.until(lambda: any(
            item["text"] == f"第{number}题画好了！请看图，说出动物名称。"
            for item in self.report["spoken"]
        ), self.application.settings.llm.timeout_seconds + 120)
        await self.ready()
        paths = next(item["paths"] for item in reversed(self.report["spoken"]) if item["paths"])
        # 对照真实生成归档验证当前图案和答案。
        for path in sorted((ROOT / ".artifacts/generated").glob("*.json"), reverse=True):
            generated = GeneratedRound.model_validate_json(path.read_text())
            for item in generated.animals:
                animal = item.animal()
                if encode_vector(animal.strokes) == paths[-1]:
                    return animal
        raise AssertionError("当前图案没有对应的真实生成归档")

    def check(self, name):
        self.report["checks"].append(name)

    async def wrong(self, animal, reply):
        wrong = next(name for name in ("企鹅", "乌龟", "长颈鹿", "猫")
                     if name not in (animal.name, *animal.aliases))
        before = len(self.report["displays"])
        await self.expect(f"我猜是{wrong}。", reply)
        displays = self.report["displays"][before:]
        assert displays[0]["path"] == expression_vector("141")
        assert displays[1]["path"] == encode_vector(animal.strokes)
        assert 1.5 <= displays[1]["time"] - displays[0]["time"] <= 3

    async def exercise_number(self):
        await self.until(lambda: bool(self.report["spoken"]))
        await self.expect("开始猜数字游戏。", "你有五次机会")
        original = self.application.agent.active.context_id
        await self.expect("换成你画我猜。", "请先说结束游戏")
        assert self.application.agent.active.context_id == original
        before = len(self.report["spoken"])
        await self.say("给我一个提示。")
        await self.until(lambda: any(
            item["text"] in ("它是一个偶数。", "它是一个奇数。")
            for item in self.report["spoken"][before:]
        ))
        self.check("猜数字提示与游戏互斥")
        lower, upper = 1, 20
        self.report["number_guesses"] = []
        for attempt in range(1, 6):
            number = (lower + upper) // 2
            before = len(self.report["spoken"])
            await self.say(f"我猜的数字是{number}。")
            await self.until(lambda before=before: any(
                item["text"].startswith(("猜大了", "猜小了", "答对了，数字是", "五次机会用完了"))
                for item in self.report["spoken"][before:]
            ))
            reply = next(item["text"] for item in self.report["spoken"][before:]
                         if item["text"].startswith(
                             ("猜大了", "猜小了", "答对了，数字是", "五次机会用完了")))
            self.report["number_guesses"].append({"number": number, "reply": reply})
            if reply.startswith("答对了"):
                assert reply == f"答对了，数字是{number}！你用了{attempt}次机会。"
                await self.until(lambda: self.application.agent.active is None)
                self.report["number_won"] = True
                self.check("真实语音猜数字直至获胜")
                break
            assert f"还有{5 - attempt}次机会" in reply
            if reply.startswith("猜大了"):
                upper = number - 1
            else:
                lower = number + 1
            assert lower <= upper
        assert self.report.get("number_won"), self.report["number_guesses"]
        await self.expect("开始猜数字游戏。", "你有五次机会")
        await self.expect("结束游戏。", "猜数字游戏已经结束")
        await self.until(lambda: self.application.agent.active is None)
        self.check("猜数字重新开始与主动退出")

    async def exercise(self):
        muted = "请告诉我月亮是什么。"
        await self.audio_file(muted)
        await self.until(lambda: bool(self.report["spoken"]))
        await self.say("我们来玩你画我猜。")
        await self.until(lambda: any(
            item["text"].startswith("正在生成第1题") for item in self.report["requested"]
        ) and self.application.output.speaking)
        await self.say(muted, during_playback=True)
        await self.ready()
        await self.expect("企鹅为什么不会飞？", "企鹅")
        assert not any("月亮" in item["content"] for item in self.application.agent.history)
        self.check("生成期间交流与播报期间暂停收音")
        animal = await self.question(1)
        self.check("真实生成全身图案并完成显示")
        await self.expect("给我一点提示。", animal.hints[0].rstrip("。！？.!?") + "。")
        self.check("主动提示不消耗回答次数")
        await self.wrong(animal, animal.hints[1].rstrip("。！？.!?") + "。")
        self.check("答错继续提示并恢复动物图案")
        await self.expect("再给我一个提示。", animal.hints[2].rstrip("。！？.!?") + "。")
        await self.expect("再给我一个提示。", "提示已经全部说完了")
        self.check("提示用完后不重复线索")
        await self.expect(f"我猜是{animal.name}。", f"答对了，是{animal.name}")
        self.check("答对计分与答对表情")
        animal = await self.question(2)
        await self.wrong(animal, animal.hints[0].rstrip("。！？.!?") + "。")
        await self.wrong(animal, f"这题的答案是{animal.name}")
        self.check("回答机会用完后公布答案")
        animal = await self.question(3)
        await self.expect("下一题。", f"这题的答案是{animal.name}")
        self.check("跳过题目不计分")
        animal = await self.question(4)
        await self.expect("企鹅是怎样游泳的？", "企鹅")
        self.check("游戏期间交流")
        await self.expect(f"我猜是{animal.name}。", f"答对了，是{animal.name}")
        animal = await self.question(5)
        await self.expect(f"我猜是{animal.name}。", f"答对了，是{animal.name}")
        await self.until(lambda: any("5题里，你答对了3题" in item["text"]
                                    for item in self.report["spoken"]))
        assert self.application.agent.active is None
        self.report.update(score=3, completed_rounds=5)
        self.check("完整五题游戏与最终分数")
        await self.expect("开始猜数字游戏。", "你有五次机会")
        original = self.application.agent.active.context_id
        await self.expect("换成你画我猜。", "请先说结束游戏")
        assert self.application.agent.active.context_id == original
        await self.expect("结束游戏。", "猜数字游戏已经结束")
        await self.until(lambda: self.application.agent.active is None)
        self.check("新增游戏与单游戏限制")


async def check_audio_cleanup(app, settings, report):
    player, speech = PcmAudioPlayer(app.robot.audio), QwenSpeech(settings)
    started = asyncio.Event()
    pcm = await speech.synthesize("现在检查关闭应用时的资源释放。这段语音会在播放过程中停止。")

    async def on_start():
        started.set()

    async with asyncio.TaskGroup() as group:
        playing = group.create_task(player.play(pcm, on_start=on_start))
        await started.wait()
        await asyncio.sleep(1)
        playing.cancel()
    assert playing.cancelled() and not player.playing
    pcm = await speech.synthesize("语音播放已经恢复，测试即将结束。")
    metrics = await player.play(pcm)
    assert metrics.pcm_bytes == len(pcm) and metrics.playback_id > 0
    assert not app.rtc.snapshot()["active"]
    microphone = await asyncio.to_thread(app.robot.microphone.open_pcm)
    try:
        frame = await asyncio.to_thread(microphone.read, timeout=2)
        assert frame.data and not microphone.dropped_frames and not microphone.decode_failures
    finally:
        await asyncio.to_thread(microphone.close)
    report["checks"].append("取消、再次播放和麦克风释放")


async def check_expression_progress(application, report):
    output = application.output
    report["progress_cycles"] = []

    async def speak_twice():
        for text in ("我正在准备一道新的动物题目。", "准备期间也可以继续聊天。"):
            await output.present(Presentation(text=text))
            await asyncio.sleep(1)

    async with asyncio.TaskGroup() as group:
        capturing = group.create_task(application.input.capture())
        await application.input.wait_ready()
        ready = asyncio.Event()
        listening = group.create_task(application.listen(ready))
        await ready.wait()
        for _ in range(3):
            before = len(report["displays"])
            await output.present(Presentation(
                text="正在准备题目，请稍等。", expression="thinking", progress_seconds=20,
            ))
            token = output.progress.token
            playing = group.create_task(speak_twice())
            await output.progress.task
            assert output.progress.step == 9
            await playing
            await asyncio.sleep(3)
            assert output.progress.step == 9
            await output.progress.finish()
            frames = report["displays"][before:]
            assert [frame["path"] for frame in frames] == [
                segmented_progress_vector(step) for step in range(11)
            ]
            report["progress_cycles"].append(list(range(11)))
            current = await output.progress.start()
            before = len(report["displays"])
            await output.progress.frame(token, 8)
            assert len(report["displays"]) == before
            output.progress.advance(current, 20)
            await output.stop_progress()
            await output.progress.frame(current, 9)
            assert len(report["displays"]) == before
            assert output.progress.task is None and not output.progress.active
        report["progress_cancelled"] = True
        listening.cancel()
        capturing.cancel()


async def check_feedback_motion(application, report):
    agent = GuessAnimalAgent(application.settings)
    animal, second, *_ = recorded_animals()
    try:
        async with asyncio.TaskGroup() as group:
            capturing = group.create_task(application.input.capture())
            await application.input.wait_ready()
            agent.game.start()
            agent.game.set_question(animal)
            agent.phase = "等待猜测"
            await application.output.present(Presentation(paths=list(animal.drawing_steps())))
            for index, text in enumerate((
                "我猜是企鹅", f"我猜是{animal.name}", "我猜是企鹅", "我猜是企鹅",
            )):
                if index == 2:
                    agent.game.advance()
                    agent.game.set_question(second)
                    await application.output.present(Presentation(paths=list(second.drawing_steps())))
                result = await agent.handle(GameRequest(
                    action="input", text=text, revision=agent.game.revision,
                ))
                for item in result.outputs:
                    await application.output.present(item)
                assert application.input.opened.is_set()
            report["feedback_motions"] = [
                {"nod": "correct", "shake_head": "incorrect"}[name]
                for name in report["gestures"]
            ]
            await application.input.pause()
            motion = asyncio.create_task(application.output.gesture("nod"))
            await asyncio.sleep(0.05)
            motion.cancel()
            await asyncio.gather(motion, return_exceptions=True)
            assert motion.cancelled()
            report["motion_cancelled"] = True
            capturing.cancel()
    finally:
        await agent.close()


async def main():
    request = json.loads((ROOT / ".artifacts/tests/robot-request.json").read_text())
    directory = Path(request["output"])
    report = {"run_id": request["run_id"], "complete": False, "checks": [],
              "requested": [], "spoken": [], "displays": [], "gestures": []}
    settings = Settings.from_environment()
    async with (
        ApplicationContext.from_environment() as app,
        connect_games(settings, ROOT / "games.json") as games,
    ):
        report["rtc_events"] = []

        def observe(message):
            if message["type"].startswith("evt.rtc."):
                report["rtc_events"].append(message["type"])

        app._transport.add_message_listener(observe)
        application = RobotApplication(app, settings, games)
        application.input = RecordedInput(app.robot, directory / "microphone.wav")
        application.output = ObservedOutput(app, settings, application.input, report)
        try:
            if request["scenario"] == "audio":
                await check_audio_cleanup(app, settings, report)
            elif request["scenario"] == "expressions":
                await check_expression_progress(application, report)
            elif request["scenario"] == "feedback":
                await check_feedback_motion(application, report)
            else:
                async with asyncio.TaskGroup() as group:
                    running = group.create_task(application.run())
                    scenario = VoiceScenario(application, directory, report)
                    if request["scenario"] == "number":
                        await scenario.exercise_number()
                    else:
                        await scenario.exercise()
                    running.cancel()
                await check_audio_cleanup(app, settings, report)
            assert not app.rtc.snapshot()["active"]
            assert not report["rtc_events"], report["rtc_events"]
            report["complete"] = True
        finally:
            await application.agent.close()
            await application.output.close()
            await application.output.clear()
            report["rtc_active_after_shutdown"] = app.rtc.snapshot()["active"]
            report["microphone_bytes"] = application.input.received_pcm_bytes
            (directory / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
            )


if __name__ == "__main__":
    asyncio.run(main())
