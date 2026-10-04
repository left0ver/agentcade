import math
import struct
from dataclasses import dataclass

Point = tuple[int, int]


@dataclass(frozen=True)
class Stroke:
    points: tuple[Point, ...]
    width: int = 6

    def __post_init__(self) -> None:
        if not 1 <= self.width <= 48 or not 1 <= len(self.points) <= 48:
            raise ValueError("笔画宽度或坐标数量超出 SDK 限制")
        if any(not 0 <= coordinate <= 411 for point in self.points for coordinate in point):
            raise ValueError("图案坐标超出屏幕范围")


def line(*points: Point, width: int = 6) -> Stroke:
    return Stroke(tuple(points), width)


def ellipse(x: int, y: int, rx: int, ry: int, *, width: int = 6) -> Stroke:
    return Stroke(
        tuple(
            (
                round(x + rx * math.cos(i * math.tau / 20)),
                round(y + ry * math.sin(i * math.tau / 20)),
            )
            for i in range(21)
        ),
        width,
    )


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
    hint: str
    strokes: tuple[Stroke, ...]

    def __post_init__(self) -> None:
        encode_vector(self.strokes)

    def drawing_steps(self) -> tuple[str, ...]:
        return tuple(
            encode_vector(self.strokes[:count]) for count in range(1, len(self.strokes) + 1)
        )


FACE = ellipse(206, 230, 125, 112)
NOSE = line((192, 256), (220, 256), (206, 269), (192, 256))
MOUTH = line((206, 270), (206, 287), (188, 298), (177, 293))
MOUTH_RIGHT = line((206, 287), (224, 298), (235, 293))
ROUND_EARS = (ellipse(111, 113, 38, 40), ellipse(301, 113, 38, 40))

ANIMALS = (
    Animal(
        "cat",
        "猫",
        ("小猫", "猫咪", "喵星人"),
        "它会喵喵叫，脸上长着胡须。",
        (
            line((91, 181), (82, 66), (162, 123)),
            line((250, 123), (330, 66), (321, 181)),
            FACE,
            NOSE,
            MOUTH,
            MOUTH_RIGHT,
            line((153, 269), (75, 252)),
            line((153, 286), (71, 293)),
            line((259, 269), (337, 252)),
            line((259, 286), (341, 293)),
        ),
    ),
    Animal(
        "rabbit",
        "兔子",
        ("兔", "小兔", "小兔子", "白兔"),
        "它有很长的耳朵，喜欢蹦蹦跳跳。",
        (
            ellipse(149, 86, 29, 68),
            ellipse(263, 86, 29, 68),
            FACE,
            NOSE,
            MOUTH,
            MOUTH_RIGHT,
            line((193, 299), (193, 322), (218, 322), (218, 299)),
            line((206, 300), (206, 322)),
        ),
    ),
    Animal(
        "dog",
        "狗",
        ("小狗", "狗狗", "汪星人"),
        "它会汪汪叫，经常陪人们散步。",
        (
            line((119, 135), (74, 119), (43, 183), (58, 256), (86, 264), (114, 177)),
            line((293, 135), (338, 119), (369, 183), (354, 256), (326, 264), (298, 177)),
            FACE,
            ellipse(206, 266, 23, 15),
            MOUTH,
            MOUTH_RIGHT,
            line((206, 299), (208, 326), (226, 333), (242, 326), (244, 301)),
        ),
    ),
    Animal(
        "pig",
        "猪",
        ("小猪", "猪猪"),
        "它有圆圆的鼻子，鼻子上有两个鼻孔。",
        (
            line((106, 160), (82, 88), (161, 125)),
            line((251, 125), (330, 88), (306, 160)),
            FACE,
            ellipse(206, 280, 55, 34),
            ellipse(185, 280, 7, 12),
            ellipse(227, 280, 7, 12),
        ),
    ),
    Animal(
        "bear",
        "熊",
        ("小熊", "棕熊", "狗熊"),
        "它有圆耳朵和强壮的身体，有些种类喜欢蜂蜜。",
        (
            *ROUND_EARS,
            FACE,
            ellipse(206, 279, 52, 38),
            NOSE,
            MOUTH,
            MOUTH_RIGHT,
        ),
    ),
    Animal(
        "panda",
        "熊猫",
        ("大熊猫", "国宝熊猫"),
        "它爱吃竹子，眼睛周围有黑眼圈。",
        (
            *ROUND_EARS,
            FACE,
            ellipse(139, 211, 38, 44, width=14),
            ellipse(273, 211, 38, 44, width=14),
            NOSE,
            MOUTH,
            MOUTH_RIGHT,
        ),
    ),
    Animal(
        "fox",
        "狐狸",
        ("小狐狸", "狐"),
        "它有尖耳朵、尖嘴巴，还有蓬松的大尾巴。",
        (
            line((84, 175), (77, 52), (167, 122)),
            line((245, 122), (335, 52), (328, 175)),
            line((167, 122), (206, 113), (245, 122)),
            line((84, 175), (65, 253), (105, 257), (206, 339), (307, 257), (347, 253), (328, 175)),
            line((92, 229), (149, 271), (206, 322), (263, 271), (320, 229)),
            line((190, 308), (222, 308), (206, 328), (190, 308)),
        ),
    ),
    Animal(
        "tiger",
        "老虎",
        ("虎", "小老虎", "大老虎"),
        "它身上有条纹，额头的花纹像一个王字。",
        (
            *ROUND_EARS,
            FACE,
            NOSE,
            MOUTH,
            MOUTH_RIGHT,
            line((177, 144), (235, 144)),
            line((185, 162), (227, 162)),
            line((177, 180), (235, 180)),
            line((206, 139), (206, 180)),
            line((87, 249), (125, 267), (93, 281)),
            line((325, 249), (287, 267), (319, 281)),
        ),
    ),
    Animal(
        "cow",
        "牛",
        ("奶牛", "小牛", "黄牛"),
        "它头上长角，会哞哞叫，也会吃青草。",
        (
            line((121, 135), (95, 77), (122, 96), (146, 119)),
            line((291, 135), (317, 77), (290, 96), (266, 119)),
            line((93, 166), (43, 151), (58, 187), (85, 199)),
            line((319, 166), (369, 151), (354, 187), (327, 199)),
            FACE,
            ellipse(206, 287, 66, 38),
            ellipse(181, 284, 7, 9),
            ellipse(231, 284, 7, 9),
        ),
    ),
    Animal(
        "elephant",
        "大象",
        ("象", "小象", "亚洲象", "非洲象"),
        "它有大耳朵，还有能卷起东西的长鼻子。",
        (
            ellipse(74, 207, 52, 88),
            ellipse(338, 207, 52, 88),
            line(
                (104, 266),
                (92, 211),
                (101, 161),
                (144, 128),
                (206, 114),
                (268, 128),
                (311, 161),
                (320, 211),
                (308, 266),
            ),
            line((104, 266), (155, 298), (179, 291)),
            line((308, 266), (257, 298), (235, 291)),
            line(
                (179, 249),
                (179, 339),
                (193, 369),
                (226, 382),
                (259, 369),
                (268, 347),
                (249, 335),
                (235, 351),
                (225, 336),
                (235, 249),
            ),
            line((190, 295), (221, 300)),
            line((190, 316), (219, 321)),
        ),
    ),
)

HOTWORDS = tuple(animal.name for animal in ANIMALS) + (
    "你画我猜",
    "猜动物",
    "提示",
    "下一题",
    "结束游戏",
)
