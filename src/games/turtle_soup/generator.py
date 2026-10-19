import argparse
import asyncio
import logging
import secrets
import sys
import time
from collections.abc import Callable
from difflib import SequenceMatcher
from functools import partial
from typing import TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typesafe_sdk import AsyncTypeSafeClient, Noul, RetryPolicy, Score

from agentcade.providers import OpenAIProvider
from games.turtle_soup.config import Settings
from games.turtle_soup.library import Generated, Library, Puzzle

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

# 每道题随机抽一个题材，避免题目都长得差不多。
THEMES = (
    "误会", "身份", "时间线", "冷知识", "日常用品", "动物", "职业", "节日", "学校", "家庭",
    "旅行", "天气", "运动", "厨房", "镜子", "照片", "信件", "游戏", "数字", "声音",
)
ROUNDS = 3

STORY_PROMPT = """你是海龟汤（情境推理游戏）的出题人，给桌面机器人出题，题目会用语音念给玩家听。
第一步不写谜题，先写一个完整、自洽的原创故事，之后再从故事里提炼汤面和汤底。

要求：
1. story：300 到 800 字的完整故事，有具体的人物、动机、时间线和因果。每一件反常的事都要在故事里得到解释，前后不能矛盾。
2. 故事里要有一件从外面看很反常、知道原因后又完全合理的事，这就是反转。反转要出人意料，但线索都在故事里交代过，玩家靠是非问题能一步步问出来。
3. 只写清汤：不涉及死亡、受伤流血、暴力、犯罪、恐怖、鬼神和色情。
4. 必须原创，不要改写广为流传的经典海龟汤或脑筋急转弯，例如够不着电梯按钮、医生是妈妈、冰块化了、秃头淋雨、盲人读书这类。
5. 不要用“其实是一场梦”“其实是在演戏”收尾，也不要靠谐音和文字游戏。
6. 题材围绕 request 里的 theme 展开，不能和 avoid 里已有的题目重复或相似。
7. key_twist：一句话说出故事最关键的反转。
8. title：2 到 8 个字的中文标题，不点破答案。id：题目编号，英文小写字母和下划线，3 到 40 个字符，不能和 taken_ids 里的重复。
9. 人物用“他”“她”“小明”“妈妈”这样好念的称呼，少用长名字和生僻字。
request 里有 review_issues 时，说明上一版没有通过审核，要针对这些问题重写一个更好的故事。
request 里有 format_issues 时，说明上一次的输出不合格，按这些问题改正。
"""

CONDENSE_PROMPT = """把一个写好的故事提炼成一道海龟汤，题目会由机器人用语音念给玩家听。

要求：
1. surface（汤面）：1 到 2 句，不超过 80 个字。只呈现故事里最反常的那个现象，留下能追问的线索，不解释原因，不点破真相。
2. truth（汤底）：3 到 5 句，不超过 200 个字，讲清楚核心因果和关键反转。只能用故事里已有的信息，不能新增人物、事件或动机，也不能漏掉关键反转。
3. key（判定猜中的关键点）：一句话，不超过 60 个字，写玩家必须说中的要点。说中这些就算猜中，细节不用全对。
4. hints（提示）：恰好三条，每条一句话，不超过 30 个字，从模糊到具体：第一条指出该往哪个方向想，第二条缩小范围，第三条已经很接近答案，但仍然不能直接说出答案。三条提示内容不同，都要符合故事，不能照抄汤底。
5. 汤面里出现的每个细节，汤底都要能解释。
6. 所有文字都要适合直接念出来，不用括号、英文和特殊符号。
request 里有 review_issues 时，说明上一版没有通过审核，要针对这些问题修改。
request 里有 format_issues 时，说明上一次的输出不合格，按这些问题改正。
"""


class StoryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,39}$")
    title: str = Field(min_length=2, max_length=8)
    story: str = Field(min_length=300, max_length=1200)
    key_twist: str = Field(min_length=4, max_length=80)


class SoupDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    surface: str = Field(min_length=10, max_length=80)
    truth: str = Field(min_length=20, max_length=200)
    key: str = Field(min_length=4, max_length=60)
    hints: list[str] = Field(min_length=3, max_length=3)


def noul(question: str, true: str, false: str) -> Noul:
    return Noul(instructions=question, criteria={"true": true, "false": false})


