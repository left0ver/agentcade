from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from robot_agents.config import AgentSettings, LLMSettings


@dataclass(frozen=True)
class Settings(AgentSettings):
    DIRECTORY: ClassVar[Path] = Path(__file__).parent

    llm: LLMSettings

    @classmethod
    def from_values(cls, values: dict[str, str]) -> "Settings":
        return cls(llm=LLMSettings.from_values(values))
