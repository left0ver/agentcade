from dataclasses import dataclass

from agentcade.vector import Stroke, encode_vector


@dataclass(frozen=True)
class Animal:
    id: str
    name: str
    aliases: tuple[str, ...]
    hints: tuple[str, ...]
    strokes: tuple[Stroke, ...]

    def __post_init__(self) -> None:
        encode_vector(self.strokes)

    def drawing_steps(self) -> tuple[str, ...]:
        return tuple(
            encode_vector(self.strokes[:count]) for count in range(1, len(self.strokes) + 1)
        )
