import asyncio
import json
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4

SCOPE = {
    "actor": ContextVar("observability_actor", default="main"),
    "turn_id": ContextVar("observability_turn_id", default=None),
    "parent_id": ContextVar("observability_parent_id", default=None),
}


@dataclass(frozen=True)
class Event:
    seq: int
    data: str


def encode(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else str(value)


def failure(error):
    # 用户结束游戏会取消未完成的请求，页面把它和出错区分显示。
    if isinstance(error, asyncio.CancelledError):
        return {"error": "已取消", "cancelled": True}
    return {"error": f"{type(error).__name__}: {error}"}


class EventBus:
    def __init__(self, capacity=2000):
        self.run_id = uuid4().hex
        self.events: deque[Event] = deque(maxlen=capacity)
        self.seq = 0
        self.changed = asyncio.Event()
        self.turns: OrderedDict[str, str | None] = OrderedDict()

    def emit(self, kind, payload):
        self.seq += 1
        record = {
            "seq": self.seq, "type": kind, "time": time.time(), "payload": payload,
            **{name: variable.get() for name, variable in SCOPE.items()},
        }
        try:
            data = json.dumps(record, ensure_ascii=False, allow_nan=False, default=encode)
        except (TypeError, ValueError) as error:
            # 记录失败只影响这一条事件，不能中断语音和游戏流程；保留标识字段，页面仍能配对调用和结果。
            record["payload"] = {
                **{name: value for name, value in payload.items()
                   if isinstance(value, str | bool | int)},
                "error": f"事件内容无法序列化：{error}",
            }
            data = json.dumps(record, ensure_ascii=False)
        self.events.append(Event(self.seq, data))
        self.wake()

    def wake(self):
        changed, self.changed = self.changed, asyncio.Event()
        changed.set()

    def after(self, seq):
        return [event for event in self.events if event.seq > seq]

    def remember(self, request_id):
        self.turns[request_id] = SCOPE["turn_id"].get()
        while len(self.turns) > 256:
            self.turns.popitem(last=False)


bus = EventBus()


@contextmanager
def scope(**values):
    tokens = [(SCOPE[name], SCOPE[name].set(value)) for name, value in values.items()]
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def elapsed_ms(started):
    return round((time.monotonic() - started) * 1000)


def user_input(text, started_at, ended_at=None, sent_until=None):
    """记录 ASR 的最终识别结果，返回新一轮的 ID。时间参数是采集音频时的单调时钟读数。"""
    identifier = uuid4().hex
    offset = time.time() - time.monotonic()
    with scope(actor="user", turn_id=identifier, parent_id=None):
        bus.emit("user.input", {
            "text": text, "spoken_at": offset + started_at,
            "speech_ended_at": None if ended_at is None else offset + ended_at,
            "audio_sent_until": None if sent_until is None else offset + sent_until,
        })
    return identifier


def turn(identifier):
    return scope(turn_id=identifier)


def reply(text):
    """记录主 Agent 将要播报的聊天或游戏回复。"""
    bus.emit("agent.reply", {"text": text})


@contextmanager
def tool_call(name, arguments):
    call_id, started = uuid4().hex, time.monotonic()
    outcome = {}
    bus.emit("tool.call", {"call_id": call_id, "tool": name, "arguments": arguments})
    try:
        # 工具内部发出的 A2A 请求以本次调用作为上级。
        with scope(parent_id=call_id):
            yield outcome
    except BaseException as error:
        outcome = failure(error)
        raise
    finally:
        bus.emit("tool.result", {
            "call_id": call_id, "tool": name, "ok": "result" in outcome, **outcome,
            "duration_ms": elapsed_ms(started),
        })


class Exchange:
    def __init__(self, request_id):
        self.request_id = request_id
        self.result = None

    def update(self, reply):
        """记录游戏在最后一条回复之前发来的中间回复。"""
        bus.emit("a2a.update", {"request_id": self.request_id, "result": reply})


@contextmanager
def a2a_request(game, context_id, request_id, request):
    started = time.monotonic()
    exchange = Exchange(request_id)
    bus.remember(request_id)
    bus.emit("a2a.request", {
        "request_id": request_id, "game_id": game.id, "game_name": game.name,
        "context_id": context_id, "request": request,
    })
    outcome = {}
    try:
        yield exchange
        outcome = {"result": exchange.result}
    except BaseException as error:
        outcome = failure(error)
        raise
    finally:
        bus.emit("a2a.result", {
            "request_id": request_id, "ok": "result" in outcome, **outcome,
            "duration_ms": elapsed_ms(started),
        })


def game_request(game_id, request_id):
    """游戏服务在独立任务中处理请求，通过 request_id 关联发起请求的那一轮。"""
    return scope(actor=game_id, turn_id=bus.turns.get(request_id), parent_id=request_id)
