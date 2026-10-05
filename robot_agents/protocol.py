from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = "desktop-game/1"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GameDescriptor(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)


class Presentation(Model):
    text: str = Field(default="", max_length=500)
    paths: list[str] = Field(default_factory=list)
    expression: Literal["happy", "sorry", "thinking"] | None = None
    gesture: Literal["nod", "shake_head"] | None = None
    restore_path: str | None = None
    clear: bool = False
    progress_seconds: int | None = Field(default=None, gt=0)


class PublicState(Model):
    phase: str = "等待开始"
    status: str = ""
    commands: list[str] = Field(default_factory=list)
    revision: int = Field(default=0, ge=0)
    active: bool = True
    idle_seconds: int | None = Field(default=None, gt=0)


class GameRequest(Model):
    protocol: Literal["desktop-game/1"] = PROTOCOL_VERSION
    request_id: str = Field(default_factory=lambda: uuid4().hex)
    action: Literal["start", "input", "prepare", "advance", "timeout", "end"]
    text: str = ""
    revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_input(self):
        if self.action == "input" and (not self.text or self.revision is None):
            raise ValueError("游戏输入需要原文和题目版本")
        return self


class GameResult(Model):
    protocol: Literal["desktop-game/1"] = PROTOCOL_VERSION
    state: PublicState
    outputs: list[Presentation] = Field(default_factory=list)
    next_action: Literal["prepare", "advance"] | None = None
    handled: bool = True

    @model_validator(mode="after")
    def validate_completion(self):
        if not self.state.active and self.next_action is not None:
            raise ValueError("已结束的游戏不能继续执行任务")
        return self


class GameAgent(Protocol):
    descriptor: GameDescriptor

    async def handle(self, request: GameRequest) -> GameResult: ...

    async def close(self) -> None: ...
