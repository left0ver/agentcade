from dataclasses import dataclass
from enum import Enum

from games.guess_animal.animals import Animal


class Decision(str, Enum):
    END = "end"
    HINT = "hint"
    SKIP = "skip"
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNCLEAR = "unclear"


@dataclass(frozen=True)
class Reply:
    outcome: str = "unchanged"
    answer: str | None = None
    hint: str | None = None
    advance: bool = False
    prepare: bool = False


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
            raise ValueError("游戏已经开始")
        self.deck = []
        self.index = self.score = self.attempts = 0
        self.hint_index = 0
        self.active = True
        self.resolved = False
        self.revision += 1
        return Reply(outcome="started", prepare=True)

    def set_question(self, animal: Animal) -> None:
        if not self.active or self.resolved or len(self.deck) != self.index:
            raise RuntimeError("当前没有等待生成的题目")
        if any(item.name == animal.name or item.id == animal.id for item in self.deck):
            raise ValueError("同一局的动物不能重复")
        self.deck.append(animal)
        self.hint_index = 0
        self.revision += 1

    def next_hint(self) -> str | None:
        animal = self.animal
        if animal is None:
            raise RuntimeError("提供提示需要当前题目")
        if self.hint_index >= len(animal.hints):
            return None
        hint = animal.hints[self.hint_index]
        self.hint_index += 1
        return hint.rstrip("。！？.!?") + "。"

    def handle(self, decision: Decision, revision: int) -> Reply:
        if revision != self.revision:
            return Reply()
        if not self.active:
            return Reply()
        if decision is Decision.END:
            self.active = False
            self.revision += 1
            return Reply(outcome="ended")
        animal = self.animal
        if self.resolved or animal is None:
            return Reply()
        if decision is Decision.HINT:
            hint = self.next_hint()
            return Reply(outcome="hint" if hint else "hints_exhausted", hint=hint)
        if decision is Decision.CORRECT:
            self.score += 1
            self.resolved = True
            return Reply(outcome="correct", answer=animal.name, advance=True)
        if decision is Decision.SKIP:
            self.resolved = True
            return Reply(outcome="skipped", answer=animal.name, advance=True)
        if decision is Decision.INCORRECT:
            self.attempts += 1
            if self.attempts >= self.max_attempts:
                self.resolved = True
                return Reply(outcome="incorrect", answer=animal.name, advance=True)
            return Reply(outcome="incorrect")
        return Reply(outcome="unclear")

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
            return Reply(outcome="completed")
        return Reply(outcome="next_question", prepare=True)
