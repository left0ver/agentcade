"""真机端到端测试：电脑扬声器替用户说话，机器人收音并回应，脚本核对轨迹事件。

    uv run python scripts/e2e_robot.py

机器人需要已经连接并放在电脑扬声器附近，测试期间保持安静，全程大约五分钟。
测试会调用真实的语音和模型服务。录音、应用日志、轨迹和报告保存在 .artifacts/e2e/<时间>/。
"""
import asyncio
import json
import os
import sys
import time
import wave
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from robot_agents.config import Settings
from robot_agents.speech import QwenSpeech

# 机器人播报用的合成音量较低，经电脑扬声器播放时放大到机器人能够听清。
SPEAKER_GAIN = "6"
QUIET_SECONDS = 1.2
MICROPHONE_REOPEN_SECONDS = 1.0


class Turn:
    """一轮对话里的全部轨迹事件。"""

    def __init__(self, events):
        self.events = events

    def select(self, kind):
        return [event for event in self.events if event["type"] == kind]

    @property
    def heard(self):
        return self.select("user.input")[0]["payload"]["text"]

    def tools(self, actor=None):
        results = {event["payload"]["call_id"]: event["payload"]
                   for event in self.select("tool.result")}
        return [
            {"actor": event["actor"], **event["payload"],
             **results.get(event["payload"]["call_id"], {})}
            for event in self.select("tool.call") if actor in (None, event["actor"])
        ]

    def tool(self, actor, name):
        return next((call for call in self.tools(actor) if call["tool"] == name), None)

    @property
    def requests(self):
        results = {event["payload"]["request_id"]: event["payload"]
                   for event in self.select("a2a.result")}
        return [
            {**event["payload"], **results.get(event["payload"]["request_id"], {}),
             "updates": [update["payload"]["result"]["text"]
                         for update in self.select("a2a.update")
                         if update["payload"]["request_id"] == event["payload"]["request_id"]]}
            for event in self.select("a2a.request")
        ]

    @property
    def outputs(self):
        return [{"actor": event["actor"], **event["payload"]}
                for event in self.select("device.output")]

    @property
    def spoken(self):
        return [item["output"]["text"] for item in self.outputs
                if item["output"].get("text") and item.get("completed")]

    @property
    def latency(self):
        """用户说完到第一条回复生成的秒数。"""
        ended = self.select("user.input")[0]["payload"].get("speech_ended_at")
        replies = self.select("agent.reply")
        return replies[0]["time"] - ended if ended and replies else None

    def settled(self):
        calls = {event["payload"]["call_id"] for event in self.select("tool.call")}
        if calls - {event["payload"]["call_id"] for event in self.select("tool.result")}:
            return False
        requests = {event["payload"]["request_id"] for event in self.select("a2a.request")}
        if requests - {event["payload"]["request_id"] for event in self.select("a2a.result")}:
            return False
        expected = Counter(event["payload"]["text"] for event in self.select("agent.reply"))
        played = Counter(item["output"]["text"] for item in self.outputs
                         if item["output"].get("text"))
        if expected - played or not played:
            return False
        return time.time() - self.events[-1]["time"] >= QUIET_SECONDS


class Trace:
    def __init__(self):
        self.events = []
        self.connected = False

    async def follow(self, url):
        async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
            while True:
                try:
                    async with client.stream("GET", f"{url}/events") as response:
                        self.connected = True
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                record = json.loads(line[6:])
                                if "type" in record:
                                    self.events.append(record)
                    return
                except httpx.TransportError:
                    # 应用启动之前连接不上，稍后再试；连上之后断开说明应用已经退出。
                    if self.connected:
                        return
                    await asyncio.sleep(0.3)

    def turn(self, turn_id):
        return Turn([event for event in self.events if event["turn_id"] == turn_id])


@dataclass
class Step:
    name: str
    say: str
    check: object
    timeout: float = 60


def game_request(turn, kind, game_id):
    request = next((item for item in turn.requests
                    if item["request"]["kind"] == kind and item["game_id"] == game_id), None)
    if request is None:
        return None, [f"没有向 {game_id} 发送 {kind} 请求"]
    if not request.get("ok"):
        return request, [f"{kind} 请求失败：{request.get('error')}"]
    problems = []
    if request["result"]["text"] not in turn.spoken:
        problems.append("最后一条回复没有播报")
    return request, problems


def check_catalog(turn):
    problems = [] if not turn.requests else ["不应该向游戏发送请求"]
    if not any("猜数字" in text or "你画我猜" in text for text in turn.spoken):
        problems.append("回复里没有提到可玩的游戏")
    return problems


