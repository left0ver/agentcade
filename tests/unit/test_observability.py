import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

import observability
from observability.events import EventBus
from observability.server import serve
from robot_agents.config import LLMSettings, Settings
from robot_agents.main_agent import MainAgent
from robot_agents.protocol import (
    ExpectedInput,
    GameDescriptor,
    GameRequest,
    GameResult,
    Presentation,
    PublicState,
)
from robot_agents.speech import RecognizedSpeech
from robot_agents.speech_input import SpeechInput
from robot_agents.tools import NoInput, Tool, ToolRegistry
from robot_agents.transport import connect_games


class GameAgent:
    descriptor = GameDescriptor(id="observed_game", name="观测游戏", description="检查事件关联。")
    load_settings = staticmethod(lambda: SimpleNamespace(llm=SimpleNamespace(timeout_seconds=5)))

    def __init__(self, settings):
        self.settings = settings

    async def handle(self, request, notify):
        async def prepare():
            return {"prepared": True}

        await ToolRegistry([Tool("prepare", "准备题目。", NoInput, prepare)]).call("prepare", {})
        await notify(GameResult(
            state=PublicState(phase="准备中"),
            expected_input=ExpectedInput(kind="wait", description="请等待。"),
        ))
        return GameResult(
            state=PublicState(phase="已结束", active=False),
            expected_input=ExpectedInput(kind="none", description="本局已经结束。"),
            outputs=[Presentation(text="准备好了。")],
        )

    async def close(self):
        pass


class SlowGame(GameAgent):
    descriptor = GameDescriptor(id="slow_game", name="慢速游戏", description="出题需要等待。")
    release: asyncio.Event

    def result(self, phase, kind, text, *, active=True):
        return GameResult(
            state=PublicState(phase=phase, active=active),
            expected_input=ExpectedInput(kind=kind, description="按提示继续。"),
            outputs=[Presentation(text=text)],
        )

    async def handle(self, request, notify):
        async def start_game():
            await notify(self.result("生成题目", "wait", "正在出题。"))
            await self.release.wait()
            return {"prepared": True}

        async def get_game_state():
            return {"phase": "生成题目"}

        async def end_game():
            return {"ended": True}

        name = {"start": "start_game", "input": "get_game_state", "end": "end_game"}[request.action]
        await ToolRegistry([
            Tool("start_game", "出题。", NoInput, start_game),
            Tool("get_game_state", "查询进度。", NoInput, get_game_state),
            Tool("end_game", "结束。", NoInput, end_game),
        ]).call(name, {})
        if request.action == "start":
            return self.result("等待回答", "answer", "题目出好了。")
        if request.action == "input":
            return self.result("生成题目", "wait", "还在出题。")
        return self.result("已结束", "none", "游戏结束。", active=False)


class ScriptedProvider:
    async def run_tools(self, system, request, registry):
        text = request["current_utterance"]
        if text == "开始游戏":
            await registry.call("start_game", {"game_id": "slow_game"})
        elif text == "好了吗":
            await registry.call("send_message", {"text": text})
        elif text == "结束游戏":
            await registry.call("end_game", {})
        else:
            return "你好呀。"
        return ""

    async def close(self):
        pass


class ScriptedSpeech:
    def __init__(self):
        self.utterances = asyncio.Queue()

    async def transcribe(self, audio, on_result, *, on_activity, ready, hotwords=()):
        ready.set()
        while (text := await self.utterances.get()) is not None:
            on_result(RecognizedSpeech(text, time.monotonic()))


class SilentMicrophone:
    async def pause(self, *, owner):
        pass

    def resume(self, *, owner):
        pass

    async def audio(self):
        return
        yield


def recorded(since):
    return [json.loads(event.data) for event in observability.bus.after(since)]


def test_turn_links_main_tool_a2a_exchange_and_game_tool(run_directory):
    catalog = run_directory / "games.json"
    catalog.write_text(json.dumps([
        {"kind": "local", "id": "observed_game", "factory": f"{__name__}:GameAgent"},
    ]), encoding="utf-8")
    since = observability.bus.seq

    async def scenario():
        async with connect_games(catalog) as games:
            async def start_game():
                result = await games["observed_game"].send("context", GameRequest(action="start"))
                return {"delivered": True, "state": result.state.model_dump()}

            turn_id = observability.user_input("开始游戏", time.monotonic() - 1)
            with observability.turn(turn_id):
                await ToolRegistry([Tool("start_game", "开始游戏。", NoInput, start_game)]).call(
                    "start_game", {},
                )
            return turn_id

    turn_id = asyncio.run(scenario())
    events = recorded(since)
    assert [(event["type"], event["actor"]) for event in events] == [
        ("user.input", "user"), ("tool.call", "main"), ("a2a.request", "main"),
        ("tool.call", "observed_game"), ("tool.result", "observed_game"),
        ("a2a.update", "main"), ("a2a.result", "main"), ("tool.result", "main"),
    ]
    assert {event["turn_id"] for event in events} == {turn_id}
    user, main_call, request, game_call, game_result, update, result, main_result = events
    assert user["payload"]["text"] == "开始游戏"
    assert user["time"] - user["payload"]["spoken_at"] == pytest.approx(1, abs=0.2)
    assert main_call["parent_id"] is None and main_result["parent_id"] is None
    call_id, request_id = main_call["payload"]["call_id"], request["payload"]["request_id"]
    assert request["parent_id"] == update["parent_id"] == result["parent_id"] == call_id
    assert request["payload"]["request"]["action"] == "start"
    assert request["payload"]["game_name"] == "观测游戏"
    assert game_call["parent_id"] == game_result["parent_id"] == request_id
    assert game_result["payload"]["result"] == {"prepared": True}
    assert update["payload"]["result"]["state"]["phase"] == "准备中"
    assert result["payload"]["ok"] is True
    assert result["payload"]["result"]["outputs"][0]["text"] == "准备好了。"
    assert main_result["payload"]["result"]["state"]["phase"] == "已结束"


