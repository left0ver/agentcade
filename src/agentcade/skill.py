from dataclasses import dataclass
from pathlib import Path

import yaml

from agentcade.protocol import GameDescriptor


@dataclass(frozen=True)
class Skill:
    descriptor: GameDescriptor
    instructions: str

    @classmethod
    def load(cls, path: Path) -> "Skill":
        text = path.read_text(encoding="utf-8")
        header, separator, body = text.removeprefix("---\n").partition("\n---\n")
        if not text.startswith("---\n") or not separator or not body.strip():
            raise ValueError(f"SKILL.md 需要 frontmatter 和流程正文：{path}")
        return cls(GameDescriptor.model_validate(yaml.safe_load(header)), body.strip())