# 审核项：Jev 打分，低于门槛就把问题带回去重写。
REVIEW = {
    "story_coherent": (0.8, noul(
        "story 是不是前后一致：没有矛盾，每件反常的事都在故事里得到了解释？",
        "故事自洽，反常的事都有解释。", "有前后矛盾，或者关键的事没有解释。")),
    "truth_faithful": (0.8, noul(
        "truth 是否忠于 story：没有编造 story 里没有的内容，并且保留了 key_twist？",
        "truth 是 story 的忠实概括，包含关键反转。", "truth 编造了内容、改动了情节或者漏掉了关键反转。")),
    "surface_fair": (0.6, noul(
        "只听 surface，通过问是非问题，玩家有没有可能推理出 key_twist？",
        "surface 留下了能追问的线索，反转可以推理出来。", "反转毫无来由，或者 surface 已经点破了答案。")),
    "surface_covered": (0.7, noul(
        "surface 里提到的每一个反常细节，truth 都给出了解释吗？",
        "每个细节都有解释。", "有细节在 truth 里没有解释。")),
    "clean_soup": (0.9, noul(
        "story 是清汤吗：不涉及死亡、受伤、暴力、犯罪、恐怖、鬼神和色情？",
        "是清汤。", "涉及了这些内容。")),
    "hints_true": (0.8, noul(
        "hints 里的每一条提示，按照 story 来看都是事实吗？",
        "每条提示都符合故事。", "有提示和故事不符。")),
    "hints_graded": (0.6, noul(
        "hints 的三条提示是否从模糊到具体逐步接近答案，并且没有一条直接说出 key_twist？",
        "三条提示逐步推进，也没有说破答案。", "提示没有层次，或者有一条直接说破了答案。")),
    "original": (0.6, noul(
        "这道题是原创的吗，而不是广为流传的经典海龟汤或脑筋急转弯的翻版？",
        "是原创的题目。", "是经典题目的翻版。")),
    "quality": (1.0, Score(
        instructions="作为一道海龟汤，这道题好不好玩？",
        criteria=["平淡：反转太简单、太随意，或者汤面让人摸不着头脑。",
                  "合格：公平、能玩，有真正的反转。",
                  "巧妙：出人意料又合情合理，让人印象深刻。"])),
}
# 带回给大模型的审核问题说明。
ISSUES = {
    "story_coherent": "故事有矛盾或者有反常的事没有解释",
    "truth_faithful": "汤底和故事不一致或者漏掉了关键反转",
    "surface_fair": "汤面要么没有能追问的线索，要么已经点破了答案",
    "surface_covered": "汤面有细节在汤底里没有解释",
    "clean_soup": "涉及了死亡、暴力、恐怖之类的内容，不是清汤",
    "hints_true": "有提示和故事不符",
    "hints_graded": "提示没有从模糊到具体，或者直接说破了答案",
    "original": "像是经典题目的翻版，不够原创",
    "quality": "反转太平淡或者太随意",
}


def copied(text: str, source: str, length: int = 12) -> bool:
    match = SequenceMatcher(None, text, source, autojunk=False).find_longest_match()
    return match.size >= length


def check_soup(soup: SoupDraft, key_twist: str) -> list[str]:
    problems = []
    if len(set(soup.hints)) < 3:
        problems.append("三条提示不能重复")
    for index, hint in enumerate(soup.hints, 1):
        if not hint or len(hint) > 30:
            problems.append(f"第 {index} 条提示要在 30 个字以内")
        if copied(hint, soup.truth) or copied(hint, key_twist):
            problems.append(f"第 {index} 条提示照抄了汤底")
    if sum(soup.surface.count(mark) for mark in "。！？!?") > 2:
        problems.append("汤面最多两句话")
    return problems


