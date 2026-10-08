import asyncio
import json
import time

import pytest

from robot_agents.audio_stream import CapturedPcm
from robot_agents.config import ROOT, Settings
from robot_agents.main_agent import MainAgent
from robot_agents.speech import QwenSpeech
from robot_agents.transport import connect_games
from tests.e2e.audio import asr_frames, write_wav

pytestmark = pytest.mark.e2e


async def audio_frames(pcm):
    for frame in asr_frames(pcm):
        yield frame


def test_audio_chat_returns_speech_without_game_request(run_directory):
    async def exercise():
        settings = Settings.from_environment()
        speech = QwenSpeech(settings)
        pcm = await speech.synthesize("企鹅为什么不会飞？")
        utterance = await speech.recognize(audio_frames(pcm))
        assert utterance and "企鹅" in utterance
        async with connect_games(ROOT / "games.json") as games:
            agent = MainAgent(settings, games)
            try:
                await agent.receive(utterance)
                output = await agent.outputs.get()
                assert output.result is None and agent.active is None
                text = output.outputs[0].text
                assert "企鹅" in text
                write_wav(run_directory / "chat.wav", await speech.synthesize(text))
                (run_directory / "chat.txt").write_text(text)
                await agent.output_done(output)
            finally:
                await agent.close()
    asyncio.run(exercise())


def test_game_announcements_preserve_numbers(run_directory):
    async def exercise():
        speech = QwenSpeech(Settings.from_environment())
        records = []
        for index, (text, expected) in enumerate((
            ("猜大了，还有四次机会。", ("四", "4")),
            ("答案是十二。", ("十二", "12")),
            ("这局五题里，你答对了三题。", ("三", "3")),
        )):
            pcm = await speech.synthesize(text)
            write_wav(run_directory / f"announcement-{index}.wav", pcm)
            recognized = await speech.recognize(audio_frames(pcm))
            records.append({"text": text, "recognized": recognized})
            assert recognized and any(word in recognized for word in expected), records[-1]
        (run_directory / "announcements.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    asyncio.run(exercise())


def test_continuous_asr_recognizes_multiple_utterances(run_directory):
    async def exercise():
        speech = QwenSpeech(Settings.from_environment())
        recordings = [await speech.synthesize(text) for text in (
            "开始猜数字游戏。", "结束游戏。",
        )]
        results = []
        async def source():
            for pcm in recordings:
                for frame in [*asr_frames(pcm), *([bytes(640)] * 75)]:
                    yield CapturedPcm(frame, time.monotonic(), False)
                    await asyncio.sleep(0.02)

        activities = []
        async with asyncio.timeout(60):
            await speech.transcribe(
                source(), results.append, on_activity=activities.append, ready=asyncio.Event(),
            )
        assert len(results) == 2, results
        assert "数字" in results[0].text and "结束" in results[1].text
        assert results[0].started_at < results[1].started_at
        assert True in activities and activities[-1] is False
        (run_directory / "recognized.json").write_text(json.dumps(
            [{"text": item.text, "started_at": item.started_at} for item in results],
            ensure_ascii=False, indent=2,
        ))
    asyncio.run(exercise())


def test_audio_game_results_come_from_registered_agent(run_directory):
    async def exercise():
        settings = Settings.from_environment()
        cases = (
            ("开始猜数字游戏。", "start"),
            ("给我一个提示。", "send"),
            ("再给我一个提示。", "send"),
            ("我猜的数字是零。", "send"),
            ("企鹅为什么不会飞？", "chat"),
            ("继续游戏。", "status"),
            ("换成你画我猜。", "switch"),
            ("结束游戏。", "end"),
            ("开始猜数字游戏。", "start"),
            ("结束游戏。", "end"),
        )
        speech = QwenSpeech(settings)
        transcript = []
        async with connect_games(ROOT / "games.json") as games:
            agent = MainAgent(settings, games)
            try:
                for text, action in cases:
                    previous = agent.status()
                    pcm = await speech.synthesize(text)
                    utterance = await speech.recognize(audio_frames(pcm))
                    assert utterance
                    transcript.append({"user": text, "recognized": utterance})
                    completion = await agent.receive(utterance)
                    output = await agent.outputs.get()
                    if action in {"start", "send", "status", "end"}:
                        assert output.result is not None
                    expected = [item.text for item in output.outputs if item.text]
                    await agent.output_done(output)
                    messages = await completion
                    assert messages == expected
                    if action == "end":
                        assert agent.active is None and "已经结束" in "".join(messages)
                    elif action == "chat":
                        assert agent.status() == previous and "企鹅" in "".join(messages)
                        assert output.result is None
                        assert not any(word in "".join(messages) for word in (
                            "下一题", "继续猜", "继续游戏", "答对了", "猜大了", "猜小了",
                        ))
                    elif action == "status":
                        assert agent.status() == previous and "还有5次机会" in "".join(messages)
                    elif action == "switch":
                        assert agent.active.game_id == "guess_number"
                        assert "先说结束游戏就好" in "".join(messages)
                    transcript[-1]["game_messages"] = messages
                    for index, message in enumerate(messages):
                        write_wav(run_directory / f"reply-{len(transcript)}-{index}.wav",
                                  await speech.synthesize(message))
            finally:
                await agent.close()
                (run_directory / "dialogue.json").write_text(
                    json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8",
                )
    asyncio.run(exercise())
