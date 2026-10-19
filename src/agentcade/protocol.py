from typing import Annotated, Literal, TypeVar

from a2a.helpers import new_data_part
from a2a.types import Part
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = "desktop-game/6"


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


class Gesture(Model):
    """点头或摇头。"""

    type: Literal["gesture"] = "gesture"
    name: Literal["nod", "shake_head"]


class Expression(Model):
    """屏幕显示开心或抱歉的表情，留到接下来这句话说完。"""

    type: Literal["expression"] = "expression"
    name: Literal["happy", "sorry"]


class Draw(Model):
    """在屏幕上逐笔画出图案，每一项是一步的完整矢量路径，最后一步留在屏幕上。"""

    type: Literal["draw"] = "draw"
    paths: list[str] = Field(min_length=1, max_length=64)


class Progress(Model):
    """显示进度，预计需要的秒数；下一次画图或清屏时结束。"""

    type: Literal["progress"] = "progress"
    seconds: int = Field(gt=0, le=600)


class Clear(Model):
    """屏幕恢复待机画面，有图案或表情时留到接下来这句话说完再换。"""

    type: Literal["clear"] = "clear"


Directive = Annotated[Gesture | Expression | Draw | Progress | Clear, Field(discriminator="type")]

# 指令需要游戏声明的机器人能力；没有列出的指令只用到所有机器人都有的基础能力。
CAPABILITIES = {"gesture": "motion", "draw": "expression.vector_accessory.v1"}


def presentations(directives: list[Directive]) -> list[Presentation]:
    """把设备指令转换成输出模块执行的内容，相邻的表情和动作合成一次反应。"""
    outputs: list[Presentation] = []
    for directive in directives:
        last = outputs[-1] if outputs else None
        if isinstance(directive, Gesture):
            if last is not None and last.expression and not last.gesture and not last.text:
                last.gesture = directive.name
            else:
                outputs.append(Presentation(gesture=directive.name))
        elif isinstance(directive, Expression):
            if last is not None and last.gesture and not last.expression and not last.text:
                last.expression = directive.name
            else:
                outputs.append(Presentation(expression=directive.name))
        elif isinstance(directive, Draw):
            outputs.append(Presentation(paths=directive.paths))
        elif isinstance(directive, Progress):
            outputs.append(Presentation(progress_seconds=directive.seconds))
        else:
            outputs.append(Presentation(clear=True))
    return outputs


class GameInput(Model):
    """主 Agent 发给游戏 Agent 的一条用户输入。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    protocol: Literal["desktop-game/6"] = PROTOCOL_VERSION
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

    中间回复可以只有设备指令；最后一条回复必须有要播报的话。主 Agent 先执行设备指令，再播报。
    """

    protocol: Literal["desktop-game/6"] = PROTOCOL_VERSION
    text: str = Field(default="", max_length=500, description="主 Agent 原样播报的面向用户回复")
    directives: list[Directive] = Field(
        default_factory=list, max_length=16, description="按顺序执行的设备指令",
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
