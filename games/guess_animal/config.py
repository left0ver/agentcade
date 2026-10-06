from dataclasses import dataclass, field
from pathlib import Path

from robot_agents.config import LLMSettings, read_environment


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    typesafe_api_key: str = field(repr=False)
    drawing_estimate_seconds: int = 60
    jev_model: str = "jev-latest"
    confidence_threshold: float = 0.65

    def __post_init__(self) -> None:
        if not self.typesafe_api_key.strip() or not self.jev_model.strip():
            raise ValueError("你画我猜需要 TYPESAFE_API_KEY 和 JEV_MODEL")
        if self.drawing_estimate_seconds <= 0:
            raise ValueError("预计等待时间必须大于零")
        if not 0.5 <= self.confidence_threshold <= 1:
            raise ValueError("JEV_CONFIDENCE_THRESHOLD 必须位于 0.5 与 1.0 之间")

    @classmethod
    def from_environment(cls, path: Path | None = None) -> "Settings":
        if path is None:
            path = Path(__file__).with_name(".env")
        values = read_environment(path)
        return cls(
            llm=LLMSettings.from_values(values),
            typesafe_api_key=values["TYPESAFE_API_KEY"],
            drawing_estimate_seconds=int(values.get("DRAWING_ESTIMATE_SECONDS", "60")),
            jev_model=values.get("JEV_MODEL", "jev-latest"),
            confidence_threshold=float(values.get("JEV_CONFIDENCE_THRESHOLD", "0.65")),
        )
