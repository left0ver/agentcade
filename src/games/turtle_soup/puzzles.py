import secrets
from collections import deque
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

PATH = Path(__file__).with_name("puzzles.yaml")
# 最近出过的题，同一次运行里新开的局优先出别的题。
recent: deque[str] = deque(maxlen=8)


class Puzzle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=1)
    surface: str = Field(min_length=1, max_length=200)
    truth: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=1)
    hints: tuple[str, str, str]


class PuzzleFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    puzzles: tuple[Puzzle, ...] = Field(min_length=1)


def load_puzzles(path: Path = PATH) -> tuple[Puzzle, ...]:
    puzzles = PuzzleFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).puzzles
    if len({puzzle.id for puzzle in puzzles}) != len(puzzles):
        raise ValueError(f"题目编号不能重复：{path}")
    return puzzles


def pick(puzzles: tuple[Puzzle, ...], played: set[str]) -> Puzzle | None:
    """随机选一道这一局还没出过的题，尽量避开最近几局出过的；都出过时返回 None。"""
    fresh = [puzzle for puzzle in puzzles if puzzle.id not in played]
    if not fresh:
        return None
    unseen = [puzzle for puzzle in fresh if puzzle.id not in recent] or fresh
    puzzle = unseen[secrets.randbelow(len(unseen))]
    recent.append(puzzle.id)
    return puzzle
