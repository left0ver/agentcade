import struct
from dataclasses import dataclass

Point = tuple[int, int]
COMMAND_HOTWORDS = ("你画我猜", "猜动物", "提示", "下一题", "结束游戏")


@dataclass(frozen=True)
class Stroke:
    points: tuple[Point, ...]
    width: int = 6

    def __post_init__(self) -> None:
        if not 1 <= self.width <= 48 or not 1 <= len(self.points) <= 48:
            raise ValueError("笔画宽度或坐标数量超出 SDK 限制")
        if any(not 0 <= coordinate <= 411 for point in self.points for coordinate in point):
            raise ValueError("图案坐标超出屏幕范围")


def encode_vector(strokes: tuple[Stroke, ...]) -> str:
    if not 1 <= len(strokes) <= 12 or sum(len(stroke.points) for stroke in strokes) > 192:
        raise ValueError("图案超出 SDK 的笔画或坐标总量限制")
    data = bytearray((1, len(strokes)))
    for stroke in strokes:
        data.extend((stroke.width, len(stroke.points)))
        for x, y in stroke.points:
            data.extend(struct.pack(">HH", x, y))
    return data.hex()


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
