from dataclasses import dataclass
from enum import Enum
from typing import Literal

from games.guess_animal.animals import Animal


class Decision(str, Enum):
    START = "start"
    END = "end"
    HINT = "hint"
    SKIP = "skip"
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNCLEAR = "unclear"
    RULES = "rules"
    STATUS = "status"


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

    @property
    def animal(self) -> Animal | None:
        return self.deck[self.index] if self.active and self.index < len(self.deck) else None

    def start(self) -> Reply:
        if self.active:
            if self.animal is None:
                return Reply("我还在准备这幅画，画好就告诉你。")
            return Reply("我们正在猜这幅画呢。你觉得是什么动物？也可以要个提示，或者跳过这题。")
        self.deck = []
        self.index = self.score = self.attempts = 0
        self.hint_index = 0
        self.active = True
        self.resolved = False
        self.revision += 1
        return Reply(
            f"来玩你画我猜吧！我来画，你来猜，一共{self.rounds}题，每题有{self.max_attempts}次机会。"
            "想要提示或者跳过，直接告诉我就好。",
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
            return "提示都给你啦，再看看这幅画，想想是什么动物。"
        hint = animal.hints[self.hint_index]
        self.hint_index += 1
        return hint.rstrip("。！？.!?") + "。"

    def handle(self, decision: Decision, revision: int) -> Reply:
        if revision != self.revision:
            return Reply()
        if decision is Decision.START:
            return self.start()
        if not self.active:
            return Reply()
        if decision is Decision.END:
            self.active = False
            self.revision += 1
            return Reply(
                f"好，游戏结束啦。这次你答对了{self.score}题。", clear=True
            )
        animal = self.animal
        if self.resolved or animal is None:
            return Reply()
        if decision is Decision.HINT:
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
            return Reply(f"再想想，给你个提示：{self.next_hint()}", feedback="incorrect")
        return Reply("你觉得是什么动物？说一个名字就好，也可以要个提示，或者跳过这题。")

    def advance(self) -> Reply:
        if not self.active or not self.resolved:
            raise RuntimeError("当前题目尚未结束")
        self.revision += 1
        self.index += 1
        self.attempts = 0
        self.hint_index = 0
        self.resolved = False
        if self.index == self.rounds:
            self.active = False
            return Reply(
                f"这一局{self.rounds}题都猜完啦！你答对了{self.score}题。",
                clear=True,
            )
        return Reply(prepare=True)
