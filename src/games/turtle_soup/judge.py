from typesafe_sdk import AsyncTypeSafeClient, Choice, RetryPolicy

from games.turtle_soup.config import Settings
from games.turtle_soup.library import Puzzle

# 置信度低于门槛时不给出是或不是，统一回答“不好说”。
UNSURE = "unsure"


class JevJudge:
    """用 Jev 按汤底回答玩家的提问，判断玩家的推理有没有猜中。"""

    def __init__(self, settings: Settings):
        self.client = AsyncTypeSafeClient(
            api_key=settings.typesafe_api_key,
            model=settings.jev_model,
            timeout=20,
            # 连接失败、超时、限流和服务端错误时重试，连同等待最多占用 30 秒。
            retry=RetryPolicy(max_retries=2, timeout=30),
        )
        self.threshold = settings.confidence_threshold

    async def choose(self, state: dict, name: str, instructions: str, criteria: dict) -> str:
        result = await self.client.system_one(
            state=state, questions={name: Choice(instructions=instructions, criteria=criteria)},
        )
        answer = result.choices[name]
        return answer.choice if answer.confidence >= self.threshold else UNSURE

    async def answer(self, puzzle: Puzzle, question: str) -> str:
        """返回 yes、no、irrelevant、not_question 或 unsure。"""
        return await self.choose(
            {"surface": puzzle.surface, "story": puzzle.story, "truth": puzzle.truth,
             "question": question},
            "answer",
            "海龟汤推理游戏：surface 是玩家听到的汤面，story 是只有主持人知道的完整故事，"
            "truth 是故事的摘要。以 story 为准，判断玩家的 question 应该怎么回答。",
            {
                "yes": "question 是是非问题，按照 story 答案是肯定的。",
                "no": "question 是是非问题，按照 story 答案是否定的。",
                "irrelevant": "question 是是非问题，但 story 里完全没有涉及；"
                              "story 里提到过的事实，哪怕只是顺带一提，也要答 yes 或 no。",
                "not_question": "不是一个可以用是或不是回答的问题，例如要答案、要提示、闲聊或陈述推理。",
            },
        )

    async def verify(self, puzzle: Puzzle, guess: str) -> str:
        """返回 solved、partial、wrong 或 unsure。"""
        return await self.choose(
            {"surface": puzzle.surface, "story": puzzle.story, "truth": puzzle.truth,
             "key": puzzle.key, "guess": guess},
            "verdict",
            "海龟汤推理游戏：判断玩家的 guess 是否还原了 truth 的关键点 key。",
            {
                "solved": "guess 说出了 key 里的全部关键点，意思一致即可，不要求措辞相同。",
                "partial": "guess 说中了一部分关键点，但还缺少重要部分。",
                "wrong": "guess 与 key 不符，或者没有说中任何关键点。",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()
