import logging
import secrets
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agentcade.config import user_config_dir

logger = logging.getLogger(__name__)

BUILTIN = Path(__file__).with_name("puzzles.yaml")
# 生成的题和玩过的记录放在用户目录，升级或重装应用时不会丢。
DATA = user_config_dir().parent / "data" / "turtle_soup"


class Puzzle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,39}$")
    title: str = Field(min_length=2, max_length=12)
    surface: str = Field(min_length=10, max_length=100)
    truth: str = Field(min_length=20, max_length=220)
    key: str = Field(min_length=4, max_length=100)
    hints: tuple[str, str, str]
    story: str = Field(min_length=200, max_length=1500)


class PuzzleFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    puzzles: tuple[Puzzle, ...] = Field(min_length=1)


class Generated(BaseModel):
    """生成的一道题，连同题材、关键反转和审核分数。"""
    model_config = ConfigDict(extra="forbid")
    puzzle: Puzzle
    created_at: float
    theme: str
    key_twist: str
    review: dict[str, float]
    passed: bool


class Play(BaseModel):
    model_config = ConfigDict(extra="forbid")
    served_at: float
    outcome: Literal["solved", "revealed", "unfinished"] | None = None


Plays = TypeAdapter(dict[str, Play])


def write_json(path: Path, data: bytes) -> None:
    # 先写临时文件再改名，程序中途退出也不会留下写了一半的文件。
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".pending")
    pending.write_bytes(data)
    pending.replace(path)


def load_builtin(path: Path = BUILTIN) -> tuple[Puzzle, ...]:
    puzzles = PuzzleFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).puzzles
    if len({puzzle.id for puzzle in puzzles}) != len(puzzles):
        raise ValueError(f"题目编号不能重复：{path}")
    return puzzles


class Library:
    """内置题加上生成的题，记录哪些题出过。"""

    def __init__(self, directory: Path = DATA, builtin: Path = BUILTIN):
        self.directory = directory
        self.played_path = directory / "played.json"
        self.builtin = load_builtin(builtin)

    def generated(self) -> list[Generated]:
        items = []
        for path in (self.directory / "puzzles").glob("*.json"):
            try:
                items.append(Generated.model_validate_json(path.read_bytes()))
            except Exception:
                logger.warning("生成的题目 %s 读取失败，跳过", path, exc_info=True)
        return sorted(items, key=lambda item: item.created_at)

    def puzzles(self) -> list[Puzzle]:
        return [*self.builtin, *(item.puzzle for item in self.generated())]

    def plays(self) -> dict[str, Play]:
        if not self.played_path.is_file():
            return {}
        try:
            return Plays.validate_json(self.played_path.read_bytes())
        except Exception:
            logger.warning("玩过的记录 %s 读取失败，当作没有记录", self.played_path, exc_info=True)
            return {}

    def pick(self) -> Puzzle | None:
        """优先出最新生成的没玩过的题，其次从没玩过的内置题里随机抽；都玩过时返回 None。"""
        plays = self.plays()
        fresh = [item.puzzle for item in self.generated() if item.puzzle.id not in plays]
        if fresh:
            return fresh[-1]
        rest = [puzzle for puzzle in self.builtin if puzzle.id not in plays]
        return secrets.choice(rest) if rest else None

    def oldest_played(self) -> Puzzle:
        plays = self.plays()
        return min(self.puzzles(),
                   key=lambda puzzle: plays[puzzle.id].served_at if puzzle.id in plays else 0)

    def serve(self, puzzle: Puzzle) -> None:
        plays = self.plays()
        plays[puzzle.id] = Play(served_at=time.time())
        write_json(self.played_path, Plays.dump_json(plays, indent=2))

    def finish(self, puzzle: Puzzle, outcome: str) -> None:
        plays = self.plays()
        plays[puzzle.id] = Play(served_at=plays[puzzle.id].served_at if puzzle.id in plays
                                else time.time(), outcome=outcome)
        write_json(self.played_path, Plays.dump_json(plays, indent=2))

    def add(self, item: Generated) -> None:
        if item.puzzle.id in {puzzle.id for puzzle in self.puzzles()}:
            raise ValueError(f"题目编号重复：{item.puzzle.id}")
        path = self.directory / "puzzles" / f"{item.puzzle.id}.json"
        write_json(path, item.model_dump_json(indent=2).encode())
