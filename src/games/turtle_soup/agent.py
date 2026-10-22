import asyncio
import logging
import time
from pathlib import Path

from agentcade.protocol import Gesture
from agentcade.skill import Skill
from agentcade.skill_agent import SkillAgent
from agentcade.tools import tool

from .config import Settings
from .generator import PuzzleGenerator
from .judge import JevJudge
from .library import Library

logger = logging.getLogger(__name__)

# 判断提问时，是点头、不是摇头；无关和不好说不做动作。
GESTURES = {"yes": "nod", "no": "shake_head"}
# 题都玩过时现场出题，超过这么多秒后不再重写，用已有的最好一版。
GENERATE_BUDGET = 240
# 现场出题时每到一个阶段播报一句；同一阶段很久没说话，就再说一句还在做什么，
# 间隔从 25 秒开始逐次拉长，最长 60 秒。
PROGRESS_INTERVALS = (25, 35, 45, 60)
PROGRESS = {
    "condense": "故事写好了，正在把它改成谜题。",
    "review": "谜题出好了，我检查一下合不合格。",
    "fallback": "改了几版都不太满意，先用最好的这一版。",
}
RETRY = "这一版没通过检查，我换个故事重新写。"
WAITING = {
    "story": ("故事还在写，再等我一下。", "还在写故事，我想把前因后果编圆一点。",
              "故事还没写完，已经过了{seconds}秒了，谢谢你耐心等。"),
    "condense": ("还在改成谜题，再等我一下。", "谜题还在改，已经过了{seconds}秒了。"),
    "review": ("还在检查，马上就好。",),
}


