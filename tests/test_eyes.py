import pytest
from watcherobot.robot import ExpressionRuntimeDomain

from guess_animal.eyes import HIDDEN_EYES, LOOKS, STANDBY_EYES


def test_standby_and_hidden_eyes_are_accepted_by_sdk():
    ExpressionRuntimeDomain._build_payload(preset="standby", **STANDBY_EYES)
    ExpressionRuntimeDomain._build_payload(preset="standby", **HIDDEN_EYES)


@pytest.mark.parametrize("name,look", LOOKS, ids=[name.split("：")[0] for name, _ in LOOKS])
def test_every_look_is_white_and_accepted_by_sdk(name, look):
    ExpressionRuntimeDomain._build_payload(preset="standby", **look)
    assert look["color"] == "#FFFFFF"
    assert look["accessory"] == "none"
    # 待机表情会把每种眼型改动过的大小、间距和眼睑恢复原样。
    assert set(look) - {"color", "roundness"} == set(STANDBY_EYES)


def test_preview_look_names_are_unique():
    assert len({name for name, _ in LOOKS}) == len(LOOKS)
