import logging
from pathlib import Path

from agentcade.protocol import Presentation
from agentcade.skill import Skill
from agentcade.skill_agent import SkillAgent
from agentcade.tools import NoInput, Tool, original_text_input
from games.turtle_soup.config import Settings
from games.turtle_soup.judge import JevJudge
from games.turtle_soup.puzzles import load_puzzles, pick

logger = logging.getLogger(__name__)

# 判断提问时，是点头、不是摇头；无关和不好说不做动作。
GESTURES = {"yes": "nod", "no": "shake_head"}


class GameAgent(SkillAgent):
    skill = Skill.load(Path(__file__).with_name("SKILL.md"))
    config = Settings

    def __init__(self, settings, device):
        super().__init__(settings, device)
        self.judge = JevJudge(settings)
        self.puzzles = load_puzzles()
        self.puzzle = None
        self.played: set[str] = set()
        self.resolved = False
        self.ended = False
        self.questions = 0
        self.hints = 0
        self.solved = 0

    def phase(self):
        if self.ended:
            return "已结束"
        if self.puzzle is None:
            return "等待开始"
        return "已揭晓" if self.resolved else "推理中"

    def state(self):
        # 汤底不放进状态，只在猜中、放弃或结束时由工具结果给出。
        public = {"phase": self.phase(), "questions_asked": self.questions,
                  "hints_used": self.hints, "puzzles_played": len(self.played),
                  "puzzles_solved": self.solved}
        if self.puzzle is not None:
            public |= {"title": self.puzzle.title, "surface": self.puzzle.surface}
        return public

    def serve(self):
        puzzle = pick(self.puzzles, self.played)
        if puzzle is None:
            return {"outcome": "no_more_puzzles", **self.state()}
        self.puzzle, self.resolved = puzzle, False
        self.questions = self.hints = 0
        self.played.add(puzzle.id)
        return {"outcome": "started", **self.state()}

    def reveal(self, outcome):
        self.resolved = True
        return {"outcome": outcome, "truth": self.puzzle.truth, **self.state()}

    def tools(self, turn):
        judged = False

        def unavailable():
            if self.ended:
                return "game_over"
            if self.puzzle is None:
                return "not_started"
            return "puzzle_resolved" if self.resolved else None

        async def start_game():
            if self.puzzle is not None or self.ended:
                return {"outcome": "already_started", **self.state()}
            return self.serve()

        async def ask_question(text):
            nonlocal judged
            reason = unavailable()
            if reason or judged:
                return {"outcome": reason or "already_judged", **self.state()}
            answer = await self.judge.answer(self.puzzle, text)
            if answer in {"not_question", "unsure"}:
                # 没有给出是或不是，不算一次提问，这条输入还可以改为提交推理。
                return {"answer": answer, **self.state()}
            judged = True
            self.questions += 1
            if answer in GESTURES:
                await self.gesture(GESTURES[answer])
            return {"answer": answer, **self.state()}

        async def submit_solution(text):
            nonlocal judged
            reason = unavailable()
            if reason or judged:
                return {"outcome": reason or "already_judged", **self.state()}
            verdict = await self.judge.verify(self.puzzle, text)
            if verdict == "unsure":
                return {"verdict": verdict, **self.state()}
            judged = True
            if verdict == "solved":
                self.solved += 1
                await self.react("happy", "nod")
                return {"verdict": verdict, **self.reveal("solved")}
            if verdict == "wrong":
                await self.gesture("shake_head")
            return {"verdict": verdict, **self.state()}

        async def get_hint():
            reason = unavailable()
            if reason:
                return {"outcome": reason, **self.state()}
            if self.hints == len(self.puzzle.hints):
                return {"outcome": "hints_exhausted", **self.state()}
            hint = self.puzzle.hints[self.hints]
            self.hints += 1
            return {"outcome": "hint", "hint": hint, **self.state()}

        async def reveal_truth():
            reason = unavailable()
            if reason:
                return {"outcome": reason, **self.state()}
            return self.reveal("revealed")

        async def next_puzzle():
            if self.ended or self.puzzle is None:
                return {"outcome": "game_over" if self.ended else "not_started", **self.state()}
            if not self.resolved:
                return {"outcome": "puzzle_unresolved", **self.state()}
            return self.serve()

        async def end_game():
            unfinished = self.puzzle is not None and not self.resolved
            self.ended = True
            turn.end_session()
            # 没猜完的那道题公布汤底，避免模型自己编一个。
            return {"outcome": "ended", **({"truth": self.puzzle.truth} if unfinished else {}),
                    **self.state()}

        answer_input = original_text_input(turn.text)
        return [
            Tool("start_game", "开局并随机选一道题，返回题目名称和汤面。", NoInput, start_game),
            Tool("ask_question", "按汤底回答用户的是非提问，结果为 yes、no、irrelevant、"
                 "not_question 或 unsure；每条用户输入只能判断一次。", answer_input, ask_question),
            Tool("submit_solution", "判断用户说出的推理有没有猜中汤底，结果为 solved、partial、"
                 "wrong 或 unsure；猜中时返回汤底。每条用户输入只能判断一次。",
                 answer_input, submit_solution),
            Tool("get_hint", "取得这道题的下一条提示，每题三条。", NoInput, get_hint),
            Tool("reveal_truth", "用户放弃时公布汤底，这道题结束。", NoInput, reveal_truth),
            Tool("next_puzzle", "这道题揭晓之后换一道新题，返回汤面。", NoInput, next_puzzle),
            Tool("end_game", "结束游戏，返回猜中的题数和没猜完那道题的汤底。", NoInput, end_game),
        ]

    async def gesture(self, name):
        try:
            await self.device(Presentation(gesture=name))
        except Exception:
            # 动作只是点缀，设备出错不影响判断结果和回复。
            logger.warning("动作 %s 没有执行", name, exc_info=True)

    async def close(self):
        try:
            await super().close()
        finally:
            await self.judge.close()
