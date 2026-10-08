import pytest
from shapely import LineString, Point, Polygon, unary_union
from svgelements import SVG, Color, Path, Rect, Shape
from watcherobot.distribution.source_files import collect_application_source_files

from robot_agents.config import ROOT
from robot_agents.expressions import (
    EXPRESSION_FILES,
    expression_strokes,
    segmented_progress_strokes,
    segmented_progress_vector,
)


@pytest.mark.parametrize("code", EXPRESSION_FILES)
def test_expression_assets_are_included_in_application(code):
    source = ROOT / "robot_agents" / "assets" / "emojis" / EXPRESSION_FILES[code]
    assert source.relative_to(ROOT) in collect_application_source_files(ROOT)


@pytest.mark.parametrize("code", EXPRESSION_FILES)
def test_vector_geometry_preserves_svg_strokes_and_filled_regions(code):
    svg = SVG.parse(str(ROOT / "robot_agents" / "assets" / "emojis" / EXPRESSION_FILES[code]))
    regions = []
    for shape in svg.elements():
        if not isinstance(shape, Shape):
            continue
        if shape.fill != Color("white") and shape.stroke != Color("white"):
            continue
        if code == "111" and isinstance(shape, Rect) and shape.fill == Color("white"):
            continue
        path = Path(shape)
        samples = [path.point(index / 1024) for index in range(1025)]
        points = [(point.x, point.y) for point in samples]
        if shape.fill == Color("white"):
            regions.append(Polygon(points))
        if shape.stroke == Color("white"):
            regions.append(LineString(points).buffer(shape.stroke_width / 2))
    expected = unary_union(regions)
    actual = unary_union(
        [
            LineString(stroke.points).buffer(stroke.width / 2)
            for stroke in expression_strokes(code)
        ]
    )
    overlap = actual.intersection(expected).area / actual.union(expected).area
    assert overlap > 0.94
    assert actual.hausdorff_distance(expected) < 2


def test_segmented_progress_keeps_gaps_and_preserves_face():
    frames = [segmented_progress_strokes(step) for step in range(11)]
    for step, strokes in enumerate(frames):
        assert strokes[:4] == expression_strokes("111")
        assert len(strokes) <= 12
        assert sum(len(stroke.points) for stroke in strokes) <= 192
        filled = unary_union([LineString(stroke.points).buffer(stroke.width / 2)
                              for stroke in strokes])
        for index in range(10):
            assert filled.covers(Point(142 + index * 14, 92)) == (index < step)
        for index in range(9):
            assert not filled.covers(Point(150 + index * 14, 92))
    assert len({segmented_progress_vector(step) for step in range(11)}) == 11


@pytest.mark.parametrize("step", [-1, 11, 1.5, True])
def test_segmented_progress_rejects_invalid_steps(step):
    with pytest.raises(ValueError):
        segmented_progress_vector(step)
