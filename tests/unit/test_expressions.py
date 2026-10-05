import math

import pytest
from shapely import LineString, Polygon, unary_union
from svgelements import SVG, Color, Path, Shape
from watcherobot.distribution.source_files import collect_application_source_files

from guess_animal.config import ROOT
from guess_animal.expressions import (
    EXPRESSION_FILES,
    expression_strokes,
    expression_vector,
    generation_progress,
)


@pytest.mark.parametrize("code", EXPRESSION_FILES)
def test_expression_assets_are_included_in_application(code):
    source = ROOT / "emojis" / EXPRESSION_FILES[code]
    assert source.relative_to(ROOT) in collect_application_source_files(ROOT)


@pytest.mark.parametrize("code,progress", [("009", 0), ("111", 62), ("141", 0)])
def test_vector_geometry_preserves_svg_strokes_and_filled_regions(code, progress):
    svg = SVG.parse(str(ROOT / "emojis" / EXPRESSION_FILES[code]))
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
