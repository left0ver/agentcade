import asyncio
import hashlib
import json
import time

import pytest
from a2a.types import ListTasksRequest, TaskState

from games.guess_animal.generator import GeneratedRound
from games.guess_number.agent import GameAgent as GuessNumberAgent
from robot_agents.config import ROOT, Settings
from robot_agents.main_agent import MainAgent
from robot_agents.protocol import GameRequest
from robot_agents.transport import connect_games, serve_game
from robot_agents.vector import encode_vector

pytestmark = pytest.mark.e2e


def test_remote_registration_reads_agent_card_and_runs_game(run_directory):
    async def exercise():
        settings = GuessNumberAgent.load_settings()
        async with serve_game(GuessNumberAgent, settings) as url:
            catalog = run_directory / "remote-games.json"
            catalog.write_text(json.dumps([
                {"kind": "remote", "id": "guess_number", "url": url},
            ]), encoding="utf-8")
            async with connect_games(catalog) as games:
                connection = games["guess_number"]
                assert connection.descriptor == GuessNumberAgent.descriptor
                start = await connection.send("remote-session", GameRequest(action="start"))
                assert start.state.active and "五次机会" in start.outputs[0].text
                end = await connection.send("remote-session", GameRequest(action="end"))
                assert not end.state.active
    asyncio.run(exercise())


def test_a2a_game_start_and_end_use_real_http():
    async def exercise():
        async with connect_games(ROOT / "games.json") as games:
            connection = games["guess_animal"]
            start = GameRequest(action="start")
            updates = []

            async def record(result):
                updates.append(result)

            result = await connection.send("test-animal-session", start, on_update=record)
            assert updates[0].state.phase == "生成题目"
            assert result.state.active and result.state.phase == "等待猜测"
            assert result.outputs[0].paths
            assert result == await connection.send("test-animal-session", start)
            ended = await connection.send("test-animal-session", GameRequest(action="end"))
            assert not ended.state.active
            assert "游戏结束" in ended.outputs[0].text
    asyncio.run(exercise())


def test_registered_second_agent_and_main_agent_interaction(run_directory):
    async def exercise():
        hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in (ROOT / "robot_agents").glob("*.py")}
        transcript = []
        catalog = json.loads((ROOT / "games.json").read_text())
        registration = run_directory / "games.json"
        registration.write_text(json.dumps(catalog[:1]), encoding="utf-8")
        async with connect_games(registration) as initial:
            assert list(initial) == ["guess_animal"]
        registration.write_text(json.dumps(catalog), encoding="utf-8")
        async with connect_games(registration) as games:
            agent = MainAgent(Settings.from_environment(), games)

            async def consume():
                request = await asyncio.wait_for(agent.outputs.get(), 30)
                if agent.output_current(request):
                    transcript.extend(output.text for output in request.outputs if output.text)
                await agent.output_done(request)
                agent.outputs.task_done()
                return request

            try:
                await agent.receive("有哪些游戏？")
                await consume()
                assert "猜数字" in transcript[-1] and "你画我猜" in transcript[-1]
                await agent.receive("我们来玩第二个游戏。")
                before_presentation = time.monotonic()
                await consume()
                assert agent.active.game_id == "guess_number"
                await agent.receive("我猜数字是10。", started_at=before_presentation)
                await consume()
                assert "刚才那次已经算过" in transcript[-1]
                assert "已经猜了0次" in agent.active.state.status
                original = agent.active.context_id
                revision = agent.active.state.revision
                await agent.receive("企鹅为什么不会飞？")
                await consume()
                assert agent.active.state.revision == revision
                assert "企鹅" in transcript[-1]
                await agent.receive("换成你画我猜。")
                await consume()
                assert agent.active.context_id == original
                assert "结束" in transcript[-1]
                await agent.tick()
                assert not agent.pending
                await agent.receive("继续游戏。")
                await consume()
                assert agent.active.state.revision == revision
                low, high = 1, 20
                for _ in range(5):
                    number = (low + high) // 2
                    await agent.receive(f"我猜数字是{number}。")
                    await consume()
                    if agent.active is None:
                        assert "答对了" in transcript[-1], transcript
                        break
                    if "大了" in transcript[-1]:
                        high = number - 1
                    elif "小了" in transcript[-1]:
                        low = number + 1
                    else:
                        raise AssertionError(transcript[-1])
                assert agent.active is None
                # 两个真实游戏同时申请开始，只允许一个获得会话。
                await asyncio.gather(
                    agent.receive("开始猜数字游戏"),
                    agent.receive("开始你画我猜游戏"),
                )
                await consume()
                await consume()
                assert agent.active.game_id == "guess_number"
                await agent.receive("结束游戏。")
                assert agent.active.closing
                await agent.receive("开始你画我猜游戏")
                ended = await consume()
                assert not ended.result.state.active and agent.active is None
                await consume()
                assert "结束" in transcript[-1]
                # 退出完成后另一个游戏才能启动，生成期间可以结束游戏。
                await agent.receive("开始你画我猜。")
                assert agent.active.game_id == "guess_animal"
                await agent.receive("结束游戏。")
                stale = await consume()
                assert stale.result.state.active
                await consume()
                assert agent.active is None
                await agent.receive("我们刚才聊过哪种鸟？")
                await consume()
                assert "企鹅" in transcript[-1]
            finally:
                await agent.close()
                (run_directory / "dialogue.json").write_text(
                    json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8",
                )
        assert all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
                   for name, digest in hashes.items())
    asyncio.run(exercise())


