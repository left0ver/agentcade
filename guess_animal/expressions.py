import math
from functools import lru_cache
from typing import Literal

from shapely import LineString, Polygon
from svgelements import SVG, Circle, Close, Color, Line, Move, Path, Rect, Shape

from guess_animal.animals import Point, Stroke, encode_vector
from guess_animal.config import ROOT

ExpressionCode = Literal["009", "111", "141"]
EXPRESSION_FILES = {
    "009": "009-squeeze.svg",
    "111": "111-think-progress.svg",
    "141": "141-sad-sorry.svg",
}
WHITE = Color("white")


@lru_cache(maxsize=3)
def _shapes(code: ExpressionCode) -> tuple[Shape, ...]:
    source = ROOT / "designs" / "eyes" / "D-yanwenzi" / EXPRESSION_FILES[code]
    svg = SVG.parse(str(source))
    if svg.width != 412 or svg.height != 412:
        raise ValueError("表情 SVG 必须使用 412×412 画布")
    return tuple(
        shape
        for shape in svg.elements()
        if isinstance(shape, Shape) and (shape.fill == WHITE or shape.stroke == WHITE)
    )


def _points(shape: Shape) -> tuple[Point, ...]:
    points = []
    for segment in Path(shape):
        if isinstance(segment, Move):
            samples = [segment.end]
        else:
            count = 1 if isinstance(segment, (Line, Close)) else 8
            samples = [segment.point(index / count) for index in range(1, count + 1)]
        for point in samples:
            coordinate = (round(point.x), round(point.y))
            if not points or coordinate != points[-1]:
                points.append(coordinate)
    return tuple(points)


def _filled_path(points: tuple[Point, ...]) -> Stroke:
    polygon = Polygon(points)
    if not polygon.is_valid or polygon.is_empty:
        raise ValueError("表情包含无效的填充轮廓")
    left, top, right, bottom = polygon.bounds
    rows = math.ceil((bottom - top) / 2)
    filled = []
    # SDK 使用折线，在 SVG 填充区域内往返绘制相邻的水平线。
    for row in range(rows):
        y = top + (row + 0.5) * (bottom - top) / rows
        section = polygon.intersection(LineString(((left, y), (right, y))))
        if not isinstance(section, LineString) or section.is_empty:
            raise ValueError("表情填充区域必须具有连续的水平截面")
        coordinates = list(section.coords)
        if row % 2:
            coordinates.reverse()
        filled.extend((round(x), round(y)) for x, y in coordinates)
    return Stroke(tuple(filled), 2)


@lru_cache(maxsize=103)
def expression_strokes(code: ExpressionCode, progress: int = 0) -> tuple[Stroke, ...]:
    if type(progress) is not int or not 0 <= progress <= 100:
        raise ValueError("表情进度必须是 0 到 100 的整数")
    shapes = _shapes(code)
    strokes = []
    for shape in shapes:
        if shape.fill == WHITE:
            if isinstance(shape, Circle):
                center = (round(shape.cx), round(shape.cy))
                strokes.append(Stroke((center, center), round(2 * shape.rx)))
            elif isinstance(shape, Rect) and code == "111":
                if progress == 0:
                    continue
                (track,) = [
                    item for item in shapes if isinstance(item, Rect) and item.stroke == WHITE
                ]
                width = (track.width - 2 * (shape.x - track.x)) * progress / 100
                diameter = min(shape.height, width)
                y = round(shape.y + shape.height / 2)
                strokes.append(
                    Stroke(
                        (
                            (round(shape.x + diameter / 2), y),
                            (round(shape.x + width - diameter / 2), y),
                        ),
                        max(1, round(diameter)),
                    )
                )
            else:
                strokes.append(_filled_path(_points(shape)))
        if shape.stroke == WHITE:
            strokes.append(Stroke(_points(shape), round(shape.stroke_width)))
    result = tuple(strokes)
    encode_vector(result)
    return result


def expression_vector(code: ExpressionCode, progress: int = 0) -> str:
    return encode_vector(expression_strokes(code, progress))


def generation_progress(elapsed: float, estimate: float) -> int:
    if not math.isfinite(elapsed) or elapsed < 0 or not math.isfinite(estimate) or estimate <= 0:
        raise ValueError("生成耗时和预计时间无效")
    # 按预计时间线性增长；仅在生成成功后显示满格。
    return min(99, int(100 * elapsed / estimate))
