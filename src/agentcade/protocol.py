from typing import Literal, TypeVar

from a2a.helpers import new_data_part
from a2a.types import Part
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = "desktop-game/5"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GameDescriptor(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    # 开机问候里跟在游戏名后面念的一句简短介绍，不带句末标点。
    summary: str = Field(min_length=1, max_length=30, pattern=r"[^。！？.!?，,；;]$")
    aliases: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)


class Presentation(Model):
    text: str = Field(default="", max_length=500)
    paths: list[str] = Field(default_factory=list)
    expression: Literal["happy", "sorry"] | None = None
    gesture: Literal["nod", "shake_head"] | None = None
    clear: bool = False
    progress_seconds: int | None = Field(default=None, gt=0)


class GameInput(Model):
    """主 Agent 发给游戏 Agent 的一条用户输入。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    protocol: Literal["desktop-game/5"] = PROTOCOL_VERSION
    kind: Literal["start", "message", "end"]
    text: str = Field(default="", description="用户原话，保持字词和标点")

    @model_validator(mode="after")
    def validate_text(self):
        # 程序退出时发送的 end 没有用户原话。
        if self.kind != "end" and not self.text.strip():
            raise ValueError("开局和局内输入需要用户原话")
        return self


class GameReply(Model):
    """游戏 Agent 回给主 Agent 的一条播报内容，一次请求至少回复一条。"""

    protocol: Literal["desktop-game/5"] = PROTOCOL_VERSION
    text: str = Field(min_length=1, max_length=500, description="主 Agent 原样播报的面向用户回复")
    session: Literal["active", "ended"] = Field(
        default="active", description="本局是否继续，只有一次请求的最后一条回复可以是 ended",
    )


Payload = TypeVar("Payload", GameInput, GameReply)


def to_part(payload: GameInput | GameReply) -> Part:
    return new_data_part(payload.model_dump(mode="json"))


def from_parts(model: type[Payload], parts) -> Payload:
    if len(parts) != 1 or not parts[0].HasField("data"):
        raise ValueError("游戏消息必须包含唯一一个结构化数据 Part")
    return model.model_validate(MessageToDict(parts[0].data))