def test_exit_cancels_real_generation_and_rejects_late_input():
    async def exercise():
        async with connect_games(ROOT / "games.json") as games:
            agent = MainAgent(Settings.from_environment(), games)
            try:
                await agent.receive("开始你画我猜游戏")
                start = await agent.outputs.get()
                await agent.output_done(start)
                original = agent.active.context_id
                revision = agent.active.state.revision
                async with asyncio.timeout(10):
                    while True:
                        tasks = await games["guess_animal"].client.list_tasks(
                            ListTasksRequest(context_id=original),
                        )
                        if any(task.status.state == TaskState.TASK_STATE_WORKING
                               for task in tasks.tasks):
                            break
                        if all(task.done() for task in agent.pending):
                            await agent.tick()
                            raise AssertionError("题目生成没有进入执行状态")
                        await asyncio.sleep(0.05)
                await asyncio.wait_for(agent.receive_utterance(
                    "现在题目准备好了吗？", original, revision,
                ), timeout=90)
                progress = await agent.outputs.get()
                await agent.output_done(progress)
                assert progress.result.state.phase == "生成题目"
                await agent.receive_utterance("结束游戏。", original, revision)
                ended = await agent.outputs.get()
                await agent.output_done(ended)
                assert agent.active is None
                await asyncio.gather(*agent.pending)
                await agent.tick()
                assert not agent.pending
                tasks = await games["guess_animal"].client.list_tasks(
                    ListTasksRequest(context_id=original),
                )
                assert len(tasks.tasks) == 3
                assert {task.status.state for task in tasks.tasks} == {
                    TaskState.TASK_STATE_CANCELED, TaskState.TASK_STATE_COMPLETED,
                }
                await agent.receive("开始猜数字游戏")
                number = await agent.outputs.get()
                await agent.output_done(number)
                current_revision = agent.active.state.revision
                await agent.receive_utterance("我猜企鹅", original, revision)
                rejected = await agent.outputs.get()
                assert "刚才那局已经结束" in rejected.outputs[0].text
                await agent.output_done(rejected)
                assert agent.active.state.revision == current_revision
            finally:
                await agent.close()
    asyncio.run(exercise())