def test_speech_turns_keep_late_game_results_with_the_turn_that_started_them(run_directory):
    catalog = run_directory / "games.json"
    catalog.write_text(json.dumps([
        {"kind": "local", "id": "slow_game", "factory": f"{__name__}:SlowGame"},
    ]), encoding="utf-8")
    settings = Settings(dashscope_api_key="observability", llm=LLMSettings(
        api_key="observability", base_url="https://api.deepseek.com", model_name="scripted",
    ))
    since = observability.bus.seq

    async def scenario():
        SlowGame.release = asyncio.Event()
        async with asyncio.timeout(20), connect_games(catalog) as games:
            agent = MainAgent(settings, games)
            await agent.provider.close()
            agent.provider = ScriptedProvider()
            listener = SpeechInput(settings, agent, SilentMicrophone(), SimpleNamespace(
                info=lambda *arguments: None,
            ))
            listener.speech = ScriptedSpeech()

            async def speak_outputs():
                while True:
                    request = await agent.outputs.get()
                    await agent.output_done(request)
                    agent.outputs.task_done()

            async def say(text):
                heard = len(listener.events)
                await listener.speech.utterances.put(text)
                while len(listener.events) == heard or listener.busy:
                    await asyncio.sleep(0.01)

            ready = asyncio.Event()
            async with asyncio.TaskGroup() as group:
                speaker = group.create_task(speak_outputs())
                listening = group.create_task(listener.run(SilentMicrophone().audio(), ready))
                await say("开始游戏")
                await say("好了吗")
                SlowGame.release.set()
                await asyncio.gather(*agent.pending)
                await agent.tick()
                await say("你好")
                await listener.speech.utterances.put(None)
                await listening
                await agent.outputs.join()
                speaker.cancel()
            await agent.close()

    asyncio.run(scenario())
    events = recorded(since)
    turns = [event for event in events if event["type"] == "user.input"]
    assert [event["payload"]["text"] for event in turns] == ["开始游戏", "好了吗", "你好"]
    first, second, chat = (
        [(item["type"], item["actor"], item["payload"].get("tool")) for item in events
         if item["turn_id"] == event["turn_id"]]
        for event in turns
    )
    assert first == [
        ("user.input", "user", None), ("tool.call", "main", "start_game"),
        ("a2a.request", "main", None), ("tool.call", "slow_game", "start_game"),
        ("a2a.update", "main", None), ("tool.result", "main", "start_game"),
        ("tool.result", "slow_game", "start_game"), ("a2a.result", "main", None),
    ]
    assert second == [
        ("user.input", "user", None), ("tool.call", "main", "send_message"),
        ("a2a.request", "main", None), ("tool.call", "slow_game", "get_game_state"),
        ("tool.result", "slow_game", "get_game_state"), ("a2a.result", "main", None),
        ("tool.result", "main", "send_message"),
    ]
    assert chat == [("user.input", "user", None), ("agent.reply", "main", None)]
    assert events[[event["type"] for event in events].index("agent.reply")]["payload"] == {
        "text": "你好呀。",
    }

    late = next(event for event in events
                if event["type"] == "a2a.result" and event["turn_id"] == turns[0]["turn_id"])
    start_call = next(event for event in events if event["payload"].get("tool") == "start_game")
    assert late["seq"] > turns[1]["seq"]
    assert late["parent_id"] == start_call["payload"]["call_id"]
    assert late["payload"]["result"]["outputs"][0]["text"] == "题目出好了。"

    closing = [event for event in events if event["turn_id"] is None]
    assert [(event["type"], event["actor"]) for event in closing] == [
        ("a2a.request", "main"), ("tool.call", "slow_game"), ("tool.result", "slow_game"),
        ("a2a.result", "main"),
    ]
    assert closing[0]["payload"]["request"]["action"] == "end"
    assert closing[0]["parent_id"] is None


