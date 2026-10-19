import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from pydantic import Field, create_model

import observability
from agentcade.config import Settings
from agentcade.protocol import CAPABILITIES, GameInput, Presentation, presentations
from agentcade.providers import OpenAIProvider
from agentcade.tools import NoInput, Tool, ToolInput, ToolRegistry, original_text_input
from observability.events import SCOPE

logger = logging.getLogger(__name__)

# 游戏请求失败时对用户说的话，按请求类型区分。
GAME_FAILURE_REPLIES = {
    "start": "这个游戏现在开不了，等一下再试试吧。",
    "message": "游戏那边没有反应，你再说一遍好吗？",
    "end": "好，这局先结束了。",
}


class StartGameInput(ToolInput):
    game_id: str = Field(min_length=1, description="list_games 返回的游戏 ID")


SYSTEM_PROMPT = """你叫小懒，是一台桌面机器人，陪用户聊天、带用户玩游戏。
你的回复会被原样念出来：只写要说的话，通常一至三句、120字以内，用简短自然的口语，语气友好，不刻意卖萌。
current_utterance 来自语音识别，可能有同音错字或只有半句，按读音和上下文理解；实在听不懂就请用户再说一遍。
history 是之前的对话，role=game_agent 的内容是游戏里说过的话。
面向用户只说游戏名称和玩法，不提 Agent、工具、会话、协议或内部状态字段。
点头、摇头、表情和画画只在游戏里才会做。聊天时用户让你做动作，如实说现在做不了，不要假装已经做了。

游戏：
可玩的游戏列在末尾的目录里，名称、别名、介绍和顺序都以目录为准，不补充目录里没有的游戏。用户问有哪些游戏时，按目录简短介绍怎么玩。
闲聊时不提游戏：不推荐，也不用“想玩随时说”这类话收尾；用户自己问起游戏才介绍。
规则、题目、答案、提示、判定和计分都由游戏负责，你不能自己生成或宣布。

工具：
start_game、end_game、send_message 一调用，这一轮就结束，游戏的回复会直接念给用户，所以每轮只选最该做的那一个。
- active_game 为 null，用户要开始、再玩一局或重新开始某个游戏：立即调用 start_game，不用再确认，刚玩过同一个游戏也一样。
- 有游戏在进行，用户的话和这局有关（回答、要提示、跳题、问规则、次数、得分、进度、说继续）：调用 send_message 传 current_utterance 完整原文，不根据历史自己回答。active_game.busy 为 true 时也一样。
- 用户要结束当前游戏：调用 end_game。用户说“结束，然后玩另一个”时也调用 end_game。
- 有游戏在进行，用户想换游戏：不转给游戏，直接告诉他先说“结束游戏”；没说换哪个时，顺便说可以换成什么。
- 普通聊天和知识问题：直接回答，不调用工具。答完就等用户说话，不催促答题，也不替游戏出题。"""


@dataclass
class ActiveGame:
    game_id: str
    context_id: str
    closing: bool = False


@dataclass
class OutputRequest:
    outputs: list[Presentation]
    completed: asyncio.Future
    trace_context: dict[str, str | None]
    role: str = "assistant"


