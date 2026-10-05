import math

import pytest
from shapely import LineString, Point, Polygon, unary_union
from svgelements import SVG, Color, Path, Shape
from watcherobot.distribution.source_files import collect_application_source_files

from robot_agents.config import ROOT
from robot_agents.expressions import (
    EXPRESSION_FILES,
    expression_strokes,
    expression_vector,
    generation_progress,
    segmented_progress_strokes,
    segmented_progress_vector,
)


@pytest.mark.parametrize("code", EXPRESSION_FILES)
def test_expression_assets_are_included_in_application(code):
    source = ROOT / "robot_agents" / "assets" / "emojis" / EXPRESSION_FILES[code]
    assert source.relative_to(ROOT) in collect_application_source_files(ROOT)


@pytest.mark.parametrize("code,progress", [("009", 0), ("111", 62), ("141", 0)])
def test_vector_geometry_preserves_svg_strokes_and_filled_regions(code, progress):
    svg = SVG.parse(str(ROOT / "robot_agents" / "assets" / "emojis" / EXPRESSION_FILES[code]))
    regions = []
    for shape in svg.elements():
        if not isinstance(shape, Shape):
            continue
        if shape.fill != Color("white") and shape.stroke != Color("white"):
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
            for stroke in expression_strokes(code, progress)
        ]
    )
    overlap = actual.intersection(expected).area / actual.union(expected).area
    assert overlap > 0.94
    assert actual.hausdorff_distance(expected) < 2


def test_progress_is_monotonic_and_stays_incomplete_until_generation_finishes():
    values = [generation_progress(elapsed, 60) for elapsed in range(601)]
    assert values == sorted(values)
    assert values[0] == 0
    assert [values[elapsed] for elapsed in (0, 15, 30, 45, 60, 120)] == [0, 25, 50, 75, 99, 99]
    assert generation_progress(15, 30) == 50
    assert generation_progress(1, 60) == 1
    assert values[-1] < 100
    assert generation_progress(1e12, 60) == 99
    assert expression_vector("111", 99) != expression_vector("111", 100)
    frames = [expression_strokes("111", i) for i in range(1, 101)]
    edges = [frame[-1].points[-1][0] + frame[-1].width / 2 for frame in frames]
    assert edges == sorted(edges)
    assert edges[-1] == 275
    assert len({expression_vector("111", i) for i in range(101)}) == 101


@pytest.mark.parametrize("elapsed,estimate", [(-1, 60), (0, 0), (math.inf, 60), (0, math.nan)])
def test_invalid_generation_timing_fails(elapsed, estimate):
    with pytest.raises(ValueError):
        generation_progress(elapsed, estimate)


def test_segmented_progress_keeps_gaps_and_preserves_face():
    frames = [segmented_progress_strokes(step) for step in range(11)]
    for step, strokes in enumerate(frames):
        assert strokes[:4] == expression_strokes("111", 0)
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