class PuzzleGenerator:
    """先写完整故事，再提炼成汤面、汤底和提示，最后由 Jev 审核。"""

    def __init__(self, settings: Settings):
        self.provider = OpenAIProvider(settings.llm)
        self.client = AsyncTypeSafeClient(
            api_key=settings.typesafe_api_key, model=settings.jev_model, timeout=60,
            retry=RetryPolicy(max_retries=2, timeout=120),
        )

    async def draft(self, model: type[T], system: str, request: dict,
                    check: Callable[[T], list[str]]) -> T:
        issues: list[str] = []
        for _ in range(3):
            output = await self.provider.generate(
                system, request | ({"format_issues": issues} if issues else {}),
                model.model_json_schema())
            try:
                value = model.model_validate_json(output)
            except ValidationError as error:
                issues = [f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                          for item in error.errors()]
                continue
            issues = check(value)
            if not issues:
                return value
        raise ValueError("大模型连续 3 次没有给出合格的内容：" + "；".join(issues))

    async def review(self, puzzle: Puzzle, key_twist: str) -> dict[str, float]:
        result = await self.client.system_one(
            state={"story": puzzle.story, "key_twist": key_twist, "surface": puzzle.surface,
                   "truth": puzzle.truth, "key": puzzle.key, "hints": list(puzzle.hints)},
            questions={name: question for name, (_, question) in REVIEW.items()},
        )
        scores = {name: answer.noul for name, answer in result.nouls.items()}
        return scores | {name: answer.score for name, answer in result.scores.items()}

    async def generate(self, avoid: list[Puzzle], *, must: bool = False,
                       budget: float | None = None) -> Generated:
        """生成一道题。must 为真时，审核没通过也返回总分最高的一版。"""
        started = time.monotonic()
        theme = secrets.choice(THEMES)
        taken = {puzzle.id for puzzle in avoid}
        issues: list[str] = []
        best: tuple[float, Generated] | None = None
        for attempt in range(1, ROUNDS + 1):
            if must and best and budget and time.monotonic() - started > budget:
                break
            story = await self.draft(StoryDraft, STORY_PROMPT, {
                "theme": theme,
                "avoid": [f"{puzzle.title}：{puzzle.surface}" for puzzle in avoid[-40:]],
                "taken_ids": sorted(taken),
                **({"review_issues": issues} if issues else {}),
            }, lambda draft: [f"id {draft.id} 已经用过"] if draft.id in taken else [])

            soup = await self.draft(SoupDraft, CONDENSE_PROMPT, {
                "story": story.story, "key_twist": story.key_twist,
                **({"review_issues": issues} if issues else {}),
            }, partial(check_soup, key_twist=story.key_twist))
            puzzle = Puzzle(id=story.id, title=story.title, surface=soup.surface,
                            truth=soup.truth, key=soup.key, hints=tuple(soup.hints),
                            story=story.story)
            scores = await self.review(puzzle, story.key_twist)
            failed = [name for name, (threshold, _) in REVIEW.items() if scores[name] < threshold]
            item = Generated(puzzle=puzzle, created_at=time.time(), theme=theme,
                             key_twist=story.key_twist, review=scores, passed=not failed)
            logger.info("出题第 %d 轮《%s》：%s", attempt, puzzle.title,
                        "通过" if not failed else "没通过 " + "、".join(failed))
            if not failed:
                return item
            total = sum(scores[name] for name in REVIEW if name != "quality") + scores["quality"] / 2
            if best is None or total > best[0]:
                best = total, item
            issues = [ISSUES[name] for name in failed]
        if must and best:
            return best[1]
        raise ValueError(f"连续 {ROUNDS} 轮没有通过审核")

    async def close(self) -> None:
        try:
            await self.provider.close()
        finally:
            await self.client.aclose()


class BlockDumper(yaml.SafeDumper):
    """和 puzzles.yaml 一样的格式：列表缩进，多行文字用 | 块。"""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


BlockDumper.add_representer(str, lambda dumper, value: dumper.represent_scalar(
    "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None))


async def main() -> None:
    parser = argparse.ArgumentParser(description="生成海龟汤题目，输出 puzzles.yaml 格式")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--save", action="store_true", help="同时加入用户目录的题库")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    library = Library()
    generator = PuzzleGenerator(Settings.from_environment())
    made: list[Generated] = []
    try:
        for index in range(args.count):
            try:
                item = await generator.generate([*library.puzzles(), *(m.puzzle for m in made)])
            except Exception:
                logger.warning("第 %d 道题生成失败", index + 1, exc_info=True)
                continue
            if args.save:
                library.add(item)
            made.append(item)
            logger.info("审核分数：%s", {name: round(value, 2) for name, value in item.review.items()})
    finally:
        await generator.close()
    yaml.dump({"puzzles": [item.puzzle.model_dump(mode="json") for item in made]}, sys.stdout,
              Dumper=BlockDumper, allow_unicode=True, sort_keys=False, width=1000)


if __name__ == "__main__":
    asyncio.run(main())
