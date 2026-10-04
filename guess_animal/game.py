from dataclasses import dataclass
from enum import Enum
from typing import Literal

from guess_animal.animals import Animal


class Decision(str, Enum):
    START = "start"
    END = "end"
    HINT = "hint"
    SKIP = "skip"
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNCLEAR = "unclear"
    IGNORE = "ignore"


@dataclass(frozen=True)
class Reply:
    text: str = ""
    advance: bool = False
    prepare: bool = False
    clear: bool = False
    feedback: Literal["correct", "incorrect"] | None = None


class Game:
    def __init__(self, rounds: int = 5, attempts: int = 2):
        if not 1 <= rounds <= 10 or attempts < 1:
            raise ValueError("题目数量或回答次数无效")
        self.rounds = rounds
        self.max_attempts = attempts
        self.deck: list[Animal] = []
        self.index = 0
        self.score = 0
        self.attempts = 0
        self.hint_index = 0
        self.revision = 0
        self.active = False
        self.resolved = False
        self.silence_count = 0

    @property
    def animal(self) -> Animal | None:
        return self.deck[self.index] if self.active and self.index < len(self.deck) else None

    def start(self) -> Reply:
        if self.active:
            if self.animal is None:
                return Reply("题目正在准备，请稍等。")
            return Reply("游戏正在进行，请说出动物名称，也可以说提示、下一题或结束游戏。")
        self.deck = []
        self.index = self.score = self.attempts = self.silence_count = 0
        self.hint_index = 0
        self.active = True
        self.resolved = False
        self.revision += 1
        return Reply(
            f"我们来玩你画我猜！一共{self.rounds}题，每题可以回答{self.max_attempts}次。"
            "直接说动物名称，也可以说提示、下一题或结束游戏。",
            prepare=True,
        )

    def set_question(self, animal: Animal) -> None:
        if not self.active or self.resolved or len(self.deck) != self.index:
            raise RuntimeError("当前没有等待生成的题目")
        if any(item.name == animal.name or item.id == animal.id for item in self.deck):
            raise ValueError("同一局的动物不能重复")
        self.deck.append(animal)
        self.hint_index = 0
        self.revision += 1

    def next_hint(self) -> str:
        animal = self.animal
        if animal is None:
            raise RuntimeError("提供提示需要当前题目")
        if self.hint_index >= len(animal.hints):
            return "提示已经全部说完了，再看看图案吧。"
        hint = animal.hints[self.hint_index]
        self.hint_index += 1
        return hint.rstrip("。！？.!?") + "。"

    def handle(self, decision: Decision, revision: int) -> Reply:
        if revision != self.revision:
            return Reply()
        self.silence_count = 0
        if decision is Decision.START:
            return self.start()
        if not self.active:
            return Reply()
        if decision is Decision.END:
            self.active = False
            self.revision += 1
            return Reply(
                f"游戏结束，你答对了{self.score}题。想继续时，说我们来玩你画我猜。", clear=True
            )
        animal = self.animal
        if self.resolved or animal is None:
            return Reply()
        if decision is Decision.HINT:
            if self.attempts == 0:
                return Reply("先看图猜一次吧，答错后我会给你提示。")
            return Reply(self.next_hint())
        if decision is Decision.CORRECT:
            self.score += 1
            self.resolved = True
            return Reply(f"答对了，是{animal.name}！", advance=True, feedback="correct")
        if decision is Decision.SKIP:
            self.resolved = True
            return Reply(f"这题的答案是{animal.name}。", advance=True)
        if decision is Decision.INCORRECT:
            self.attempts += 1
            if self.attempts >= self.max_attempts:
                self.resolved = True
                return Reply(f"这题的答案是{animal.name}。", advance=True, feedback="incorrect")
            return Reply(f"还没有答对。{self.next_hint()}", feedback="incorrect")
        return Reply("请说一个动物名称，也可以说提示、下一题或结束游戏。")

    def advance(self) -> Reply:
        if not self.active or not self.resolved:
            raise RuntimeError("当前题目尚未结束")
        self.revision += 1
        self.index += 1
        self.attempts = self.silence_count = 0
        self.hint_index = 0
        self.resolved = False
        if self.index == self.rounds:
            self.active = False
            return Reply(
                f"这一局完成啦！{self.rounds}题里，你答对了{self.score}题。"
                "想继续时，说我们来玩你画我猜。",
                clear=True,
            )
        return Reply(prepare=True)

    def silence(self) -> Reply:
        if not self.active or self.animal is None:
            return Reply()
        self.silence_count += 1
        if self.silence_count >= 3:
            return self.handle(Decision.END, self.revision)
        return Reply("我在等你的回答。你可以说动物名称、提示，或者结束游戏。")
