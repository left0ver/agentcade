import asyncio
import hashlib
import json
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator

from guess_animal.animals import Animal, Stroke, encode_vector
from guess_animal.config import ROOT, Settings

Coordinate = Annotated[int, Field(strict=True, ge=0, le=411)]
Name = Annotated[str, Field(min_length=1, max_length=24)]


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


class GeneratedStroke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: int = Field(strict=True, ge=1, le=48)
    points: list[tuple[Coordinate, Coordinate]] = Field(min_length=2, max_length=48)


class GeneratedAnimal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    name: Name
    aliases: list[Name] = Field(max_length=8)
    hint: str = Field(min_length=5, max_length=80)
    strokes: list[GeneratedStroke] = Field(min_length=3, max_length=12)

    def animal(self) -> Animal:
        return Animal(
            id=self.id,
            name=self.name,
            aliases=tuple(self.aliases),
            hint=self.hint,
            strokes=tuple(Stroke(tuple(stroke.points), stroke.width) for stroke in self.strokes),
        )

    @model_validator(mode="after")
    def validate_drawing(self) -> "GeneratedAnimal":
        animal = self.animal()
        points = {point for stroke in animal.strokes for point in stroke.points}
        if len(points) < 12:
            raise ValueError("动物图案至少需要十二个不同坐标")
        if max(x for x, _ in points) - min(x for x, _ in points) < 120:
            raise ValueError("动物图案宽度不足")
        if max(y for _, y in points) - min(y for _, y in points) < 120:
            raise ValueError("动物图案高度不足")
        if any(
            normalize_name(name) in normalize_name(self.hint) for name in [self.name, *self.aliases]
        ):
            raise ValueError("动物提示中不能包含答案或认可别名")
        return self


class GeneratedRound(BaseModel):
    model_config = ConfigDict(extra="forbid")
    animals: list[GeneratedAnimal] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_unique(self) -> "GeneratedRound":
        names: set[str] = set()
        ids: set[str] = set()
        drawings: set[str] = set()
        for animal in self.animals:
            candidates = {normalize_name(name) for name in [animal.name, *animal.aliases]}
            drawing = encode_vector(animal.animal().strokes)
            if names & candidates or animal.id in ids or drawing in drawings:
                raise ValueError("同一局的动物、别名和图案必须互不重复")
            names.update(candidates)
            ids.add(animal.id)
            drawings.add(drawing)
        return self


class GenerationHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recent_names: list[str] = Field(default_factory=list)
    drawing_hashes: list[str] = Field(default_factory=list)

    def updated(self, animals: tuple[Animal, ...]) -> "GenerationHistory":
        names = [
            normalize_name(name) for animal in animals for name in (animal.name, *animal.aliases)
        ]
        if set(names) & set(self.recent_names):
            raise ValueError("DeepSeek 返回了近期已经使用的动物")
        hashes = [
            hashlib.sha256(bytes.fromhex(encode_vector(animal.strokes))).hexdigest()
            for animal in animals
        ]
        if set(hashes) & set(self.drawing_hashes):
            raise ValueError("DeepSeek 返回了已经生成过的相同图案")
        return GenerationHistory(
            recent_names=(self.recent_names + names)[-80:],
            drawing_hashes=self.drawing_hashes + hashes,
        )


