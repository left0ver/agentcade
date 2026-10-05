from dataclasses import dataclass

from robot_agents.vector import Stroke, encode_vector

COMMAND_HOTWORDS = ("你画我猜", "猜动物", "提示", "下一题", "结束游戏")


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
