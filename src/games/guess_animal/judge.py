from typesafe_sdk import AsyncTypeSafeClient, Choice, RetryPolicy

from games.guess_animal.animals import Animal
from games.guess_animal.config import Settings
from games.guess_animal.game import Decision


class JevJudge:
    def __init__(self, settings: Settings):
        self.client = AsyncTypeSafeClient(
            api_key=settings.typesafe_api_key,
            model=settings.jev_model,
            timeout=20,
            # 连接失败、超时、限流和服务端错误时重试，连同等待最多占用 30 秒。
            retry=RetryPolicy(max_retries=2, timeout=30),
        )
        self.threshold = settings.confidence_threshold

    async def decide(self, text: str, animal: Animal) -> Decision:
        if not text.strip():
            return Decision.UNCLEAR
        criteria = {
            "correct": "明确作答并指向目标动物或认可别名，可以包含我猜、应该是等表达。",
            "incorrect": "明确作答为另一种动物。",
            "unclear": "没有明确回答一个动物名称，包括查询规则、进度、请求提示或跳题。",
        }
        result = await self.client.system_one(
            state={
                "utterance": text,
                "target": animal.name,
                "accepted_aliases": list(animal.aliases),
            },
            questions={
                "decision": Choice(
                    instructions=(
                        "根据 utterance 判定用户猜测的动物是否匹配 target 或 accepted_aliases。"
                    ),
                    criteria=criteria,
                )
            },
        )
        answer = result.choices["decision"]
        if answer.confidence < self.threshold:
            return Decision.UNCLEAR
        return Decision(answer.choice)

    async def close(self) -> None:
        await self.client.aclose()
