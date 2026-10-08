import pytest

from games.guess_animal.game import Decision, Game
from tests.samples import recorded_animals

RECORDED = recorded_animals()


def start_question(game):
    game.start()
    game.set_question(RECORDED[0])


def test_unclear_input_preserves_question_and_attempts():
    game = Game()
    start_question(game)
    revision = game.revision
    animal = game.animal
    reply = game.handle(Decision.UNCLEAR, revision)
    assert reply.text and not reply.advance and not reply.prepare
    assert game.animal is animal
    assert game.attempts == game.score == game.hint_index == 0
    assert game.revision == revision


def test_answer_from_previous_question_does_not_consume_next_attempt():
    game = Game()
    start_question(game)
    previous_revision = game.revision
    game.handle(Decision.CORRECT, previous_revision)
    game.advance()
    game.set_question(RECORDED[1])
    reply = game.handle(Decision.INCORRECT, previous_revision)
    assert not reply.text
    assert game.attempts == 0 and game.score == 1


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
    for decision in (Decision.HINT, Decision.UNCLEAR):
        assert not game.handle(decision, game.revision).advance
    assert game.attempts == game.score == 0


@pytest.mark.parametrize(
    "decisions",
    [(Decision.HINT, Decision.INCORRECT), (Decision.INCORRECT, Decision.HINT)],
)
def test_hints_and_wrong_answers_share_progress_without_repeating_clues(decisions):
    game = Game()
    start_question(game)
    hints = RECORDED[0].hints
    for index, decision in enumerate(decisions):
        reply = game.handle(decision, game.revision)
        prefix = "再想想，给你个提示：" if decision is Decision.INCORRECT else ""
        assert reply.text == prefix + hints[index] + "。"
        assert game.hint_index == index + 1
        assert game.attempts == decisions[:index + 1].count(Decision.INCORRECT)
    third = game.handle(Decision.HINT, game.revision)
    assert third.text == hints[2] + "。"
    for _ in range(2):
        exhausted = game.handle(Decision.HINT, game.revision)
        assert "提示都给你啦" in exhausted.text
        assert all(hint not in exhausted.text for hint in hints)
    assert game.attempts == 1 and game.score == 0
    final = game.handle(Decision.INCORRECT, game.revision)
    assert final.text == f"这题的答案是{RECORDED[0].name}。"
    assert final.advance and final.feedback == "incorrect"


def test_hint_progress_resets_for_each_question_and_new_game():
    game = Game()
    start_question(game)
    revision = game.revision
    game.handle(Decision.INCORRECT, revision)
    game.handle(Decision.SKIP, revision)
    assert not game.handle(Decision.HINT, revision).text
    game.advance()
    game.set_question(RECORDED[1])
    assert not game.handle(Decision.HINT, revision).text
    assert game.hint_index == game.attempts == 0
    assert game.handle(Decision.HINT, game.revision).text == RECORDED[1].hints[0] + "。"
    reply = game.handle(Decision.INCORRECT, game.revision)
    assert reply.text == "再想想，给你个提示：" + RECORDED[1].hints[1] + "。"
    game.handle(Decision.END, game.revision)
    start_question(game)
    assert game.handle(Decision.HINT, game.revision).text == RECORDED[0].hints[0] + "。"
    reply = game.handle(Decision.INCORRECT, game.revision)
    assert reply.text == "再想想，给你个提示：" + RECORDED[0].hints[1] + "。"


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
    for decision in (Decision.CORRECT, Decision.HINT, Decision.END):
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


def test_advance_requires_resolved_question():
    game = Game()
    with pytest.raises(RuntimeError):
        game.advance()
    start_question(game)
    with pytest.raises(RuntimeError):
        game.advance()


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


def test_answers_during_preparation_do_not_change_game():
    game = Game()
    game.start()
    pending_revision = game.revision
    for decision in (Decision.CORRECT, Decision.INCORRECT, Decision.SKIP, Decision.HINT):
        assert not game.handle(decision, pending_revision).text
    assert not game.start().prepare
    assert game.active
    assert game.attempts == game.score == 0
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
