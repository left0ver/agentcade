import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

import observability


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NoInput(ToolInput):
    pass


class TextInput(ToolInput):
    text: str = Field(min_length=1, description="用户当前消息的完整原文")


def original_text_input(text: str):
    return create_model(
        "CurrentMessageInput", __base__=TextInput,
        text=(Literal[text], Field(description="本轮用户消息的完整原文，保持字词和标点")),
    )


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    inputs: type[BaseModel]
    handler: Callable[..., Awaitable[Any]]

    def definition(self):
        return {
            "name": self.name, "description": self.description,
            "parameters": self.inputs.model_json_schema(),
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool], *, terminal=()):
        self.tools = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools):
            raise ValueError("工具名称不能重复")
        self.calls: list[dict] = []
        self.lock = asyncio.Lock()
        self.terminal = set(terminal)
        self.completed = asyncio.Event()

    def definitions(self):
        return [tool.definition() for tool in self.tools.values()]

    async def call(self, name: str, arguments: dict):
        async with self.lock:
            if self.completed.is_set():
                raise ValueError("当前工具调用轮次已经结束")
            with observability.tool_call(name, arguments) as outcome:
                tool = self.tools[name]
                inputs = tool.inputs.model_validate(arguments)
                result = await tool.handler(**inputs.model_dump())
                # 在工具执行位置验证返回值可以通过模型接口传输。
                json.dumps(result, ensure_ascii=False, allow_nan=False)
                outcome["result"] = result
            self.calls.append({"name": name, "arguments": arguments, "result": result})
            if name in self.terminal:
                self.completed.set()
            return result
