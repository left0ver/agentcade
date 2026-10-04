import pytest
from watcherobot.robot import _expression_custom_vector_path

from guess_animal.animals import ANIMALS, Stroke, encode_vector
from guess_animal.game import Decision, Game


def test_deck_has_five_distinct_animals():
    game = Game()
    reply = game.start()
    assert reply.draw
    assert len({animal.id for animal in game.deck}) == 5
    assert all(animal in ANIMALS for animal in game.deck)


def test_whole_game_counts_each_correct_answer_once():
    game = Game()
    game.start()
    for expected_score in range(1, 6):
        revision = game.revision
        assert game.handle(Decision.CORRECT, revision).advance
        assert not game.handle(Decision.CORRECT, revision).advance
        assert game.score == expected_score
        reply = game.advance()
    assert reply.clear and "5题" in reply.text
    assert not game.active
    assert game.animal is None


def test_wrong_answers_reveal_only_after_second_attempt():
    game = Game()
    game.start()
    assert not game.handle(Decision.INCORRECT, game.revision).advance
    assert game.attempts == 1
    assert game.handle(Decision.INCORRECT, game.revision).advance
    assert game.score == 0
    game.advance()
    assert game.attempts == 0


def test_hint_and_unclear_do_not_consume_attempts():
    game = Game()
    game.start()
    for decision in (Decision.HINT, Decision.UNCLEAR, Decision.IGNORE):
        assert not game.handle(decision, game.revision).advance
    assert game.attempts == game.score == 0


def test_skip_moves_to_next_animal_without_score():
    game = Game()
    game.start()
    animal = game.animal
    assert game.handle(Decision.SKIP, game.revision).advance
    assert game.advance().draw
    assert game.animal != animal
    assert game.score == 0


def test_old_results_cannot_change_a_later_question_or_new_game():
    game = Game()
    game.start()
    old_revision = game.revision
    game.handle(Decision.SKIP, old_revision)
    game.advance()
    assert not game.handle(Decision.CORRECT, old_revision).advance
    assert game.score == 0
    game.handle(Decision.END, game.revision)
    game.start()
    assert not game.handle(Decision.CORRECT, old_revision).advance
    assert game.score == 0


def test_idle_commands_and_answers_do_not_start_game():
    game = Game()
    for decision in (Decision.CORRECT, Decision.HINT, Decision.END, Decision.IGNORE):
        assert not game.handle(decision, game.revision).text
        assert not game.active
    assert game.handle(Decision.START, game.revision).draw


def test_repeated_start_preserves_game():
    game = Game()
    game.start()
    deck = tuple(game.deck)
    game.handle(Decision.INCORRECT, game.revision)
    game.handle(Decision.START, game.revision)
    assert tuple(game.deck) == deck
    assert game.attempts == 1


def test_silence_ends_game_after_three_windows_and_response_resets_count():
    game = Game()
    assert not game.silence().text
    game.start()
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
    game.start()
    with pytest.raises(RuntimeError):
        game.advance()


@pytest.mark.parametrize("animal", ANIMALS, ids=lambda animal: animal.id)
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