def check_number_start(turn):
    request, problems = game_request(turn, "start", "guess_number")
    if request and request.get("ok") and request["result"]["session"] != "active":
        problems.append("开局后会话应当继续")
    if not turn.tool("guess_number", "start_game"):
        problems.append("游戏没有调用 start_game")
    return problems


def check_guess(turn):
    _, problems = game_request(turn, "message", "guess_number")
    call = turn.tool("guess_number", "submit_guess")
    if not call:
        problems.append("游戏没有调用 submit_guess")
    elif call["arguments"].get("number") != 15:
        problems.append(f"提交的数字是 {call['arguments'].get('number')}，应当是 15")
    return problems


def check_hint(game_id):
    def check(turn):
        _, problems = game_request(turn, "message", game_id)
        call = turn.tool(game_id, "get_hint")
        if not call or not call.get("ok"):
            problems.append("游戏没有成功调用 get_hint")
        return problems
    return check


def check_chat(turn):
    problems = [] if not turn.requests else ["闲聊不应该转给游戏"]
    if not turn.spoken:
        problems.append("主 Agent 没有回复")
    return problems


def check_end(game_id):
    def check(turn):
        request, problems = game_request(turn, "end", game_id)
        if request and request.get("ok") and request["result"]["session"] != "ended":
            problems.append("结束后会话应当是 ended")
        if not turn.tool(game_id, "end_game"):
            problems.append("游戏没有调用 end_game")
        return problems
    return check


def check_animal_start(turn):
    request, problems = game_request(turn, "start", "guess_animal")
    if request and not request["updates"]:
        problems.append("画图之前没有先回复一句")
    elif request and not set(request["updates"]) <= set(turn.spoken):
        problems.append("中间回复没有播报")
    outputs = [item for item in turn.outputs if item["actor"] == "guess_animal"]
    if not any(item["output"].get("progress_seconds") and item.get("completed") for item in outputs):
        problems.append("没有显示进度")
    if not any(item["output"].get("paths") and item.get("completed") for item in outputs):
        problems.append("没有在屏幕上画出题目")
    return problems


def check_answer(turn):
    _, problems = game_request(turn, "message", "guess_animal")
    call = turn.tool("guess_animal", "submit_answer")
    if not call or not call.get("ok"):
        problems.append("游戏没有成功调用 submit_answer")
    elif call["arguments"].get("text") != turn.heard:
        problems.append("提交的答案不是用户原话")
    elif call["result"].get("outcome") not in {"correct", "incorrect", "unclear"}:
        problems.append(f"判定结果异常：{call['result'].get('outcome')}")
    return problems


STEPS = [
    Step("查询游戏", "有哪些游戏可以玩？", check_catalog),
    Step("开始猜数字", "我想玩猜数字。", check_number_start),
    Step("猜一个数", "我猜十五。", check_guess),
    Step("猜数字要提示", "给我一个提示吧。", check_hint("guess_number")),
    Step("游戏中闲聊", "对了，你叫什么名字？", check_chat),
    Step("结束猜数字", "结束游戏。", check_end("guess_number")),
    Step("开始你画我猜", "我们来玩你画我猜。", check_animal_start, timeout=420),
    Step("你画我猜要提示", "给我一个提示吧。", check_hint("guess_animal")),
    Step("回答动物", "我猜是恐龙。", check_answer),
    Step("结束你画我猜", "结束游戏。", check_end("guess_animal")),
]


