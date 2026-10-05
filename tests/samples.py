import json
from pathlib import Path

from guess_animal.generator import GeneratedRound


def recorded_round() -> dict:
    source = Path(__file__).parent / "fixtures" / "generated_round.json"
    return json.loads(source.read_text(encoding="utf-8"))


def recorded_animals():
    return tuple(item.animal() for item in GeneratedRound.model_validate(recorded_round()).animals)
