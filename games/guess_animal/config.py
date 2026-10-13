from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from robot_agents.config import AgentSettings, LLMSettings


@dataclass(frozen=True)
class Settings(AgentSettings):
    DIRECTORY: ClassVar[Path] = Path(__file__).parent

    llm: LLMSettings
    typesafe_api_key: str = field(repr=False)
    drawing_estimate_seconds: int
    jev_model: str
    confidence_threshold: float

    def __post_init__(self) -> None:
        if self.drawing_estimate_seconds <= 0:
            raise ValueError("DRAWING_ESTIMATE_SECONDS 必须大于零")
        if not 0.5 <= self.confidence_threshold <= 1:
            raise ValueError("JEV_CONFIDENCE_THRESHOLD 必须位于 0.5 与 1.0 之间")

    @classmethod
    def from_values(cls, values: dict[str, str]) -> "Settings":
        return cls(
            llm=LLMSettings.from_values(values),
            typesafe_api_key=values["TYPESAFE_API_KEY"],
            drawing_estimate_seconds=int(values["DRAWING_ESTIMATE_SECONDS"]),
            jev_model=values["JEV_MODEL"],
            confidence_threshold=float(values["JEV_CONFIDENCE_THRESHOLD"]),
        )