async def wait_for(condition, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        await asyncio.sleep(0.1)
    raise TimeoutError(f"等待{what}超过 {timeout:.0f} 秒")


async def run_command(*command):
    process = await asyncio.create_subprocess_exec(
        *command, cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode, output.decode(errors="replace")


async def main():
    directory = ROOT / ".artifacts" / "e2e" / time.strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True)
    settings = Settings.from_environment()
    speech = QwenSpeech(settings)
    recordings = {}
    for text in dict.fromkeys(step.say for step in STEPS):
        path = directory / f"say-{len(recordings) + 1:02d}.wav"
        with wave.open(str(path), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(24000)
            recording.writeframes(await speech.synthesize(text))
        recordings[text] = path
    print(f"已合成 {len(recordings)} 段用户语音，结果目录：{directory}")

    log_lines = []
    app = await asyncio.create_subprocess_exec(
        "uv", "run", "watcherobot", "app", "run", ".", cwd=ROOT,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        # 命令行工具的输出经过管道时会缓冲，关闭缓冲才能实时拿到应用日志。
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    async def pump():
        async for raw in app.stdout:
            log_lines.append(raw.decode(errors="replace").rstrip("\n"))

    pumping = asyncio.create_task(pump())
    trace = Trace()
    results = []
    following = asyncio.create_task(
        trace.follow(f"http://127.0.0.1:{settings.observability_port}"),
    )
    try:
        await wait_for(lambda: trace.connected or app.returncode is not None, 60, "应用启动")
        if not trace.connected:
            raise RuntimeError("应用没有启动：" + " | ".join(log_lines[-5:]))
        await wait_for(
            lambda: any(event["type"] == "device.output" and event["payload"].get("completed")
                        and event["payload"]["output"].get("text", "").startswith("你好呀")
                        for event in trace.events),
            60, "开机问候播报完成",
        )
        print("机器人已经说完问候，开始对话。")
        await asyncio.sleep(QUIET_SECONDS + MICROPHONE_REOPEN_SECONDS)

        for index, step in enumerate(STEPS, 1):
            started = time.monotonic()
            result = {"step": step.name, "say": step.say, "problems": []}
            results.append(result)
            try:
                heard = None
                for attempt in range(2):
                    mark = len(trace.events)
                    await run_command("afplay", "-v", SPEAKER_GAIN, str(recordings[step.say]))
                    try:
                        heard = await wait_for(
                            lambda mark=mark: next((event for event in trace.events[mark:]
                                                    if event["type"] == "user.input"), None),
                            12, "机器人识别到这句话",
                        )
                        break
                    except TimeoutError:
                        result["replayed"] = True
                if heard is None:
                    raise TimeoutError("播放两次之后机器人都没有识别到这句话")
                turn_id = heard["turn_id"]
                await wait_for(lambda turn_id=turn_id: trace.turn(turn_id).settled(),
                               step.timeout, "这一轮处理和播报完成")
                turn = trace.turn(turn_id)
                result.update(heard=turn.heard, spoken=turn.spoken, latency=turn.latency,
                              tools=[f"{call['actor']}.{call['tool']}" for call in turn.tools()])
                if SequenceMatcher(None, step.say, turn.heard).ratio() < 0.6:
                    result["problems"].append(f"识别结果和原话差别较大：{turn.heard}")
                result["problems"] += step.check(turn)
            except TimeoutError as error:
                result["problems"].append(str(error))
            result["seconds"] = round(time.monotonic() - started, 1)
            status = "通过" if not result["problems"] else "失败：" + "；".join(result["problems"])
            latency = "" if result.get("latency") is None else f"，响应 {result['latency']:.1f} 秒"
            replayed = "，第一次没有识别到、重播了一次" if result.get("replayed") else ""
            print(f"[{index}/{len(STEPS)}] {step.name}（{result['seconds']} 秒{latency}{replayed}）"
                  f"{status}")
            print(f"      用户：{result.get('heard', step.say)}")
            for text in result.get("spoken", []):
                print(f"      机器人：{text}")
            if app.returncode is not None:
                print("应用已经退出，停止测试。")
                break
            await asyncio.sleep(MICROPHONE_REOPEN_SECONDS)
    finally:
        await run_command("uv", "run", "watcherobot", "app", "stop")
        try:
            await asyncio.wait_for(app.wait(), 30)
        except TimeoutError:
            app.terminate()
        await pumping
        following.cancel()
        (directory / "app.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        (directory / "trace.json").write_text(
            json.dumps(trace.events, ensure_ascii=False, indent=1), encoding="utf-8",
        )

    exit_line = next((line for line in reversed(log_lines) if "process exited" in line), "")
    warnings = [line for line in log_lines if " WARNING " in line or " ERROR " in line]
    crashed = any("Traceback" in line for line in log_lines) or "code=0" not in exit_line
    passed = sum(not result["problems"] for result in results)
    report = {
        "passed": passed, "total": len(STEPS), "ran": len(results), "exit": exit_line,
        "crashed": crashed, "warnings": warnings, "steps": results,
    }
    (directory / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    replays = sum(bool(result.get("replayed")) for result in results)
    print(f"\n通过 {passed}/{len(STEPS)} 步，{replays} 步重播过；"
          f"应用退出：{exit_line or '没有退出记录'}")
    if warnings:
        print(f"应用日志里有 {len(warnings)} 条告警或错误：")
        for line in warnings[:10]:
            print("   ", line[-160:])
    print(f"报告：{directory / 'report.json'}")
    return 0 if passed == len(STEPS) and not crashed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