SYSTEM_PROMPT = """你为桌面机器人的中文猜动物游戏设计题目及原创简笔画。
返回且只返回一个 JSON 对象，结构为 {"animals": [...]}。
每只动物对象的格式示例：
{"id":"animal_slug","name":"动物名称","aliases":["同义名称"],
"hint":"描述习性或外形的中文提示。",
"strokes":[{"width":6,"points":[[100,100],[160,60],[210,100]]}]}
示例只解释字段，实际每只动物至少需要三条笔画和十二个不同坐标。

选择真实存在、中文名称清楚、容易通过外形辨识的动物。
自由选择物种，不限定动物目录，兼顾哺乳类、鸟类、水生动物、爬行类等。
同一局不能出现相同动物、同义名称或仅品种不同的同类动物。
aliases 只包含确切同义词，不能包含其他物种、宽泛分类或含糊简称。
hint 描述习性或外形，不得包含 name 或任何 alias，也不能直接公布答案。

使用正面或略侧的卡通动物头像，结合已有机器人眼睛进行设计。
屏幕为 412×412，x 从左到右、y 从上到下，所有坐标为 0 到 411 的整数。
机器人眼睛约位于 (140,206) 和 (272,206)，中央区域保留给眼睛。
头部主轮廓应覆盖 x=80 到 x=332，顶部约 y=120，下巴约 y=330。
耳朵或角可以向上延伸到 y=25，嘴部、鼻子或喙安排在 y=255 到 y=330。
不要另外画一对眼睛。不能把头部缩在屏幕上半部，也不能让已有眼睛位于轮廓外面。
以耳朵、嘴、喙、角、胡须、鼻子、轮廓等鲜明特征表达动物。
轮廓应占据屏幕主要区域，四周留出空间，避免线条交叉遮住眼睛。
每条笔画为依次连接的折线，曲线使用多个坐标近似，闭合轮廓需重复首点。
每只动物最多十二条笔画，每条最多四十八个点，全部笔画总计最多一百九十二个点。
笔画 width 为 4 到 10；建议每只动物总计 40 到 100 个点，保持简洁和辨识度。
按外轮廓、显著特征、局部装饰的顺序排列笔画。
每次重新构思轮廓、耳朵姿态、表情和构图，不能复用固定坐标模板。
不要输出文字、字母、数字、答案标签或任何背景物体。
"""


class AnimalGenerator:
    def __init__(self, settings: Settings, history_path: Path | None = None):
        self.client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=120,
            max_retries=0,
        )
        self.model = settings.deepseek_model
        self.history_path = history_path or ROOT / ".artifacts" / "generation_history.json"
        self.history = (
            GenerationHistory.model_validate_json(self.history_path.read_text(encoding="utf-8"))
            if self.history_path.exists()
            else GenerationHistory()
        )
        self.lock = asyncio.Lock()

    async def generate(self, count: int = 1) -> tuple[Animal, ...]:
        if not 1 <= count <= 10:
            raise ValueError("每次生成必须包含一至十种动物")
        async with self.lock:
            prompt = {
                "animal_count": count,
                "exclude_names": self.history.recent_names,
                "request_id": str(uuid4()),
                "composition": secrets.choice(
                    (
                        "活泼的表情与不对称的耳朵姿态",
                        "圆润简洁的轮廓与夸张的物种特征",
                        "轻微侧向的头部与好奇的表情",
                        "有趣的嘴部形状与舒展的耳朵或角",
                    )
                ),
                "instruction": "请生成指定数量的全新动物题目，避开近期动物及其同义名称。",
            }
            async with asyncio.timeout(130):
                result = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                    ],
                    response_format={"type": "json_object"},
                    extra_body={"thinking": {"type": "enabled"}},
                    reasoning_effort="low",
                    max_tokens=32768,
                )
            choice = result.choices[0]
            if choice.finish_reason != "stop" or not choice.message.content:
                raise RuntimeError(
                    f"DeepSeek 未返回完整的动物题目，结束原因：{choice.finish_reason}"
                )
            generated = GeneratedRound.model_validate_json(choice.message.content)
            if len(generated.animals) != count:
                raise ValueError("DeepSeek 返回的动物数量与请求不一致")
            animals = tuple(item.animal() for item in generated.animals)
            history = self.history.updated(animals)
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            pending = self.history_path.with_suffix(".pending")
            pending.write_text(history.model_dump_json(indent=2), encoding="utf-8")
            pending.replace(self.history_path)
            self.history = history
            archive = self.history_path.parent / "generated"
            archive.mkdir(exist_ok=True)
            (archive / f"{time.time_ns()}.json").write_text(
                generated.model_dump_json(indent=2),
                encoding="utf-8",
            )
            return animals

    async def close(self) -> None:
        await self.client.close()
