import asyncio

from games.guess_animal.config import Settings
from games.guess_animal.game import Decision, Game, Reply
from games.guess_animal.generator import AnimalGenerator
from games.guess_animal.judge import JevJudge
from robot_agents.game_tools import GAME_TOOL_PROMPT, GameTurn
from robot_agents.protocol import (
    ExpectedInput,
    GameDescriptor,
    GameResult,
    Presentation,
    PublicState,
)
from robot_agents.providers import OpenAIProvider
from robot_agents.tools import NoInput, Tool, ToolRegistry, original_text_input
from robot_agents.vector import encode_vector

SYSTEM_PROMPT = """你是你画我猜游戏 Agent，机器人画动物，用户猜名称，每局五题，每题两次机会。
用户作答时调用 submit_answer，text 必须传递用户完整原文，答案由工具判定。
请求提示调用 get_hint，规则调用 get_rules，进度或继续游戏调用 get_game_state。
当前题目尚未结束时，用户要求下一题、跳过、公布答案或说不知道，调用 skip_question。
当前题目已经结束且用户要求下一题时调用 next_question。
生成题目期间只能查询规则、进度或结束游戏，不能作答、提示或者切换题目。
""" + GAME_TOOL_PROMPT


class GameAgent:
    load_settings = staticmethod(Settings.from_environment)
    descriptor = GameDescriptor(
        id="guess_animal", name="你画我猜", aliases=["猜动物", "画画猜动物"],
        description="机器人画动物，玩家猜名称。每局五题，每题两次机会，可以要提示或跳题。",
        required_capabilities=["expression.vector_accessory.v1", "motion"],
    )

    def __init__(self, settings):
        self.game = Game()
        self.provider = OpenAIProvider(settings.llm)
        self.generator = AnimalGenerator(settings)
        self.judge = JevJudge(settings)
        self.estimate = settings.drawing_estimate_seconds
        self.phase = "等待开始"
        self.history = []
        self.lock = asyncio.Lock()
        self.last_tool_calls = []

    def state(self):
        game = self.game
        if not game.active:
            commands = []
        elif self.phase == "生成题目":
            commands = ["查询进度", "查询规则", "结束游戏"]
        elif game.resolved:
            commands = ["下一题", "结束游戏"]
        else:
            commands = ["回答动物名称", "提示", "跳过这题", "结束游戏"]
        return PublicState(
            phase=self.phase, revision=game.revision, active=game.active,
            status=(f"第{game.index + 1}题，得分{game.score}，已回答{game.attempts}次"
                    if game.active else f"游戏结束，答对了{game.score}题。"),
            commands=commands,
            details={"round": min(game.index + 1, game.rounds), "total_rounds": game.rounds,
                     "score": game.score, "attempts": game.attempts,
                     "remaining_attempts": game.max_attempts - game.attempts,
                     "resolved": game.resolved},
        )

    def expected_input(self):
        if not self.game.active:
            return ExpectedInput(kind="none", description="本局已经结束，无需继续回答。")
        if self.phase == "生成题目":
            return ExpectedInput(kind="wait", description="题目正在生成，请等待，也可以查询进度或结束游戏。",
                                 examples=["现在准备好了吗", "结束游戏"])
        if self.game.resolved:
            return ExpectedInput(kind="command", description="当前题目已经结束，请说下一题或结束游戏。",
                                 examples=["下一题", "结束游戏"])
        return ExpectedInput(kind="answer", description="请根据图案说出动物名称，也可以请求提示或跳题。",
                             examples=["我猜是某种动物", "给我提示", "跳过这题", "结束游戏"])

    def result(self, outputs=()):
        return GameResult(state=self.state(), expected_input=self.expected_input(), outputs=list(outputs))

    def present(self, reply: Reply):
        outputs = []
        if reply.text or reply.clear:
            outputs.append(Presentation(
                text=reply.text, clear=reply.clear,
                expression={"correct": "happy", "incorrect": "sorry"}.get(reply.feedback),
                gesture={"correct": "nod", "incorrect": "shake_head"}.get(reply.feedback),
                restore_path=(encode_vector(self.game.animal.strokes)
                              if reply.feedback == "incorrect" and self.game.animal else None),
            ))
        if reply.prepare:
            self.phase = "生成题目"
            outputs.append(Presentation(
                text=(f"正在生成第{self.game.index + 1}题，大约需要{self.estimate}秒。"
                      "准备好了我会告诉你，请稍等。"),
                expression="thinking", progress_seconds=self.estimate,
            ))
        elif reply.advance:
            self.phase = "等待下一题"
            outputs.append(Presentation(text="准备好以后，请说下一题。"))
        elif not self.game.active:
            self.phase = "已结束"
        return self.result(outputs)

    async def generate_question(self, turn, progress):
        await turn.notify(progress)
        animal, = await self.generator.generate(1)
        async with self.lock:
            self.game.set_question(animal)
            self.phase = "等待猜测"
            return turn.stage(self.result([Presentation(
                paths=list(animal.drawing_steps()),
                text=f"第{self.game.index + 1}题画好了！请看图，说出动物名称。",
            )]))

    def check_input(self, turn, *, resolved=False):
        turn.claim()
        if not self.game.active:
            raise ValueError("游戏会话已经结束")
        if turn.request.revision != self.game.revision:
            return "刚才的题目已经结束，请看当前图案再回答。"
        if self.game.animal is None:
            return "题目还在准备，请等待，也可以询问进度。"
        if self.game.resolved != resolved:
            return ("这道题已经结束，请说下一题。" if self.game.resolved
                    else "当前题目尚未结束，可以回答或者要求跳过。")
        return None

    def tools_for(self, turn):
        async def start_game():
            async with self.lock:
                turn.claim("start")
                if self.game.revision:
                    raise ValueError("游戏已经开始")
                progress = self.present(self.game.start())
            return await self.generate_question(turn, progress)

        async def get_rules():
            return turn.stage(self.result([Presentation(text=self.descriptor.description)]))

        async def get_game_state():
            return turn.stage(self.result([Presentation(text=self.state().status + "。" + self.phase + "。")]))

        async def get_hint():
            async with self.lock:
                message = self.check_input(turn)
                if message:
                    return turn.stage(self.result([Presentation(text=message)]))
                reply = self.game.handle(Decision.HINT, self.game.revision)
                self.game.revision += 1
                return turn.stage(self.present(reply))

        async def submit_answer(text):
            async with self.lock:
                if text != turn.request.text:
                    raise ValueError("submit_answer 必须传递用户消息的完整原文")
                message = self.check_input(turn)
                if message:
                    return turn.stage(self.result([Presentation(text=message)]))
                decision = await self.judge.decide(text, self.game.animal, game_active=True)
                if decision not in {Decision.CORRECT, Decision.INCORRECT}:
                    return turn.stage(self.result([Presentation(text="请明确说出一个动物名称。")]))
                reply = self.game.handle(decision, self.game.revision)
                self.game.revision += 1
                result = self.present(reply)
                if self.game.resolved and self.game.index + 1 == self.game.rounds:
                    feedback = result.outputs[:1]
                    result = self.present(self.game.advance())
                    result.outputs = feedback + result.outputs
                return turn.stage(result)

        async def skip_question():
            async with self.lock:
                message = self.check_input(turn)
                if message:
                    return turn.stage(self.result([Presentation(text=message)]))
                reply = self.game.handle(Decision.SKIP, self.game.revision)
                progress = self.present(self.game.advance())
                progress.outputs.insert(0, Presentation(text=reply.text))
                if not self.game.active:
                    return turn.stage(progress)
            return await self.generate_question(turn, progress)

        async def next_question():
            async with self.lock:
                message = self.check_input(turn, resolved=True)
                if message:
                    return turn.stage(self.result([Presentation(text=message)]))
                progress = self.present(self.game.advance())
                if not self.game.active:
                    return turn.stage(progress)
            return await self.generate_question(turn, progress)

        async def end_game():
            async with self.lock:
                turn.claim("end")
                if self.game.active:
                    reply = self.game.handle(Decision.END, self.game.revision)
                else:
                    reply = Reply("游戏已经结束。", clear=True)
                return turn.stage(self.present(reply))

        return ToolRegistry([
            Tool("start_game", "初始化游戏并生成第一题，生成期间报告进度。", NoInput, start_game),
            Tool("get_rules", "查询游戏规则。", NoInput, get_rules),
            Tool("get_game_state", "查询当前进度、得分和下一步输入要求。", NoInput, get_game_state),
            Tool("get_hint", "取得下一条提示并更新提示使用记录。", NoInput, get_hint),
            Tool("submit_answer", "判定用户的动物答案并更新得分、机会和题目状态。",
                 original_text_input(turn.request.text), submit_answer),
            Tool("skip_question", "公布当前答案并生成下一题，最后一题结束整局。", NoInput, skip_question),
            Tool("next_question", "当前题目结束后生成下一题。", NoInput, next_question),
            Tool("end_game", "结束游戏并返回得分。请求处理层负责取消未完成的生成任务。", NoInput, end_game),
            turn.message_tool(),
        ], terminal={"send_message"})

    async def handle(self, request, notify):
        if request.action == "input":
            if not self.game.active:
                raise ValueError("游戏会话已经结束")
            if request.revision != self.game.revision:
                return self.result([Presentation(text="刚才的题目已经结束，请看当前图案再回答。")])
        turn = GameTurn(request, notify, self.result)
        registry = self.tools_for(turn)
        await self.provider.run_tools(SYSTEM_PROMPT, {
            "request": request.model_dump(), "history": self.history[-12:],
            "game": self.result().model_dump(),
        }, registry)
        self.last_tool_calls = registry.calls
        result = turn.finish()
        self.history.append({"utterance": request.text, "state": result.state.model_dump(),
                             "messages": [output.text for output in result.outputs]})
        self.history = self.history[-12:]
        return result

    async def close(self):
        try:
            await self.provider.close()
        finally:
            try:
                await self.generator.close()
            finally:
                await self.judge.close()