class MainAgent:
    def __init__(self, settings: Settings, games):
        self.provider = OpenAIProvider(settings.llm)
        self.games = games
        self.active: ActiveGame | None = None
        self.history = deque(maxlen=48)
        self.outputs: asyncio.Queue[OutputRequest] = asyncio.Queue(32)
        self.lock = asyncio.Lock()
        self.input_lock = asyncio.Lock()
        self.pending: set[asyncio.Task] = set()
        self.closed = False

    def system_prompt(self):
        catalog = [game.descriptor.model_dump() for game in self.games.values()]
        return (
            SYSTEM_PROMPT
            + "\n\n当前可用游戏：\n"
            + json.dumps(catalog, ensure_ascii=False, indent=2)
        )

    @property
    def busy(self):
        return any(not task.done() for task in self.pending)

    def status(self):
        if self.active is None:
            return None
        return {
            "game_id": self.active.game_id,
            "name": self.games[self.active.game_id].descriptor.name,
            "busy": self.busy,
            "closing": self.active.closing,
        }

    async def emit(self, outputs, *, role="assistant"):
        completion = asyncio.get_running_loop().create_future()
        self.outputs.put_nowait(OutputRequest(
            outputs, completion, {name: variable.get() for name, variable in SCOPE.items()}, role,
        ))
        return completion

    async def reply(self, text, *, role="assistant"):
        observability.reply(text, role)
        return await self.emit([Presentation(text=text)], role=role)

    async def receive(self, text):
        async with self.input_lock:
            completions = []
            registry = self.tools_for(text, completions)
            history = list(self.history)
            self.history.append({"role": "user", "content": text})
            try:
                response = await self.provider.run_tools([
                    {"role": "system", "content": self.system_prompt()},
                    {"role": "user", "content": json.dumps({
                        "history": history,
                        "active_game": self.status(),
                        "current_utterance": text,
                    }, ensure_ascii=False)},
                ], registry)
            except Exception:
                # 模型服务出错或超时只影响这一句话，说明情况后继续听下一句。
                logger.exception("主 Agent 处理用户输入失败")
                if completions:
                    return completions[-1]
                return await self.reply("我刚才没反应过来，你再说一遍好吗？")
            if completions:
                return completions[-1]
            if response:
                return await self.reply(response)
            return None

    def tools_for(self, utterance, completions):
        operated = False
        message_input = original_text_input(utterance)
        start_input = create_model(
            "RegisteredGameInput", __base__=StartGameInput,
            game_id=(Literal[tuple(self.games)] if self.games else str,
                     Field(description="注册目录中的游戏 ID")),
        )

        def claim():
            nonlocal operated
            if operated:
                raise ValueError("每轮只能执行一次游戏会话操作")
            operated = True

        async def notice(message):
            completions.append(await self.reply(message))
            return {"message": message}

        async def list_games():
            return [game.descriptor.model_dump() for game in self.games.values()]

        async def start_game(game_id):
            async with self.lock:
                claim()
                if self.active is not None:
                    name = self.games[self.active.game_id].descriptor.name
                    return await notice(f"我们还在玩{name}呢。想换一个的话，先说结束游戏就好。")
                if game_id not in self.games:
                    raise ValueError("主 Agent 选择了未注册的游戏")
                self.active = ActiveGame(game_id, uuid4().hex)
                completions.append(await self.send(GameInput(kind="start", text=utterance)))
                return {"delivered": True, "game": self.status()}

        def unavailable():
            if self.active is None:
                return "我们还没开始玩呢。想玩什么？也可以问我有哪些游戏。"
            if self.active.closing:
                return "这局马上就结束了，稍等一下。"
            return None

        async def end_game():
            async with self.lock:
                claim()
                message = unavailable()
                if message:
                    return await notice(message)
                self.active.closing = True
                await self.cancel_pending()
                completions.append(await self.send(
                    GameInput(kind="end", text=utterance, reason="user"),
                ))
                return {"delivered": True, "game": self.status()}

        async def send_message(text):
            async with self.lock:
                claim()
                message = unavailable()
                # 游戏逐条处理输入，上一条的回复还没结束时不再发送新的输入。
                if message is None and self.busy:
                    message = "我还在准备呢，稍等一下哦。"
                if message:
                    return await notice(message)
                completions.append(await self.send(GameInput(kind="message", text=text)))
                return {"delivered": True, "game": self.status()}

        return ToolRegistry([
            Tool("list_games", "列出可用游戏的 ID、名称、描述和别名。", NoInput, list_games),
            Tool("start_game", "开始指定游戏，建立会话并把用户原话发给游戏 Agent。", start_input, start_game),
            Tool("end_game", "结束当前游戏并取消未完成的游戏请求。", NoInput, end_game),
            Tool("send_message", "将用户原话发送给当前游戏 Agent。", message_input, send_message),
        ], terminal={"start_game", "end_game", "send_message"})

    async def send(self, request):
        """把一条输入发给当前游戏，返回第一条带话的回复的播报结果；其余回复到达后继续播报。"""
        active = self.active
        request_id = uuid4().hex
        first = asyncio.get_running_loop().create_future()

        async def deliver(reply):
            if self.active is not active:
                return
            completion = await self.accept(active.game_id, request_id, reply)
            # 只有设备指令的回复（例如点头）不结束这一轮，等到第一句话播完再恢复收音。
            if reply.text and not first.done():
                first.set_result(completion)

        async def exchange():
            try:
                await deliver(await self.games[active.game_id].send(
                    active.context_id, request, on_reply=deliver, request_id=request_id,
                ))
            except Exception:
                logger.exception("游戏请求 %s 失败", request.kind)
                if self.active is not active:
                    return
                # 开局和结束失败时不保留这一局，局内输入失败时用户可以重说。
                if request.kind != "message":
                    self.active = None
                completion = await self.reply(GAME_FAILURE_REPLIES[request.kind])
                if not first.done():
                    first.set_result(completion)

        task = asyncio.create_task(exchange())
        self.pending.add(task)
        await asyncio.wait({first, task}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            self.pending.discard(task)
            await task
        if not first.done():
            # 会话在等待期间被清除（例如正在退出），没有需要播报的内容。
            first.set_result([])
            return first
        return first.result()

    async def accept(self, game_id, request_id, reply):
        """先执行回复里的设备指令，再播报要说的话。"""
        if reply.session == "ended":
            self.active = None
        completion = None
        outputs = self.permitted(game_id, reply.directives)
        if outputs:
            # 设备指令记在发出它的游戏名下，和游戏处理请求时的其他记录排在一起。
            with observability.game_request(game_id, request_id):
                completion = await self.emit(outputs, role="game_agent")
        if reply.text:
            completion = await self.reply(reply.text, role="game_agent")
        return completion

    def permitted(self, game_id, directives) -> list[Presentation]:
        """游戏只能使用它声明过的机器人能力，没有声明的指令跳过并记录。"""
        declared = set(self.games[game_id].descriptor.required_capabilities)
        allowed = []
        for directive in directives:
            needed = CAPABILITIES.get(directive.type)
            if needed and needed not in declared:
                logger.warning("游戏 %s 的 %s 指令需要能力 %s，但没有声明，已跳过",
                               game_id, directive.type, needed)
                continue
            allowed.append(directive)
        return presentations(allowed)

    async def output_done(self, request):
        async with self.lock:
            for output in request.outputs:
                if output.text:
                    self.history.append({"role": request.role, "content": output.text})
            request.completed.set_result([output.text for output in request.outputs if output.text])

    async def tick(self):
        if self.input_lock.locked():
            return
        for task in list(self.pending):
            if task.done():
                self.pending.remove(task)
                if not task.cancelled() and task.exception() is not None:
                    logger.error("游戏请求任务异常结束", exc_info=task.exception())

    async def cancel_pending(self):
        pending = list(self.pending)
        for task in pending:
            task.cancel()
        if pending:
            results = await asyncio.gather(*pending, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("游戏请求任务异常结束", exc_info=result)
        self.pending.clear()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.cancel_pending()
        try:
            if self.active and not self.active.closing:
                await self.games[self.active.game_id].send(
                    self.active.context_id, GameInput(kind="end", reason="shutdown"),
                )
        except Exception:
            # 退出时通知游戏失败不影响关闭流程。
            logger.exception("退出时结束游戏失败")
        finally:
            self.active = None
            await self.provider.close()
