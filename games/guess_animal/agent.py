from games.guess_animal.config import Settings
from games.guess_animal.game import Decision, Game, Reply
from games.guess_animal.generator import AnimalGenerator
from games.guess_animal.judge import JevJudge
from robot_agents.protocol import (
    GameDescriptor,
    GameRequest,
    GameResult,
    Presentation,
    PublicState,
)
from robot_agents.vector import encode_vector


class GameAgent:
    load_settings = staticmethod(Settings.from_environment)

    descriptor = GameDescriptor(
        id="guess_animal", name="你画我猜", aliases=["猜动物", "画画猜动物"],
        description="机器人画动物，玩家猜名称。每局五题，每题两次机会，可以要提示或跳题。",
        required_capabilities=["expression.vector_accessory.v1", "motion"],
    )

    def __init__(self, settings: Settings):
        self.game = Game()
        self.generator = AnimalGenerator(settings)
        self.judge = JevJudge(settings)
        self.estimate = settings.drawing_estimate_seconds
        self.phase = "等待开始"
        self.history: list[dict] = []

    def state(self):
        status = (f"第{self.game.index + 1}题，得分{self.game.score}，已回答{self.game.attempts}次"
                  if self.game.active else f"游戏结束，答对了{self.game.score}题。")
        return PublicState(
            phase=self.phase,
            status=status,
            commands=["回答动物名称", "提示", "下一题", "结束游戏"],
            revision=self.game.revision, active=self.game.active,
            idle_seconds=20 if self.phase == "等待猜测" and not self.game.resolved else None,
        )

    def present(self, reply: Reply) -> GameResult:
        outputs = []
        if reply.text or reply.clear:
            outputs.append(Presentation(
                text=reply.text, clear=reply.clear,
                expression={"correct": "happy", "incorrect": "sorry"}.get(reply.feedback),
                gesture={"correct": "nod", "incorrect": "shake_head"}.get(reply.feedback),
                restore_path=(encode_vector(self.game.animal.strokes)
                              if reply.feedback == "incorrect" else None),
            ))
        next_action = None
        if reply.prepare:
            self.phase = "生成题目"
            outputs.append(Presentation(
                text=(f"正在生成第{self.game.index + 1}题，大约需要{self.estimate}秒。"
                      "准备好了我会告诉你，请稍等。"),
                expression="thinking", progress_seconds=self.estimate,
            ))
            next_action = "prepare"
        elif reply.advance:
            self.phase = "播报答案"
            next_action = "advance"
        elif not self.game.active:
            self.phase = "已结束"
        return GameResult(state=self.state(), outputs=outputs, next_action=next_action)

    async def handle(self, request: GameRequest) -> GameResult:
        if request.action == "start":
            return self.present(self.game.start())
        if request.action == "end":
            return self.present(self.game.handle(Decision.END, self.game.revision))
        if request.revision != self.game.revision:
            return GameResult(state=self.state(), outputs=[Presentation(
                text="刚才的题目已经结束，请看当前图案再回答。",
            )])
        if request.action == "prepare":
            if self.phase != "生成题目":
                raise ValueError("当前阶段不能生成题目")
            animal, = await self.generator.generate(1)
            self.game.set_question(animal)
            self.phase = "等待猜测"
            return GameResult(state=self.state(), outputs=[Presentation(
                paths=list(animal.drawing_steps()),
                text=f"第{self.game.index + 1}题画好了！请看图，说出动物名称。",
            )])
        if request.action == "advance":
            return self.present(self.game.advance())
        if request.action == "timeout":
            return self.present(self.game.silence())
        decision = await self.judge.decide(
            request.text, self.game.animal, game_active=self.game.active,
        )
        self.history.append({"text": request.text, "decision": decision.value})
        self.history = self.history[-24:]
        if decision is Decision.CHAT:
            self.game.silence_count = 0
            return GameResult(state=self.state(), handled=False)
        if decision is Decision.IGNORE:
            return GameResult(state=self.state())
        reply = self.game.handle(decision, self.game.revision)
        if self.game.animal is None and not reply.text and not reply.prepare:
            reply = Reply("题目还在准备，你可以继续和我聊天。")
        return self.present(reply)

    async def close(self):
        try:
            await self.generator.close()
        finally:
            await self.judge.close()
