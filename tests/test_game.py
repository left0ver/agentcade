from pathlib import Path

import pytest
from watcherobot.robot import _expression_custom_vector_path

from guess_animal.animals import Stroke, encode_vector
from guess_animal.game import Decision, Game
from guess_animal.generator import GeneratedRound

RECORDED = tuple(
    animal.animal()
    for animal in GeneratedRound.model_validate_json(
        (Path(__file__).parent / "fixtures" / "generated_round.json").read_text(encoding="utf-8")
    ).animals
)


def start_question(game):
    game.start()
    game.set_question(RECORDED[0])


def test_start_waits_for_one_question():
    game = Game()
    reply = game.start()
    assert reply.prepare
    assert game.deck == []
    assert game.animal is None
    assert game.active
    game.set_question(RECORDED[0])
    assert game.deck == [RECORDED[0]]
    assert game.animal == RECORDED[0]


def test_whole_game_counts_each_correct_answer_once():
    game = Game()
    game.start()
    for expected_score in range(1, 6):
        assert game.animal is None
        game.set_question(RECORDED[expected_score - 1])
        revision = game.revision
        assert game.handle(Decision.CORRECT, revision).advance
        assert not game.handle(Decision.CORRECT, revision).advance
        assert game.score == expected_score
        reply = game.advance()
        assert reply.prepare == (expected_score < 5)
    assert reply.clear and "5题" in reply.text
    assert not game.active
    assert game.animal is None


def test_wrong_answers_reveal_only_after_second_attempt():
    game = Game()
    start_question(game)
    assert not game.handle(Decision.INCORRECT, game.revision).advance
    assert game.attempts == 1
    assert game.handle(Decision.INCORRECT, game.revision).advance
    assert game.score == 0
    game.advance()
    assert game.attempts == 0


def test_answer_feedback_matches_the_result_on_every_attempt():
    game = Game()
    start_question(game)
    first = game.handle(Decision.INCORRECT, game.revision)
    assert first.feedback == "incorrect" and not first.advance
    second = game.handle(Decision.INCORRECT, game.revision)
    assert second.feedback == "incorrect" and second.advance
    assert game.handle(Decision.CORRECT, game.revision).feedback is None
    game.advance()
    game.set_question(RECORDED[1])
    correct = game.handle(Decision.CORRECT, game.revision)
    assert correct.feedback == "correct" and correct.advance


@pytest.mark.parametrize("decision", [Decision.HINT, Decision.SKIP, Decision.UNCLEAR, Decision.END])
def test_other_commands_and_stale_answers_have_no_answer_feedback(decision):
    game = Game()
    start_question(game)
    assert game.handle(Decision.CORRECT, game.revision - 1).feedback is None
    assert game.handle(decision, game.revision).feedback is None


def test_hint_and_unclear_do_not_consume_attempts():
    game = Game()
    start_question(game)
    for decision in (Decision.HINT, Decision.UNCLEAR, Decision.IGNORE):
        assert not game.handle(decision, game.revision).advance
    assert game.attempts == game.score == 0


def test_skip_moves_to_next_animal_without_score():
    game = Game()
    start_question(game)
    animal = game.animal
    assert game.handle(Decision.SKIP, game.revision).advance
    assert game.advance().prepare
    assert game.animal is None
    assert len(game.deck) == 1
    game.set_question(RECORDED[1])
    assert game.animal != animal
    assert game.score == 0


def test_old_results_cannot_change_a_later_question_or_new_game():
    game = Game()
    start_question(game)
    old_revision = game.revision
    game.handle(Decision.SKIP, old_revision)
    game.advance()
    game.set_question(RECORDED[1])
    assert not game.handle(Decision.CORRECT, old_revision).advance
    assert game.score == 0
    game.handle(Decision.END, game.revision)
    start_question(game)
    assert not game.handle(Decision.CORRECT, old_revision).advance
    assert game.score == 0


def test_idle_commands_and_answers_do_not_start_game():
    game = Game()
    for decision in (Decision.CORRECT, Decision.HINT, Decision.END, Decision.IGNORE):
        assert not game.handle(decision, game.revision).text
        assert not game.active
    assert game.handle(Decision.START, game.revision).prepare


def test_repeated_start_preserves_game():
    game = Game()
    start_question(game)
    deck = tuple(game.deck)
    game.handle(Decision.INCORRECT, game.revision)
    game.handle(Decision.START, game.revision)
    assert tuple(game.deck) == deck
    assert game.attempts == 1


def test_silence_ends_game_after_three_windows_and_response_resets_count():
    game = Game()
    assert not game.silence().text
    start_question(game)
    game.silence()
    game.silence()
    game.handle(Decision.HINT, game.revision)
    assert game.silence_count == 0
    assert not game.silence().clear
    assert not game.silence().clear
    assert game.silence().clear
    assert not game.active


def test_advance_requires_resolved_question():
    game = Game()
    with pytest.raises(RuntimeError):
        game.advance()
    start_question(game)
    with pytest.raises(RuntimeError):
        game.advance()


@pytest.mark.parametrize("animal", RECORDED, ids=lambda animal: animal.id)
def test_every_drawing_step_is_accepted_by_sdk(animal):
    for path in animal.drawing_steps():
        assert _expression_custom_vector_path(path) == path


def test_invalid_drawing_is_rejected_before_device_command():
    with pytest.raises(ValueError):
        Stroke(((412, 0),))
    with pytest.raises(ValueError):
        encode_vector((Stroke(((0, 0),)),) * 13)


@pytest.mark.parametrize("rounds,attempts", [(0, 2), (11, 2), (5, 0)])
def test_invalid_game_rules_fail_immediately(rounds, attempts):
    with pytest.raises(ValueError):
        Game(rounds, attempts)


def test_question_can_only_be_added_while_waiting_for_generation():
    game = Game()
    with pytest.raises(RuntimeError):
        game.set_question(RECORDED[0])
    start_question(game)
    with pytest.raises(RuntimeError):
        game.set_question(RECORDED[1])
    game.handle(Decision.SKIP, game.revision)
    with pytest.raises(RuntimeError):
        game.set_question(RECORDED[1])


def test_next_question_must_have_a_new_animal():
    game = Game()
    start_question(game)
    game.handle(Decision.SKIP, game.revision)
    game.advance()
    with pytest.raises(ValueError, match="不能重复"):
        game.set_question(RECORDED[0])
    assert game.animal is None
    assert len(game.deck) == 1


def test_answers_and_silence_during_preparation_do_not_change_game():
    game = Game()
    game.start()
    pending_revision = game.revision
    for decision in (Decision.CORRECT, Decision.INCORRECT, Decision.SKIP, Decision.HINT):
        assert not game.handle(decision, pending_revision).text
    for _ in range(3):
        assert not game.silence().text
    assert not game.start().prepare
    assert game.active
    assert game.attempts == game.score == game.silence_count == 0
    game.set_question(RECORDED[0])
    assert not game.handle(Decision.CORRECT, pending_revision).advance
    assert game.score == 0


def test_game_can_end_while_question_is_pending():
    game = Game()
    game.start()
    assert game.handle(Decision.END, game.revision).clear
    assert not game.active
    with pytest.raises(RuntimeError):
        game.set_question(RECORDED[0])
