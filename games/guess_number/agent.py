import secrets
from pathlib import Path

from pydantic import Field

from games.guess_number.config import Settings
from robot_agents.skill import Skill
from robot_agents.skill_agent import SkillAgent
from robot_agents.tools import NoInput, Tool, ToolInput

MAX_ATTEMPTS = 5


class GuessInput(ToolInput):
    number: int = Field(ge=1, le=20, description="用户明确猜测的整数")


class GameAgent(SkillAgent):
    skill = Skill.load(Path(__file__).with_name("SKILL.md"))
    load_settings = staticmethod(Settings.from_environment)

    def __init__(self, settings, device):
        super().__init__(settings, device)
        self.target = None
        self.attempts = 0
        self.active = False

    def state(self):
        if self.active:
            phase = "等待猜测"
        else:
            phase = "等待开始" if self.target is None else "已结束"
        return {"phase": phase, "attempts": self.attempts,
                "remaining_attempts": MAX_ATTEMPTS - self.attempts}

    def tools(self, turn):
        answered = False

        def finish(outcome):
            self.active = False
            turn.end_session()
            return {"outcome": outcome, "answer": self.target, **self.state()}

        async def start_game():
            if self.target is not None:
                return {"outcome": "already_started", **self.state()}
            self.target = secrets.randbelow(20) + 1
            self.active = True
            return {"outcome": "started", "minimum": 1, "maximum": 20,
                    "max_attempts": MAX_ATTEMPTS}

        async def get_hint():
            if not self.active:
                return {"outcome": "not_playing", **self.state()}
            return {"parity": "even" if self.target % 2 == 0 else "odd"}

        async def submit_guess(number):
            nonlocal answered
            if not self.active:
                return {"outcome": "not_playing", **self.state()}
            if answered:
                return {"outcome": "already_answered", **self.state()}
            answered = True
            self.attempts += 1
            # 答对点头并显示开心，答错摇头并显示抱歉，由程序在判定时直接触发。
            if number == self.target:
                await self.react("happy", "nod")
                return finish("correct")
            await self.react("sorry", "shake_head")
            if self.attempts == MAX_ATTEMPTS:
                return finish("exhausted")
            return {"outcome": "incorrect",
                    "direction": "too_high" if number > self.target else "too_low",
                    **self.state()}

        async def end_game():
            return finish("ended")

        return [
            Tool("start_game", "初始化游戏并随机选择秘密整数。", NoInput, start_game),
            Tool("get_hint", "提供秘密整数的奇偶提示。", NoInput, get_hint),
            Tool("submit_guess", "提交用户猜测，判断大小并更新次数；每条用户输入只能提交一次。",
                 GuessInput, submit_guess),
            Tool("end_game", "结束游戏，返回秘密整数和当前状态。", NoInput, end_game),
        ]
