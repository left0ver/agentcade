import pytest

from robot_agents.vector import Stroke, encode_vector
from tests.samples import recorded_animals


@pytest.mark.parametrize("point", [(-1, 200), (412, 200), (200, -1), (200, 412)])
def test_drawing_rejects_coordinates_outside_screen(point):
    with pytest.raises(ValueError, match="坐标"):
        Stroke((point,))


@pytest.mark.parametrize("width", [0, 49])
def test_drawing_rejects_unsupported_line_width(width):
    with pytest.raises(ValueError, match="宽度"):
        Stroke(((100, 100), (200, 200)), width)


def test_vector_encoding_matches_known_device_bytes():
    strokes = (Stroke(((0, 411), (411, 0)), 6),)
    assert encode_vector(strokes) == "010106020000019b019b0000"


def test_drawing_steps_finish_with_the_complete_animal():
    for animal in recorded_animals():
        steps = animal.drawing_steps()
        assert len(steps) == len(animal.strokes)
        assert len(set(steps)) == len(steps)
        assert steps[-1] == encode_vector(animal.strokes)


def test_drawing_rejects_total_stroke_and_point_overflow():
    with pytest.raises(ValueError, match="笔画或坐标总量"):
        encode_vector((Stroke(((100, 100), (200, 200))),) * 13)
    with pytest.raises(ValueError, match="笔画或坐标总量"):
        encode_vector((Stroke(((100, 100), (200, 200)) * 24),) * 5)
