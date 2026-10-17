from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from agentcade.config import AgentSettings, LLMSettings


@dataclass(frozen=True)
class Settings(AgentSettings):
    DIRECTORY: ClassVar[Path] = Path(__file__).parent

    llm: LLMSettings
    typesafe_api_key: str = field(repr=False)
    jev_model: str
    confidence_threshold: float

    def __post_init__(self) -> None:
        if not 0.5 <= self.confidence_threshold <= 1:
            raise ValueError("JEV_CONFIDENCE_THRESHOLD 必须位于 0.5 与 1.0 之间")

    @classmethod
    def from_values(cls, values: dict[str, str]) -> "Settings":
        return cls(
            llm=LLMSettings.from_values(values),
            typesafe_api_key=values["TYPESAFE_API_KEY"],
            jev_model=values["JEV_MODEL"],
            confidence_threshold=float(values["JEV_CONFIDENCE_THRESHOLD"]),
        )
