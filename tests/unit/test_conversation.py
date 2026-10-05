import pytest
from pydantic import ValidationError

from robot_agents.conversation import ConversationReply


@pytest.mark.parametrize("reply", ["", "  ", "回答" * 251])
def test_empty_or_oversized_reply_is_rejected(reply):
    with pytest.raises(ValidationError):
        ConversationReply.model_validate({"reply": reply})


def test_response_must_contain_only_the_spoken_reply():
    with pytest.raises(ValidationError):
        ConversationReply.model_validate({"reply": "你好。", "score": 1})


def test_reply_whitespace_is_removed_before_speech():
    assert ConversationReply.model_validate_json('{"reply":"  你好。  "}').reply == "你好。"
