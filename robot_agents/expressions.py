import math
from functools import lru_cache
from typing import Literal

from shapely import LineString, Polygon
from svgelements import SVG, Circle, Close, Color, Line, Move, Path, Rect, Shape

from robot_agents.config import ROOT
from robot_agents.vector import Point, Stroke, encode_vector

ExpressionCode = Literal["009", "111", "141"]
EXPRESSION_FILES = {
    "009": "009-squeeze.svg",
    "111": "111-think-progress.svg",
    "141": "141-sad-sorry.svg",
}
WHITE = Color("white")


@lru_cache(maxsize=3)
def _shapes(code: ExpressionCode) -> tuple[Shape, ...]:
    source = ROOT / "robot_agents" / "assets" / "emojis" / EXPRESSION_FILES[code]
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


@lru_cache(maxsize=3)
def expression_strokes(code: ExpressionCode) -> tuple[Stroke, ...]:
    shapes = _shapes(code)
    strokes = []
    for shape in shapes:
        if shape.fill == WHITE:
            if isinstance(shape, Circle):
                center = (round(shape.cx), round(shape.cy))
                strokes.append(Stroke((center, center), round(2 * shape.rx)))
            elif isinstance(shape, Rect) and code == "111":
                # 进度填充由 segmented_progress_strokes 按格数绘制。
                continue
            else:
                strokes.append(_filled_path(_points(shape)))
        if shape.stroke == WHITE:
            strokes.append(Stroke(_points(shape), round(shape.stroke_width)))
    result = tuple(strokes)
    encode_vector(result)
    return result


def expression_vector(code: ExpressionCode) -> str:
    return encode_vector(expression_strokes(code))


@lru_cache(maxsize=11)
def segmented_progress_strokes(step: int) -> tuple[Stroke, ...]:
    if type(step) is not int or not 0 <= step <= 10:
        raise ValueError("进度格数必须是 0 到 10 的整数")
    strokes = list(expression_strokes("111"))
    # 填充线沿外框底边连接，每组最多三格，符合 SDK 的笔画和坐标限制。
    for first in range(0, step, 3):
        points = []
        for index in range(first, min(first + 3, step)):
            left = 139 + index * 14
            right = left + 7
            points.append((left, 101))
            for row, y in enumerate(range(88, 97, 2)):
                points.extend(((left, y), (right, y)) if row % 2 == 0
                              else ((right, y), (left, y)))
            points.append((right, 101))
        strokes.append(Stroke(tuple(points), 2))
    result = tuple(strokes)
    encode_vector(result)
    return result


def segmented_progress_vector(step: int) -> str:
    return encode_vector(segmented_progress_strokes(step))