def test_ending_the_game_marks_unfinished_requests_as_cancelled(run_directory):
    catalog = run_directory / "games.json"
    catalog.write_text(json.dumps([
        {"kind": "local", "id": "slow_game", "factory": f"{__name__}:SlowGame"},
    ]), encoding="utf-8")
    settings = Settings(dashscope_api_key="observability", llm=LLMSettings(
        api_key="observability", base_url="https://api.deepseek.com", model_name="scripted",
    ))
    since = observability.bus.seq

    async def scenario():
        SlowGame.release = asyncio.Event()
        async with asyncio.timeout(20), connect_games(catalog) as games:
            agent = MainAgent(settings, games)
            await agent.provider.close()
            agent.provider = ScriptedProvider()

            async def speak_outputs():
                while True:
                    request = await agent.outputs.get()
                    await agent.output_done(request)
                    agent.outputs.task_done()

            speaker = asyncio.create_task(speak_outputs())
            try:
                for text in ("开始游戏", "结束游戏"):
                    with observability.turn(observability.user_input(text, time.monotonic())):
                        await (await agent.receive(text))
                assert agent.active is None
            finally:
                await agent.close()
                speaker.cancel()

    asyncio.run(scenario())
    events = recorded(since)
    started, ended = [event for event in events if event["type"] == "user.input"]
    first = [event for event in events if event["turn_id"] == started["turn_id"]]
    cancelled = [(event["type"], event["actor"]) for event in first
                 if event["payload"].get("cancelled")]
    assert sorted(cancelled) == [("a2a.result", "main"), ("tool.result", "slow_game")]
    assert all(event["payload"]["ok"] is False and event["payload"]["error"] == "已取消"
               for event in first if event["payload"].get("cancelled"))
    second = [(event["type"], event["actor"], event["payload"].get("ok")) for event in events
              if event["turn_id"] == ended["turn_id"]]
    assert second == [
        ("user.input", "user", None), ("tool.call", "main", None), ("a2a.request", "main", None),
        ("tool.call", "slow_game", None), ("tool.result", "slow_game", True),
        ("a2a.result", "main", True), ("tool.result", "main", True),
    ]


def test_failed_tool_is_recorded_and_still_raises():
    since = observability.bus.seq

    async def broken():
        raise RuntimeError("设备没有响应")

    with pytest.raises(RuntimeError, match="设备没有响应"):
        asyncio.run(ToolRegistry([Tool("broken", "失败。", NoInput, broken)]).call("broken", {}))
    call, result = recorded(since)
    assert call["payload"]["call_id"] == result["payload"]["call_id"]
    assert result["payload"]["ok"] is False
    assert result["payload"]["error"] == "RuntimeError: 设备没有响应"
    assert "result" not in result["payload"]


def test_unserializable_payload_does_not_interrupt_the_caller():
    events = EventBus()
    events.emit("tool.result", {"call_id": "call", "ok": True, "result": float("nan")})
    payload = json.loads(events.events[-1].data)["payload"]
    assert "无法序列化" in payload.pop("error")
    assert payload == {"call_id": "call", "ok": True}


def test_buffer_keeps_only_recent_events():
    events = EventBus(capacity=3)
    for index in range(5):
        events.emit("user.input", {"text": str(index)})
    assert [event.seq for event in events.after(0)] == [3, 4, 5]


def test_occupied_port_reports_the_setting_to_change():
    async def scenario():
        async with serve(0, EventBus()) as url, serve(int(url.rsplit(":", 1)[1]), EventBus()):
            pass

    with pytest.raises(RuntimeError, match="OBSERVABILITY_PORT"):
        asyncio.run(scenario())


async def read(http, url, headers, count, after=""):
    records, identifier = [], None
    async with http.stream(
        "GET", url + "/events", headers=headers, params={"after": after},
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                identifier = line[4:]
            elif line.startswith("data: ") and identifier:
                records.append((identifier, json.loads(line[6:])["payload"]["text"]))
                identifier = None
            if len(records) == count:
                break
    return records


def test_page_stream_replays_resumes_and_closes_with_the_service():
    async def scenario():
        events = EventBus()
        events.emit("user.input", {"text": "第一句"})
        async with asyncio.timeout(10), httpx.AsyncClient(trust_env=False) as http:
            async with serve(0, events) as url:
                page = await http.get(url)
                foreign = await http.get(url, headers={"Host": "attacker.example"})
                first = await read(http, url, {}, 1)
                events.emit("user.input", {"text": "第二句"})
                resumed = await read(http, url, {"Last-Event-ID": first[0][0]}, 1)
                reconnected = await read(http, url, {}, 1, after=first[0][0])
                restarted = await read(http, url, {}, 2, after="previous-run:9")
                watching = await http.send(http.build_request("GET", url + "/events"), stream=True)
            # 服务关闭时，仍然打开的事件流随之结束。
            await watching.aread()
            await watching.aclose()
        return events, page, foreign, first, resumed, reconnected, restarted

    events, page, foreign, first, resumed, reconnected, restarted = asyncio.run(scenario())
    assert page.status_code == 200 and "<title>轨迹" in page.text
    assert foreign.status_code == 400
    assert first == [(f"{events.run_id}:1", "第一句")]
    assert resumed == reconnected == [(f"{events.run_id}:2", "第二句")]
    assert [text for _, text in restarted] == ["第一句", "第二句"]
