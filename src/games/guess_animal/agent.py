import logging
from pathlib import Path

from agentcade.protocol import Draw, Progress
from agentcade.skill import Skill
from agentcade.skill_agent import SkillAgent
from agentcade.tools import NoInput, Tool, original_text_input

from .config import Settings
from .game import Decision, Game
from .generator import AnimalGenerator
from .judge import JevJudge

logger = logging.getLogger(__name__)

GENERATION_ATTEMPTS = 3


class GameAgent(SkillAgent):
    skill = Skill.load(Path(__file__).with_name("SKILL.md"))
    config = Settings

    def __init__(self, settings):
        super().__init__(settings)
        self.game = Game()
        self.generator = AnimalGenerator(settings)
        self.judge = JevJudge(settings)
        self.estimate = settings.drawing_estimate_seconds

    def phase(self):
        game = self.game
        if not game.active:
            return "已结束" if game.revision else "等待开始"
        if game.animal is None:
            return "等待出题"
        return "等待下一题" if game.resolved else "等待猜测"

    def state(self):
        game = self.game
        return {
            "phase": self.phase(), "round": min(game.index + 1, game.rounds),
            "total_rounds": game.rounds, "score": game.score, "attempts": game.attempts,
            "remaining_attempts": game.max_attempts - game.attempts,
        }

    def facts(self, reply):
        # 答案只在题目结束时出现在规则结果里，这里原样转给模型。
        public = {"outcome": reply.outcome, "answer": reply.answer, "hint": reply.hint}
        return {**{name: value for name, value in public.items() if value is not None},
                **self.state()}

    async def generate(self, turn):
        """出一题并画到屏幕上：先请用户稍等、显示进度，画好后直接展示。"""
        # 生成需要较长时间，先回复一句让主 Agent 恢复收音。
        if not turn.spoken:
            await turn.say("稍等一下，我来画一只动物。")
        try:
            await self.act(Progress(seconds=self.estimate))
        except Exception:
            # 进度画面只是提示，显示失败不影响出题。
            logger.warning("显示进度失败", exc_info=True)
        for attempt in range(1, GENERATION_ATTEMPTS + 1):
            try:
                animal, = await self.generator.generate(1)
                self.game.set_question(animal)
            except Exception as error:
                # 模型偶尔画出不合格的图案或者超时，重新生成；全部失败后题目保持待出状态。
                logger.warning("生成题目失败（%s/%s）：%s", attempt, GENERATION_ATTEMPTS,
                               error, exc_info=True)
                continue
            await self.act(Draw(paths=list(self.game.animal.drawing_steps())))
            return {"outcome": "question_ready", **self.state()}
        return {"outcome": "generation_failed", **self.state()}

    def announce(self, turn, answer):
        """一题结束、用户没猜出来时，答案一定要说出来，避免模型自己编一个。"""
        turn.must_say(answer, fallback=f"这题的答案是{answer}。")

    async def on_start(self, turn):
        self.game.start()
        return await self.generate(turn)

    async def on_end(self, turn):
        animal = self.game.animal
        unfinished = animal is not None and not self.game.resolved
        self.game.handle(Decision.END, self.game.revision)
        if unfinished:
            self.announce(turn, animal.name)
        return {"outcome": "ended", **({"answer": animal.name} if unfinished else {}),
                **self.state()}

    def tools(self, turn):
        answered = False

        def unavailable(*, resolved=False):
            if not self.game.active:
                return "game_over"
            if self.game.animal is None:
                return "question_not_ready"
            if self.game.resolved != resolved:
                return "question_resolved" if self.game.resolved else "question_unresolved"
            return None

        async def get_hint():
            reason = unavailable()
            if reason:
                return {"outcome": reason, **self.state()}
            return self.facts(self.game.handle(Decision.HINT, self.game.revision))

        async def submit_answer(text):
            nonlocal answered
            reason = unavailable()
            if reason or answered:
                return {"outcome": reason or "already_answered", **self.state()}
            decision = await self.judge.decide(text, self.game.animal)
            answered = True
            if decision not in {Decision.CORRECT, Decision.INCORRECT}:
                return {"outcome": "unclear", **self.state()}
            reply = self.game.handle(decision, self.game.revision)
            # 答对点头并显示开心，答错摇头并显示抱歉，由程序在判定时直接触发。
            if decision is Decision.CORRECT:
                await self.react("happy", "nod")
            else:
                await self.react("sorry", "shake_head")
                if reply.answer:
                    self.announce(turn, reply.answer)
            if self.game.resolved and self.game.index + 1 == self.game.rounds:
                self.game.advance()
                turn.end_session()
            return self.facts(reply)

        async def skip_question():
            reason = unavailable()
            if reason:
                return {"outcome": reason, **self.state()}
            reply = self.game.handle(Decision.SKIP, self.game.revision)
            if self.game.index + 1 == self.game.rounds:
                self.game.advance()
                turn.end_session()
                self.announce(turn, reply.answer)
                return self.facts(reply)
            # 跳过后直接出下一题，用户不用再说一次：先公布答案，再生成。
            said = f"这题的答案是{reply.answer}。我来画下一题，稍等一下。"
            await turn.say(said)
            self.game.advance()
            return {**await self.generate(turn), "skipped_answer": reply.answer, "said": said}

        async def next_question():
            # 上一次没有生成成功时，题目仍然待出，直接重新生成。
            if self.game.active and self.game.animal is None:
                return await self.generate(turn)
            reason = unavailable(resolved=True)
            if reason:
                return {"outcome": reason, **self.state()}
            self.game.advance()
            return await self.generate(turn)

        return [
            Tool("get_hint", "取得当前题目的下一条提示。", NoInput, get_hint),
            Tool("submit_answer", "判定用户的动物答案并更新得分和机会；每条用户输入只能提交一次。",
                 original_text_input(turn.text), submit_answer),
            Tool("skip_question", "跳过当前题目：把答案告诉用户，然后直接画好下一题并展示，需要等待一段时间；"
                 "跳过最后一题则结束整局。", NoInput, skip_question),
            Tool("next_question", "当前题目结束后画好下一题并展示，需要等待一段时间。",
                 NoInput, next_question),
        ]

    async def close(self):
        try:
            await super().close()
        finally:
            try:
                await self.generator.close()
            finally:
                await self.judge.close()
