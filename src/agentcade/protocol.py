from typing import Literal, TypeVar

from a2a.helpers import new_data_part
from a2a.types import Part
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

PROTOCOL_VERSION = "desktop-game/7"


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
    # 选填，写进名片，用户安装远程游戏时在确认页上看到。
    version: str = Field(default="1.0.0", min_length=1, max_length=32)
    author: str = Field(default="", max_length=64)
    homepage: str = Field(default="", max_length=512)

    def catalog(self) -> dict:
        """给主 Agent 看的游戏目录条目，只包含选游戏和介绍玩法用得到的内容。"""
        return self.model_dump(include={"id", "name", "description", "summary", "aliases"})


def integral(value):
    """A2A 的结构化数据里数字都是浮点数，整数值还原成 int，SDK 要整数的参数才能通过。"""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [integral(item) for item in value]
    if isinstance(value, dict):
        return {key: integral(item) for key, item in value.items()}
    return value


class SdkCall(Model):
    """一次机器人 SDK 调用，例如 lights.set_color("#00FF00", brightness=0.8)。

    call 是 SDK 里的“模块.方法”，参数和 SDK 一样；主 Agent 检查游戏能不能调用、有没有声明能力后，
    在机器人上执行同名方法。
    """

    call: str = Field(pattern=r"^[a-z_]+\.[a-z_]+$", description="SDK 里的模块.方法，例如 lights.set_color")
    args: list[JsonValue] = Field(default_factory=list)
    kwargs: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("args", "kwargs")
    @classmethod
    def restore_integers(cls, value):
        return integral(value)


class Presentation(Model):
    """主 Agent 的一条输出：一句话、一次游戏的 SDK 调用，或者一局结束后复位设备。"""

    text: str = Field(default="", max_length=500)
    call: SdkCall | None = None
    reset: bool = False


class GameInput(Model):
    """主 Agent 发给游戏 Agent 的一条用户输入。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    protocol: Literal["desktop-game/7"] = PROTOCOL_VERSION
    kind: Literal["start", "message", "end"]
    text: str = Field(default="", description="用户原话，保持字词和标点")
    reason: Literal["user", "shutdown"] | None = Field(
        default=None, description="end 的原因：user 是用户要求结束，shutdown 是程序退出",
    )

    @model_validator(mode="after")
    def validate_text(self):
        # 程序退出时发送的 end 没有用户原话。
        if self.kind != "end" and not self.text.strip():
            raise ValueError("开局和局内输入需要用户原话")
        if (self.kind == "end") != (self.reason is not None):
            raise ValueError("只有 end 带有结束原因，并且必须带")
        return self


class GameReply(Model):
    """游戏 Agent 回给主 Agent 的一条回复：要播报的话和设备指令，一次请求至少回复一条。

    设备指令是机器人 SDK 调用。中间回复可以只有设备指令；最后一条回复必须有要播报的话。
    主 Agent 先执行设备指令，再播报。
    """

    protocol: Literal["desktop-game/7"] = PROTOCOL_VERSION
    text: str = Field(default="", max_length=500, description="主 Agent 原样播报的面向用户回复")
    directives: list[SdkCall] = Field(
        default_factory=list, max_length=16, description="按顺序执行的机器人 SDK 调用",
    )
    session: Literal["active", "ended"] = Field(
        default="active", description="本局是否继续，只有一次请求的最后一条回复可以是 ended",
    )

    @model_validator(mode="after")
    def validate_content(self):
        if not self.text and not self.directives:
            raise ValueError("回复至少要有要播报的话或设备指令")
        return self


Payload = TypeVar("Payload", GameInput, GameReply)


def to_part(payload: GameInput | GameReply) -> Part:
    return new_data_part(payload.model_dump(mode="json"))


def from_parts(model: type[Payload], parts) -> Payload:
    if len(parts) != 1 or not parts[0].HasField("data"):
        raise ValueError("游戏消息必须包含唯一一个结构化数据 Part")
    return model.model_validate(MessageToDict(parts[0].data))
