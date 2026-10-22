"""猜谜语：一个可以单独部署的示例游戏。

谜语和判断都写在代码里，主持交给大模型；猜对时点头并在屏幕上画一颗星星，猜错时摇头。
改成你自己的游戏时，保留 GameAgent 的结构，换掉题目、工具和 SKILL.md。
"""
import math
import random
from pathlib import Path

from agentcade.protocol import Draw, Gesture
from agentcade.skill import Skill
from agentcade.skill_agent import SkillAgent
from agentcade.tools import tool
from agentcade.vector import Stroke, encode_vector

from .config import Settings

# 谜面、可以算对的谜底、一条提示。
RIDDLES = [
    ("什么东西越洗越脏？", ["水"], "洗东西的时候离不开它。"),
    ("什么门永远关不上？", ["球门"], "足球场上就有。"),
    ("什么东西天气越热，它爬得越高？", ["温度计", "温度表"], "它是用来量冷热的。"),
    ("什么东西有头没有脚，有身没有手？", ["针", "钉子"], "它又细又尖。"),
]


def star() -> list[str]:
    """一颗五角星的作画步骤：先画一半，再画完整。屏幕坐标 0 到 411。"""
    points = []
    for index in range(11):
        radius = 150 if index % 2 == 0 else 62
        angle = -math.pi / 2 + index * math.pi / 5
        points.append((round(206 + radius * math.cos(angle)), round(206 + radius * math.sin(angle))))
    return [encode_vector((Stroke(tuple(points[:6]), 8),)),
            encode_vector((Stroke(tuple(points), 8),))]


class GameAgent(SkillAgent):
    skill = Skill.load(Path(__file__).with_name("SKILL.md"))
    config = Settings

    def __init__(self, settings):
        super().__init__(settings)
        self.riddle = None
        self.resolved = False
        self.solved = 0

    def state(self):
        # 每轮写进提示词，不能包含谜底。
        return {"riddle": self.riddle[0] if self.riddle else None, "solved": self.solved}

    def ask(self, turn):
        self.riddle, self.resolved = random.choice(RIDDLES), False
        # 谜面一定要念出来，大模型漏掉时程序会补上。
        turn.must_say(self.riddle[0])
        return {"riddle": self.riddle[0]}

    def reveal(self, turn):
        self.resolved = True
        answer = self.riddle[1][0]
        turn.must_say(answer, fallback=f"谜底是{answer}。")
        return answer

    async def on_start(self, turn):
        # 开局时程序直接出好第一个谜语，不用等大模型调用工具。
        return self.ask(turn)

    async def on_end(self, turn):
        # 用户没猜出来就结束时，公布这个谜语的谜底。
        if self.riddle and not self.resolved:
            return {"solved": self.solved, "answer": self.reveal(turn)}
        return {"solved": self.solved}

    # 工具名就是方法名，也可以用 @tool("说明", name="...") 另起一个。参数 turn 由框架传入，
    # 其余参数由大模型填写，要写类型注解，例如 number: Annotated[int, Field(ge=1, le=20)]。
    @tool("出一个新谜语，返回谜面。")
    def new_riddle(self, turn):
        return self.ask(turn)

    @tool("判断用户这一轮说的谜底对不对（直接读取用户原话）。")
    async def check_answer(self, turn):
        if self.riddle is None:
            return {"outcome": "no_riddle"}
        # 判定用 turn.text，也就是用户原话，不让大模型转述或者替用户纠正。
        if any(answer in turn.text for answer in self.riddle[1]):
            self.solved += 1
            await self.act(Gesture(name="nod"), Draw(paths=star()))
            return {"correct": True, "answer": self.reveal(turn), **self.state()}
        await self.act(Gesture(name="shake_head"))
        return {"correct": False}

    @tool("取得当前谜语的提示。")
    def get_hint(self):
        return {"hint": self.riddle[2]} if self.riddle else {"outcome": "no_riddle"}

    @tool("用户放弃时公布谜底。")
    def reveal_answer(self, turn):
        return {"answer": self.reveal(turn)} if self.riddle else {"outcome": "no_riddle"}
