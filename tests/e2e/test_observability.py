import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

import observability
from robot_agents.audio_stream import CapturedPcm
from robot_agents.config import Settings
from robot_agents.main_agent import MainAgent
from robot_agents.speech import QwenSpeech
from robot_agents.speech_input import SpeechInput
from robot_agents.transport import connect_games
from tests.e2e.audio import asr_frames

pytestmark = pytest.mark.e2e


class Microphone:
    async def pause(self, *, owner):
        pass

    def resume(self, *, owner):
        pass


def test_real_speech_turns_record_tools_and_a2a_exchange(run_directory):
    since = observability.bus.seq

    async def exercise():
        # 合成音频音量充足，输入增益使用 1，避免放大后削波。
        settings = replace(Settings.from_environment(), input_gain=1.0)
        speech = QwenSpeech(settings)
        recordings = [await speech.synthesize(text) for text in ("开始猜数字游戏。", "结束游戏。")]
        async with asyncio.timeout(120), connect_games(settings.game_catalog) as games:
            agent = MainAgent(settings, games)
            listener = SpeechInput(settings, agent, Microphone(), SimpleNamespace(
                info=lambda *arguments: None,
            ))

            async def speak():
                while True:
                    request = await agent.outputs.get()
                    await agent.output_done(request)
                    agent.outputs.task_done()

            async def source():
                for pcm in recordings:
                    heard = len(listener.events)
                    for frame in [*asr_frames(pcm), *([bytes(640)] * 100)]:
                        yield CapturedPcm(frame, time.monotonic(), False)
                        await asyncio.sleep(0.02)
                    # 上一句处理完成后才送入下一句，处理期间的音频会被静音。
                    while len(listener.events) == heard or listener.busy:
                        yield CapturedPcm(bytes(640), time.monotonic(), False)
                        await asyncio.sleep(0.02)

            speaker = asyncio.create_task(speak())
            try:
                await listener.run(source(), asyncio.Event())
                await agent.outputs.join()
            finally:
                await agent.close()
                speaker.cancel()

    asyncio.run(exercise())
    events = [json.loads(event.data) for event in observability.bus.after(since)]
    (run_directory / "events.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    started, ended = [event for event in events if event["type"] == "user.input"]
    assert "猜数字" in started["payload"]["text"] and "结束" in ended["payload"]["text"]
    for user, action, tool in ((started, "start", "start_game"), (ended, "end", "end_game")):
        assert 0.5 < user["time"] - user["payload"]["spoken_at"] < 15
        turn = [event for event in events if event["turn_id"] == user["turn_id"]]
        main_call, = [event for event in turn
                      if event["type"] == "tool.call" and event["actor"] == "main"]
        request, = [event for event in turn if event["type"] == "a2a.request"]
        result, = [event for event in turn if event["type"] == "a2a.result"]
        game_calls = [event for event in turn
                      if event["type"] == "tool.call" and event["actor"] == "guess_number"]
        assert main_call["payload"]["tool"] == tool
        assert request["parent_id"] == result["parent_id"] == main_call["payload"]["call_id"]
        assert request["payload"]["request"]["action"] == action
        assert [event["payload"]["tool"] for event in game_calls] == [tool, "send_message"]
        assert {event["parent_id"] for event in game_calls} == {request["payload"]["request_id"]}
        assert result["payload"]["ok"] and result["payload"]["result"]["outputs"][0]["text"]
    assert all(event["turn_id"] for event in events)
