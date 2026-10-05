import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from guess_animal.generator import GeneratedRound, GenerationHistory
from tests.samples import recorded_animals, recorded_round

THREE_DAYS_SECONDS = 72 * 60 * 60


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
    animals = recorded_animals()
    history = GenerationHistory().updated(animals, now=1_000_000)
    with pytest.raises(ValueError, match="近期"):
        history.updated(animals, now=1_000_000 + THREE_DAYS_SECONDS - 0.001)


def test_history_rejects_recent_drawings_with_different_names():
    first, second, *_ = recorded_animals()
    history = GenerationHistory().updated((first,), now=1_000_000)
    renamed = replace(first, name=second.name, aliases=second.aliases)
    with pytest.raises(ValueError, match="相同图案"):
        history.updated((renamed,), now=1_000_000 + THREE_DAYS_SECONDS - 0.001)


@pytest.mark.parametrize("elapsed", [THREE_DAYS_SECONDS, THREE_DAYS_SECONDS + 1])
def test_names_and_drawings_can_repeat_after_72_hours(elapsed):
    animals = recorded_animals()
    history = GenerationHistory().updated(animals, now=1_000_000)
    assert history.active(1_000_000 + elapsed).recent_names == []
    assert history.active(1_000_000 + elapsed).drawing_hashes == []
    refreshed = history.updated(animals, now=1_000_000 + elapsed)
    assert len(refreshed.entries) == len(animals)
    assert all(entry.generated_at == 1_000_000 + elapsed for entry in refreshed.entries)
    with pytest.raises(ValueError, match="近期"):
        refreshed.updated(animals, now=1_000_000 + elapsed + 1)


def test_history_expiry_preserves_other_recent_questions():
    first, second, *_ = recorded_animals()
    history = GenerationHistory().updated((first,), now=1_000_000)
    history = history.updated((second,), now=1_000_000 + 3600)
    active = history.active(1_000_000 + THREE_DAYS_SECONDS)
    assert first.name not in active.recent_names
    assert second.name in active.recent_names
    assert len(active.drawing_hashes) == 1
    with pytest.raises(ValueError, match="近期"):
        active.updated((second,), now=1_000_000 + THREE_DAYS_SECONDS)


def test_history_normalizes_aliases_before_comparing():
    first, second, *_ = recorded_animals()
    history = GenerationHistory().updated((first,), now=1_000_000)
    alias = replace(second, name=f"  {first.aliases[0]}  ")
    with pytest.raises(ValueError, match="近期"):
        history.updated((alias,), now=1_000_001)


def test_history_round_trip_retains_names_and_drawing_hashes():
    animals = recorded_animals()
    history = GenerationHistory().updated(animals, now=1_000_000)
    restored = GenerationHistory.model_validate_json(history.model_dump_json())
    assert restored == history
    assert len(restored.drawing_hashes) == len(animals)
    assert all(entry.generated_at == 1_000_000 for entry in restored.entries)


def test_saved_history_keeps_its_original_expiry_after_restart(run_directory):
    history = GenerationHistory().updated(recorded_animals(), now=1_000_000)
    path = run_directory / "history.json"
    path.write_text(history.model_dump_json())
    restored = GenerationHistory.load(path)
    assert restored.active(1_000_000 + THREE_DAYS_SECONDS - 1).recent_names
    assert restored.active(1_000_000 + THREE_DAYS_SECONDS).recent_names == []


def test_legacy_history_uses_archive_generation_time(run_directory):
    data = recorded_round()
    animals = recorded_animals()
    history = GenerationHistory().updated(animals, now=1_000_000)
    path = run_directory / "history.json"
    path.write_text(json.dumps({
        "recent_names": history.recent_names[-2:],
        "drawing_hashes": history.drawing_hashes,
    }))
    archive = run_directory / "generated"
    archive.mkdir()
    for index, animal in enumerate(data["animals"]):
        animal["hint"] = animal.pop("hints")[0]
        timestamp = (1_000_000 + index * 3600) * 1_000_000_000
        (archive / f"{timestamp}.json").write_text(json.dumps({"animals": [animal]}))
    restored = GenerationHistory.load(path)
    assert restored.recent_names == history.recent_names
    active = restored.active(1_000_000 + THREE_DAYS_SECONDS)
    assert animals[0].name not in active.recent_names
    assert animals[1].name in active.recent_names


def test_legacy_history_requires_archives_for_generation_time(run_directory):
    history = GenerationHistory().updated(recorded_animals(), now=1_000_000)
    path = run_directory / "history.json"
    path.write_text(json.dumps({
        "recent_names": history.recent_names,
        "drawing_hashes": history.drawing_hashes,
    }))
    with pytest.raises(ValueError, match="缺少生成时间"):
        GenerationHistory.load(path)


def test_missing_history_starts_empty(run_directory):
    assert GenerationHistory.load(run_directory / "history.json").entries == []
