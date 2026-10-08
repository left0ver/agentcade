import asyncio
import hashlib
import json
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from games.guess_animal.animals import Animal
from games.guess_animal.config import Settings
from robot_agents.config import ROOT
from robot_agents.providers import create_provider
from robot_agents.vector import Stroke, encode_vector

Coordinate = Annotated[int, Field(strict=True, ge=0, le=411)]
Coordinates = Annotated[list[Coordinate], Field(min_length=2, max_length=2)]
Name = Annotated[str, Field(min_length=1, max_length=24)]
Hint = Annotated[str, Field(min_length=4, max_length=16, pattern=r"^它")]
HISTORY_TTL_SECONDS = 3 * 24 * 60 * 60


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


class GeneratedStroke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: int = Field(strict=True, ge=1, le=48)
    points: list[Coordinates] = Field(min_length=2, max_length=48)


class GeneratedAnimal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    name: Name
    aliases: list[Name] = Field(max_length=8)
    hints: list[Hint] = Field(
        min_length=3,
        max_length=3,
        description="三个由宽泛到明显的完整中文短句，每条以它开头，只描述一个特征，长度为 4 至 16 个字符",
    )
    strokes: list[GeneratedStroke] = Field(min_length=3, max_length=12)

    def animal(self) -> Animal:
        return Animal(
            id=self.id,
            name=self.name,
            aliases=tuple(self.aliases),
            hints=tuple(self.hints),
            strokes=tuple(
                Stroke(tuple((x, y) for x, y in stroke.points), stroke.width)
                for stroke in self.strokes
            ),
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
        normalized_hints = [normalize_name(hint) for hint in self.hints]
        if len(set(normalized_hints)) != len(normalized_hints):
            raise ValueError("动物提示不能重复")
        if any(
            normalize_name(name) in hint
            for hint in normalized_hints
            for name in [self.name, *self.aliases]
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


class LegacyGenerationHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recent_names: list[str] = Field(default_factory=list)
    drawing_hashes: list[str] = Field(default_factory=list)


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: float = Field(ge=0, allow_inf_nan=False)
    names: list[str]
    drawing_hash: str

    @classmethod
    def from_animal(cls, animal: Animal, generated_at: float) -> "HistoryEntry":
        return cls(
            generated_at=generated_at,
            names=[normalize_name(name) for name in (animal.name, *animal.aliases)],
            drawing_hash=hashlib.sha256(bytes.fromhex(encode_vector(animal.strokes))).hexdigest(),
        )


class ArchivedAnimal(BaseModel):
    # 从生成归档中读取动物名称和图案。
    name: str
    aliases: list[str]
    strokes: list[GeneratedStroke]

    def history_entry(self, generated_at: float) -> HistoryEntry:
        strokes = tuple(
            Stroke(tuple((x, y) for x, y in stroke.points), stroke.width)
            for stroke in self.strokes
        )
        return HistoryEntry(
            generated_at=generated_at,
            names=[normalize_name(name) for name in (self.name, *self.aliases)],
            drawing_hash=hashlib.sha256(bytes.fromhex(encode_vector(strokes))).hexdigest(),
        )


class GenerationHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[HistoryEntry] = Field(default_factory=list)

    @property
    def recent_names(self) -> list[str]:
        return list(dict.fromkeys(name for entry in self.entries for name in entry.names))

    @property
    def drawing_hashes(self) -> list[str]:
        return [entry.drawing_hash for entry in self.entries]

    def active(self, now: float) -> "GenerationHistory":
        return GenerationHistory(entries=[
            entry for entry in self.entries if entry.generated_at > now - HISTORY_TTL_SECONDS
        ])

    @classmethod
    def load(cls, path: Path) -> "GenerationHistory":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if "entries" in data:
            return cls.model_validate(data)
        legacy = LegacyGenerationHistory.model_validate(data)
        entries = []
        for archive in sorted((path.parent / "generated").glob("*.json")):
            generated_at = int(archive.stem) / 1_000_000_000
            archived = json.loads(archive.read_text(encoding="utf-8"))
            for item in archived["animals"]:
                entry = ArchivedAnimal.model_validate(item).history_entry(generated_at)
                if entry.drawing_hash in legacy.drawing_hashes:
                    entries.append(entry)
        history = cls(entries=entries)
        if (set(legacy.drawing_hashes) - set(history.drawing_hashes)
                or set(legacy.recent_names) - set(history.recent_names)):
            raise ValueError("出题历史缺少生成时间，请恢复同目录 generated 中的原始归档")
        return history

    def updated(
        self, animals: tuple[Animal, ...], *, now: float | None = None,
    ) -> "GenerationHistory":
        generated_at = time.time() if now is None else now
        history = self.active(generated_at)
        entries = [HistoryEntry.from_animal(animal, generated_at) for animal in animals]
        names = {name for entry in entries for name in entry.names}
        if names & set(history.recent_names):
            raise ValueError("绘图 provider 返回了近期已经使用的动物")
        if {entry.drawing_hash for entry in entries} & set(history.drawing_hashes):
            raise ValueError("绘图 provider 返回了最近三天生成过的相同图案")
        return GenerationHistory(entries=history.entries + entries)


SYSTEM_PROMPT = """你为桌面机器人的中文猜动物游戏设计题目及原创简笔画。
返回且只返回一个 JSON 对象，结构为 {"animals": [...]}。
每只动物对象的格式示例：
{"id":"animal_slug","name":"动物名称","aliases":["同义名称"],
"hints":["它生活在草原上", "它喜欢吃树叶", "它的脖子很长"],
"strokes":[{"width":6,"points":[[100,100],[160,60],[210,100]]}]}
示例只解释字段，实际每只动物至少需要三条笔画和十二个不同坐标。

选择真实存在、中文名称清楚、容易通过外形辨识的动物。
优先选择日常生活、常见童话或动物园中广为人知、外形容易辨识的动物。
动物名称和别名应使用常用、容易读写的汉字，不要生成名称中含有不常见生僻字的动物。
自由选择物种，不限定动物目录，兼顾哺乳类、鸟类、水生动物、爬行类等。
同一局不能出现相同动物、同义名称或仅品种不同的同类动物。
aliases 只包含确切同义词，不能包含其他物种、宽泛分类或含糊简称。
hints 必须恰好包含三个简短中文提示，按照从宽泛到明显的顺序引导用户猜答案。
每条必须以“它”开头，包含主语，写成适合语音播报的自然、完整短句。
可以使用“它生活在……”“它喜欢……”“它会……”“它的……很……”等句式。
第一条给出生活环境或宽泛习性，第二条缩小范围，第三条给出鲜明的外形或行为特征。
每条只描述一个特征，建议 4 至 12 个字符，最多 16 个字符，不要把多个线索写进同一句。
三条提示提供不同的信息，不能重复、编号或包含 name、任何 alias，也不能直接公布答案。

在黑色画布上独立设计完整的白色动物全身简笔画，所有可见内容都由 strokes 绘制。
每只动物必须完整展示头部、躯干，以及该物种具有的四肢、翅膀、鳍、尾巴等身体部位。
根据物种选择最容易辨识全身形态的视角与姿态，身体各部位的连接关系和比例应当自然。
屏幕为 412×412，x 从左到右、y 从上到下，所有坐标为 0 到 411 的整数。
你必须绘制动物自己的眼睛，根据头部形状和视角决定眼睛的数量、位置和形状。
眼睛使用独立笔画绘制，可以使用小圆、短弧或短线，单只眼睛宽度约占头部宽度的 3% 到 8%。
正面视角通常需要两只眼睛，侧面视角通常只需一只可见眼睛，比例自然，轮廓清楚。
头部大小应与躯干匹配，并绘制耳朵、嘴、喙、角、胡须、鼻子等该物种需要的鲜明特征。
完整动物居中，占据屏幕主要区域，所有身体部位都必须在画面内，四周至少留出 20 像素。
全部笔画坐标的最大 x 与最小 x 之差、最大 y 与最小 y 之差，都必须至少为 120。
输出前检查这两个跨度；包括蜗牛、蛇等动物，也要通过全身姿态和构图满足最小宽高。
肢体和尾部舒展，避免身体轮廓、肢体与五官相互遮挡。
每条笔画为依次连接的折线，曲线使用多个坐标近似，闭合轮廓需重复首点。
每只动物最多十二条笔画，每条最多四十八个点，全部笔画总计最多一百九十二个点。
笔画 width 为 4 到 10；建议每只动物总计 80 到 160 个点，保持简洁和辨识度。
规划全部笔画的分配，将相连的身体轮廓与肢体合并为连续折线，为眼睛和物种特征保留笔画。
将眼睛所需笔画计入十二条笔画的总量，按躯干、头部、肢体和尾部、五官及其他特征的顺序排列。
每次重新构思全身轮廓、身体姿态、五官比例和构图，不能复用固定坐标模板。
不要输出文字、字母、数字、答案标签或任何背景物体。
"""


class AnimalGenerator:
    def __init__(self, settings: Settings, history_path: Path | None = None):
        self.provider = create_provider(settings.llm)
        self.history_path = history_path or ROOT / ".artifacts" / "generation_history.json"
        self.history = GenerationHistory.load(self.history_path)
        self.lock = asyncio.Lock()

    async def generate(self, count: int = 1) -> tuple[Animal, ...]:
        if not 1 <= count <= 10:
            raise ValueError("每次生成必须包含一至十种动物")
        async with self.lock:
            self.history = self.history.active(time.time())
            prompt = {
                "animal_count": count,
                "exclude_names": self.history.recent_names,
                "request_id": str(uuid4()),
                "composition": secrets.choice(
                    (
                        "侧面全身，展现自然行进的姿态与完整身体轮廓",
                        "略微转向观众的全身姿态，清楚展示躯干与肢体",
                        "完整全身，表现该动物自然活动时的姿态与鲜明物种特征",
                        "完整全身，头部好奇地转向一侧，肢体和尾部自然舒展",
                    )
                ),
                "instruction": "请生成指定数量的动物全身图案题目，避开最近三天的动物及其同义名称。",
            }
            output = await self.provider.generate(
                SYSTEM_PROMPT, prompt, GeneratedRound.model_json_schema()
            )
            generated = GeneratedRound.model_validate_json(output)
            if len(generated.animals) != count:
                raise ValueError("绘图 provider 返回的动物数量与请求不一致")
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
        await self.provider.close()
