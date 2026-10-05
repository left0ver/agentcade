import pytest
from pydantic import ValidationError

from guess_animal.generator import GeneratedRound, GenerationHistory
from tests.samples import recorded_round


@pytest.mark.parametrize("coordinate", [-1, 412, 1.5, True, "12"])
def test_coordinates_require_integers_within_screen(coordinate):
    data = recorded_round()
    data["animals"][0]["strokes"][0]["points"][0][0] = coordinate
    with pytest.raises(ValidationError):
        GeneratedRound.model_validate(data)


@pytest.mark.parametrize("coordinates", [[120], [120, 150, 180]])
def test_each_point_requires_exactly_two_coordinates(coordinates):
    data = recorded_round()
    data["animals"][0]["strokes"][0]["points"][0] = coordinates
    with pytest.raises(ValidationError):
        GeneratedRound.model_validate(data)


def test_repeated_animal_aliases_are_rejected():
    data = recorded_round()
    data["animals"][1]["aliases"].append(data["animals"][0]["name"])
    with pytest.raises(ValidationError, match="互不重复"):
        GeneratedRound.model_validate(data)


def test_total_point_limit_is_checked_across_strokes():
    data = recorded_round()
    stroke = data["animals"][0]["strokes"][0]
    stroke["points"] = stroke["points"] * 3
    data["animals"][0]["strokes"] = [stroke] * 5
    with pytest.raises(ValidationError, match="坐标总量"):
        GeneratedRound.model_validate(data)


@pytest.mark.parametrize("index", range(3))
def test_hint_must_not_reveal_answer(index):
    data = recorded_round()
    data["animals"][0]["hints"][index] = "它就是家猫。"
    with pytest.raises(ValidationError, match="包含答案"):
        GeneratedRound.model_validate(data)


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_hints_require_exactly_three_clues(count):
    data = recorded_round()
    data["animals"][0]["hints"] = (data["animals"][0]["hints"] * 2)[:count]
    with pytest.raises(ValidationError):
        GeneratedRound.model_validate(data)


def test_duplicate_and_long_hints_are_rejected():
    data = recorded_round()
    hints = data["animals"][0]["hints"]
    hints[1] = hints[0]
    with pytest.raises(ValidationError, match="不能重复"):
        GeneratedRound.model_validate(data)
    hints[1] = "它是小型家养动物，擅长捕鼠，爱干净，趾底有肉垫，行动无声。"
    with pytest.raises(ValidationError):
        GeneratedRound.model_validate(data)


@pytest.mark.parametrize("index", range(3))
def test_each_hint_requires_an_explicit_subject(index):
    data = recorded_round()
    data["animals"][0]["hints"][index] = data["animals"][0]["hints"][index][1:]
    with pytest.raises(ValidationError, match="pattern"):
        GeneratedRound.model_validate(data)


def test_identical_drawings_are_rejected_even_with_different_names():
    data = recorded_round()
    data["animals"][1]["strokes"] = data["animals"][0]["strokes"]
    with pytest.raises(ValidationError, match="互不重复"):
        GeneratedRound.model_validate(data)


@pytest.mark.parametrize("axis,message", [(0, "宽度不足"), (1, "高度不足")])
def test_drawing_must_fill_minimum_screen_width_and_height(axis, message):
    data = recorded_round()
    for stroke in data["animals"][0]["strokes"]:
        for point in stroke["points"]:
            point[axis] //= 4
    with pytest.raises(ValidationError, match=message):
        GeneratedRound.model_validate(data)


def test_history_rejects_recent_species():
    animals = tuple(
        item.animal() for item in GeneratedRound.model_validate(recorded_round()).animals
    )
    history = GenerationHistory().updated(animals)
    with pytest.raises(ValueError, match="近期"):
        history.updated(animals)


def test_history_rejects_reused_drawings_after_names_expire():
    animals = tuple(
        item.animal() for item in GeneratedRound.model_validate(recorded_round()).animals
    )
    history = GenerationHistory().updated(animals)
    history.recent_names = []
    with pytest.raises(ValueError, match="相同图案"):
        history.updated(animals)


def test_history_round_trip_retains_names_and_drawing_hashes():
    animals = tuple(
        item.animal() for item in GeneratedRound.model_validate(recorded_round()).animals
    )
    history = GenerationHistory().updated(animals)
    restored = GenerationHistory.model_validate_json(history.model_dump_json())
    assert restored == history
    assert len(restored.drawing_hashes) == len(animals)
