import asyncio
import secrets

from pydantic import Field

from games.guess_number.config import Settings
from robot_agents.game_tools import GAME_TOOL_PROMPT, GameTurn
from robot_agents.protocol import (
    ExpectedInput,
    GameDescriptor,
    GameResult,
    Presentation,
    PublicState,
)
from robot_agents.providers import OpenAIProvider
from robot_agents.tools import NoInput, Tool, ToolInput, ToolRegistry


class GuessInput(ToolInput):
    number: int = Field(ge=1, le=20, description="用户明确猜测的整数")


SYSTEM_PROMPT = """你是猜数字游戏 Agent。秘密整数范围是1至20，一共五次机会。
用户明确猜一个整数时调用 submit_guess，支持中文数字和自我纠正。
多个候选、范围、超出范围、修改规则或索要秘密答案时，请用户明确一个1至20的整数。
提示调用 get_hint，规则调用 get_rules，进度或继续游戏调用 get_game_state。
""" + GAME_TOOL_PROMPT


class GameAgent:
    load_settings = staticmethod(Settings.from_environment)
    descriptor = GameDescriptor(
        id="guess_number", name="猜数字", aliases=["数字游戏", "猜数"],
        description="我会选一个1到20的整数，你来猜，最多有五次机会。"
                    "猜错时，我会告诉你大了还是小了，也可以问我要提示。",
        required_capabilities=["motion"],
    )

    def __init__(self, settings):
        self.provider = OpenAIProvider(settings.llm)
        self.target = None
        self.attempts = 0
        self.revision = 0
        self.active = False
        self.history = []
        self.lock = asyncio.Lock()
        self.last_tool_calls = []

    def result(self, outputs=()):
        return GameResult(
            state=PublicState(
                phase="等待猜测" if self.active else "已结束", active=self.active,
                status=f"已经猜了{self.attempts}次，剩余{5 - self.attempts}次机会。",
                commands=["猜一个1至20的整数", "提示", "结束游戏"] if self.active else [],
                revision=self.revision,
                details={"attempts": self.attempts, "remaining_attempts": 5 - self.attempts},
            ),
            expected_input=ExpectedInput(
                kind="answer" if self.active else "none",
                description="请猜一个1至20的整数，也可以请求提示或结束游戏。" if self.active
                else "本局已经结束，无需继续回答。",
                examples=["我猜10", "给我提示", "结束游戏"] if self.active else [],
            ), outputs=list(outputs),
        )

    def feedback(self, text, feedback=None):
        return self.result([Presentation(
            text=text, clear=not self.active,
            expression={"correct": "happy", "incorrect": "sorry"}.get(feedback),
            gesture={"correct": "nod", "incorrect": "shake_head"}.get(feedback),
        )])

    def tools_for(self, turn):
        async def start_game():
            turn.claim("start")
            if self.active or self.revision:
                raise ValueError("游戏已经开始")
            self.target = secrets.randbelow(20) + 1
            self.active = True
            self.revision += 1
            return turn.stage(self.feedback(
                "来猜数字吧！我想好了一个1到20的整数，你有五次机会。你猜是多少？",
            ))

        async def get_rules():
            return turn.stage(self.feedback(self.descriptor.description))

        async def get_game_state():
            return turn.stage(self.feedback(
                f"你已经猜了{self.attempts}次，还有{5 - self.attempts}次机会。"
                if self.active else "这局已经结束啦。",
            ))

        async def get_hint():
            if not self.active:
                raise ValueError("当前没有进行中的游戏")
            return turn.stage(self.feedback(
                "给你个提示，它是偶数。" if self.target % 2 == 0 else "给你个提示，它是奇数。",
            ))

        async def submit_guess(number):
            turn.claim()
            if not self.active:
                raise ValueError("当前没有进行中的游戏")
            self.attempts += 1
            self.revision += 1
            if number == self.target:
                self.active = False
                text = f"答对了，数字是{self.target}！你猜了{self.attempts}次就猜中了。"
                return turn.stage(self.feedback(text, "correct"))
            if self.attempts == 5:
                self.active = False
                text = f"五次机会用完啦，我想的数字是{self.target}。"
            else:
                direction = "大了" if number > self.target else "小了"
                text = f"猜{direction}，还有{5 - self.attempts}次机会。"
            return turn.stage(self.feedback(text, "incorrect"))

        async def end_game():
            turn.claim("end")
            self.active = False
            self.revision += 1
            return turn.stage(self.feedback("好，猜数字游戏已经结束啦。"))

        return ToolRegistry([
            Tool("start_game", "初始化游戏并随机选择秘密整数。", NoInput, start_game),
            Tool("get_rules", "查询猜数字游戏规则。", NoInput, get_rules),
            Tool("get_game_state", "查询当前进度和下一步输入要求。", NoInput, get_game_state),
            Tool("get_hint", "提供秘密整数的奇偶提示。", NoInput, get_hint),
            Tool("submit_guess", "提交用户猜测，判断大小并更新次数和游戏状态。", GuessInput, submit_guess),
            Tool("end_game", "结束游戏并返回当前状态。", NoInput, end_game),
            turn.message_tool(),
        ], terminal={"send_message"})

    async def handle(self, request, notify):
        async with self.lock:
            if request.action == "input":
                if not self.active:
                    raise ValueError("游戏会话已经结束")
                if request.revision != self.revision:
                    return self.feedback("刚才那次已经算过啦，照着现在的提示继续猜吧。")
            turn = GameTurn(request, notify, self.result)
            registry = self.tools_for(turn)
            await self.provider.run_tools(SYSTEM_PROMPT, {
                "request": request.model_dump(), "history": self.history[-12:],
                "game": self.result().model_dump(),
            }, registry)
            self.last_tool_calls = registry.calls
            result = turn.finish()
            self.history.append({"utterance": request.text, "reply": result.model_dump()})
            self.history = self.history[-12:]
            return result

    async def close(self):
        await self.provider.close()