def test_complete_five_rounds_over_a2a_with_real_generation(run_directory):
    async def exercise():
        transcript = []
        async with connect_games(ROOT / "games.json") as games:
            connection = games["guess_animal"]
            context_id = run_directory.name
            result = await connection.send(context_id, GameRequest(action="start"))

            async def send(action, text=""):
                nonlocal result
                updates = []

                async def record(update):
                    updates.append(update)

                result = await connection.send(context_id, GameRequest(
                    action=action, text=text, revision=result.state.revision,
                ), on_update=record)
                transcript.extend(update.model_dump(mode="json") for update in updates)
                return "".join(output.text for update in updates for output in update.outputs)

            try:
                for number in range(1, 6):
                    assert result.state.phase == "等待猜测"
                    drawing = result.outputs[0].paths[-1]
                    animals = (
                        item.animal()
                        for path in sorted((ROOT / ".artifacts/generated").glob("*.json"),
                                           reverse=True)
                        for item in GeneratedRound.model_validate_json(path.read_text()).animals
                    )
                    animal = next(item for item in animals if encode_vector(item.strokes) == drawing)
                    wrong = next(name for name in ("企鹅", "乌龟", "长颈鹿")
                                 if name not in (animal.name, *animal.aliases))
                    if number == 1:
                        assert animal.hints[0].rstrip("。") in await send("input", "给我提示")
                        assert "再想想，给你个提示" in await send("input", f"我猜是{wrong}")
                        assert animal.hints[2].rstrip("。") in await send("input", "再给一个提示")
                        assert "提示都给你啦" in await send("input", "再给一个提示")
                    if number == 2:
                        await send("input", f"我猜是{wrong}")
                        assert f"答案是{animal.name}" in await send("input", f"我猜是{wrong}")
                    elif number == 3:
                        assert f"答案是{animal.name}" in await send("input", "下一题")
                    else:
                        assert f"答对了，是{animal.name}" in await send("input", f"我猜是{animal.name}")
                    if number < 5 and number != 3:
                        assert result.state.phase == "等待下一题"
                        await send("input", "下一题")
                assert not result.state.active
                assert "5题都猜完啦！你答对了3题" in "".join(output.text for output in result.outputs)
                tasks = await connection.client.list_tasks(ListTasksRequest(context_id=context_id))
                assert len(tasks.tasks) == 14
                assert all(task.status.state == TaskState.TASK_STATE_COMPLETED
                           for task in tasks.tasks)
            finally:
                (run_directory / "rounds.json").write_text(
                    json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8",
                )
    asyncio.run(exercise())


def test_real_voice_enters_main_agent_and_registered_game(run_directory):
    async def exercise():
        from robot_agents.speech import QwenSpeech
        from tests.e2e.audio import asr_frames, write_wav

        settings = Settings.from_environment()
        speech = QwenSpeech(settings)
        recognized = []
        async with connect_games(ROOT / "games.json") as games:
            agent = MainAgent(settings, games)
            try:
                cases = (
                    ("有哪些游戏？", "list_games"), ("开始猜数字游戏。", "start_game"),
                    ("给我一个提示。", "send_message"),
                    ("我猜的数字是零。", "send_message"),
                    ("结束游戏。", "end_game"),
                )
                for index, (text, action) in enumerate(cases):
                    pcm = await speech.synthesize(text)
                    write_wav(run_directory / f"speech-{index}.wav", pcm)

                    async def audio(recording=pcm):
                        for frame in asr_frames(recording):
                            yield frame

                    utterance = await speech.recognize(audio())
                    assert utterance
                    await agent.receive(utterance)
                    assert action in [call["name"] for call in agent.last_tool_calls]
                    recognized.append({"tool": action, "utterance": utterance})
                    output = await agent.outputs.get()
                    await agent.output_done(output)
                    if index in {1, 2, 3}:
                        assert agent.active.game_id == "guess_number"
                assert agent.active is None
            finally:
                await agent.close()
                (run_directory / "recognized.json").write_text(
                    json.dumps(recognized, ensure_ascii=False), encoding="utf-8",
                )
    asyncio.run(exercise())
