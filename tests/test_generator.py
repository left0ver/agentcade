import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from guess_animal.generator import GeneratedRound, GenerationHistory


def recorded_round() -> dict:
    return json.loads(
        (Path(__file__).parent / "fixtures" / "generated_round.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("coordinate", [-1, 412, 1.5, True, "12"])
def test_coordinates_require_integers_within_screen(coordinate):
    data = recorded_round()
    data["animals"][0]["strokes"][0]["points"][0][0] = coordinate
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


def test_hint_must_not_reveal_answer():
    data = recorded_round()
    data["animals"][0]["hint"] = "这只动物就是家猫。"
    with pytest.raises(ValidationError, match="包含答案"):
        GeneratedRound.model_validate(data)


def test_identical_drawings_are_rejected_even_with_different_names():
    data = recorded_round()
    data["animals"][1]["strokes"] = data["animals"][0]["strokes"]
    with pytest.raises(ValidationError, match="互不重复"):
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
