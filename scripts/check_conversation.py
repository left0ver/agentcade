import asyncio
import json

from guess_animal.config import ROOT, Settings
from guess_animal.conversation import Conversation
from guess_animal.game import Decision
from guess_animal.generator import GeneratedRound
from guess_animal.judge import JevJudge


async def main() -> None:
    settings = Settings.from_environment()
    animal = GeneratedRound.model_validate_json(
        (ROOT / "tests/fixtures/generated_round.json").read_text(encoding="utf-8")
    ).animals[0].animal()
    judge, conversation = JevJudge(settings), Conversation(settings)
    report = {"complete": False, "decisions": [], "conversation": []}
    cases = (
        ("你好，可以聊聊天吗", None, False, Decision.CHAT),
        ("企鹅为什么不会飞", animal, True, Decision.CHAT),
        ("你还要画多久", None, True, Decision.CHAT),
        ("我们来玩你画我猜", None, False, Decision.START),
        ("结束游戏", None, True, Decision.END),
        ("我猜是猫", animal, True, Decision.CORRECT),
        ("应该是乌龟", animal, True, Decision.INCORRECT),
        ("猫或者狗", animal, True, Decision.UNCLEAR),
        ("提示", animal, True, Decision.HINT),
        ("下一题", animal, True, Decision.SKIP),
    )
    output = ROOT / ".artifacts" / "conversation_check.json"
    output.parent.mkdir(exist_ok=True)
    try:
        for text, target, active, expected in cases:
            actual = await judge.decide(text, target, game_active=active)
            report["decisions"].append({"text": text, "actual": actual.value})
            if actual is not expected:
                raise AssertionError(f"{text}：得到 {actual.value}，预期 {expected.value}")
            print(f"{text} → {actual.value}", flush=True)
        for text in ("企鹅为什么不会飞？", "那它们是怎么游泳的？"):
            answer = await conversation.answer(text, phase="生成题目")
            report["conversation"].append({"question": text, "answer": answer})
            print(answer, flush=True)
        report["complete"] = True
    finally:
        await judge.close()
        await conversation.close()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