class GameAgent(SkillAgent):
    skill = Skill.load(Path(__file__).with_name("SKILL.md"))
    config = Settings

    def __init__(self, settings):
        super().__init__(settings)
        self.judge = JevJudge(settings)
        self.generator = PuzzleGenerator(settings)
        self.library = Library()
        self.puzzle = None
        self.played = 0
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
                  "hints_used": self.hints, "puzzles_played": self.played,
                  "puzzles_solved": self.solved}
        if self.puzzle is not None:
            public |= {"title": self.puzzle.title, "surface": self.puzzle.surface}
        return public

    async def serve(self, turn):
        puzzle, outcome = self.library.pick(), "started"
        if puzzle is None:
            puzzle, outcome = await self.create(turn)
        self.library.serve(puzzle)
        self.puzzle, self.resolved = puzzle, False
        self.questions = self.hints = 0
        self.played += 1
        return {"outcome": outcome, **self.state()}

    async def create(self, turn):
        """题库里的题都玩过了，现场出一道；出题失败时重出最早玩过的那道。"""
        started = spoke = time.monotonic()
        stage, waited = "story", 0

        async def say(text):
            nonlocal spoke
            spoke = time.monotonic()
            try:
                await turn.say(text)
            except Exception:
                # 进度播报只是告知，发送失败不影响出题。
                logger.warning("出题进度没有播报：%s", text, exc_info=True)

        async def progress(name, attempt):
            nonlocal stage, waited
            stage, waited = name, 0
            text = RETRY if name == "story" and attempt > 1 else PROGRESS.get(name)
            if text:
                await say(text)

        async def waiting():
            nonlocal waited
            while True:
                await asyncio.sleep(1)
                interval = PROGRESS_INTERVALS[min(waited, len(PROGRESS_INTERVALS) - 1)]
                if stage in WAITING and time.monotonic() - spoke >= interval:
                    texts = WAITING[stage]
                    text = texts[waited % len(texts)]
                    waited += 1
                    await say(text.format(seconds=round(time.monotonic() - started)))

        await say("题库里的题你都玩过了，我现场给你出一道新的，先写个故事，大概要一两分钟。")
        reminder = asyncio.create_task(waiting())
        try:
            item = await self.generator.generate(self.library.puzzles(), must=True,
                                                 budget=GENERATE_BUDGET, progress=progress)
            self.library.add(item)
            return item.puzzle, "generated"
        except Exception:
            logger.warning("现场出题失败，改为重出玩过的题", exc_info=True)
            return self.library.oldest_played(), "replayed"
        finally:
            reminder.cancel()

    def reveal(self, turn, outcome):
        self.resolved = True
        self.library.finish(self.puzzle, outcome)
        turn.must_say(self.puzzle.truth)
        return {"outcome": outcome, "truth": self.puzzle.truth, **self.state()}

    async def on_start(self, turn):
        result = await self.serve(turn)
        turn.must_say(self.puzzle.surface)
        return result

    async def on_end(self, turn):
        unfinished = self.puzzle is not None and not self.resolved
        if unfinished:
            self.library.finish(self.puzzle, "unfinished")
            # 没猜完的那道题公布汤底，避免模型自己编一个。
            turn.must_say(self.puzzle.truth)
        self.ended = True
        return {"outcome": "ended", **({"truth": self.puzzle.truth} if unfinished else {}),
                **self.state()}

    def unavailable(self):
        if self.ended:
            return "game_over"
        if self.puzzle is None:
            return "not_started"
        return "puzzle_resolved" if self.resolved else None

    @tool("按汤底回答用户这一轮的是非提问（直接读取用户原话），结果为 yes、no、irrelevant、"
          "not_question 或 unsure；每条用户输入只能判断一次。")
    async def ask_question(self, turn):
        reason = self.unavailable()
        if reason or turn.judged:
            return {"outcome": reason or "already_judged", **self.state()}
        answer = await self.judge.answer(self.puzzle, turn.text)
        if answer in {"not_question", "unsure"}:
            # 没有给出是或不是，不算一次提问，这条输入还可以改为提交推理。
            return {"answer": answer, **self.state()}
        turn.judged = True
        self.questions += 1
        if answer in GESTURES:
            await self.act(Gesture(name=GESTURES[answer]))
        return {"answer": answer, **self.state()}

    @tool("判断用户这一轮说出的推理有没有猜中汤底（直接读取用户原话），结果为 solved、partial、"
          "wrong 或 unsure；猜中时返回汤底。每条用户输入只能判断一次。")
    async def submit_solution(self, turn):
        reason = self.unavailable()
        if reason or turn.judged:
            return {"outcome": reason or "already_judged", **self.state()}
        verdict = await self.judge.verify(self.puzzle, turn.text)
        if verdict == "unsure":
            return {"verdict": verdict, **self.state()}
        turn.judged = True
        if verdict == "solved":
            self.solved += 1
            await self.react("happy", "nod")
            return {"verdict": verdict, **self.reveal(turn, "solved")}
        if verdict == "wrong":
            await self.act(Gesture(name="shake_head"))
        return {"verdict": verdict, **self.state()}

    @tool("取得这道题的下一条提示，每题三条。")
    def get_hint(self):
        reason = self.unavailable()
        if reason:
            return {"outcome": reason, **self.state()}
        if self.hints == len(self.puzzle.hints):
            return {"outcome": "hints_exhausted", **self.state()}
        hint = self.puzzle.hints[self.hints]
        self.hints += 1
        return {"outcome": "hint", "hint": hint, **self.state()}

    @tool("用户放弃时公布汤底，这道题结束。")
    def reveal_truth(self, turn):
        reason = self.unavailable()
        if reason:
            return {"outcome": reason, **self.state()}
        return self.reveal(turn, "revealed")

    @tool("这道题揭晓之后换一道没玩过的题，返回汤面；题都玩过时会现场出一道新题。")
    async def next_puzzle(self, turn):
        if self.ended or self.puzzle is None:
            return {"outcome": "game_over" if self.ended else "not_started", **self.state()}
        if not self.resolved:
            return {"outcome": "puzzle_unresolved", **self.state()}
        result = await self.serve(turn)
        turn.must_say(self.puzzle.surface)
        return result

    async def close(self):
        try:
            await super().close()
        finally:
            try:
                await self.judge.close()
            finally:
                await self.generator.close()
