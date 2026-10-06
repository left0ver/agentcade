from dataclasses import dataclass
from pathlib import Path

from robot_agents.config import LLMSettings, read_environment


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings

    @classmethod
    def from_environment(cls, path: Path | None = None) -> "Settings":
        if path is None:
            path = Path(__file__).with_name(".env")
        return cls(llm=LLMSettings.from_values(read_environment(path)))
