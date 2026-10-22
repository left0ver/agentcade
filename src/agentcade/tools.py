import asyncio
import inspect
import json
import re
import typing
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


# 大模型接口对工具名的限制。
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
# 工具方法里叫这个名字的参数由框架传入当前这一轮，不交给大模型填写。
TURN_PARAMETER = "turn"


@dataclass(frozen=True)
class ToolSpec:
    """用 @tool 声明的工具：发给大模型的定义和对应的方法。"""
    name: str
    description: str
    inputs: type[BaseModel]
    function: Callable[..., Any]
    takes_turn: bool


def tool(description: str, *, name: str | None = None):
    """把游戏 Agent 的方法声明成工具。description 原样发给大模型，name 不写时用方法名。

    参数 turn 由框架传入当前这一轮；其余参数由大模型填写，必须写类型注解，有默认值的是可选参数，
    取值范围和说明写成 Annotated[int, Field(ge=1, le=20, description="用户猜的数")]。
    方法可以是同步或 async 的，返回能转成 JSON 的字典。
    """
    if not isinstance(description, str):
        raise TypeError('@tool 要带上工具说明，写成 @tool("说明")')

    def decorate(function):
        function.__tool__ = tool_spec(function, description, name)
        return function

    return decorate


def tool_spec(function, description: str, name: str | None) -> ToolSpec:
    """检查方法的签名，生成工具定义；写错的地方在定义类时就报错。"""
    where = function.__qualname__
    name = function.__name__ if name is None else name
    if not TOOL_NAME.fullmatch(name):
        raise ValueError(f"{where} 的工具名 {name!r} 不合法：只能用字母、数字、下划线和短横线，最长 64 个字符")
    if not description.strip():
        raise ValueError(f"{where} 的工具说明不能为空")
    hints = typing.get_type_hints(function, include_extras=True)
    fields, takes_turn = {}, False
    # 第一个参数是 self。
    for parameter in list(inspect.signature(function).parameters.values())[1:]:
        if parameter.name == TURN_PARAMETER:
            takes_turn = True
            continue
        if parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
            raise TypeError(f"{where} 的参数 {parameter.name} 要写成普通参数，不能用 *args、**kwargs 或仅限位置的参数")
        if parameter.name not in hints:
            raise TypeError(f"{where} 的参数 {parameter.name} 缺少类型注解")
        default = ... if parameter.default is parameter.empty else parameter.default
        fields[parameter.name] = (hints[parameter.name], default)
    try:
        inputs = create_model(f"{name}_input", __base__=ToolInput, **fields)
        inputs.model_json_schema()
    except Exception as error:
        raise TypeError(f"{where} 的参数类型生成不了工具定义：{error}") from error
    return ToolSpec(name, description, inputs, function, takes_turn)


def collect_tools(cls: type) -> tuple[ToolSpec, ...]:
    """按定义顺序收集类上用 @tool 声明的方法；子类重写同名方法时以子类为准。"""
    found = {}
    for klass in reversed(cls.__mro__):
        for attribute, value in vars(klass).items():
            found[attribute] = getattr(value, "__tool__", None)
    specs = tuple(spec for spec in found.values() if isinstance(spec, ToolSpec))
    owners = {}
    for spec in specs:
        if spec.name in owners:
            raise ValueError(f"{cls.__name__} 的工具名 {spec.name} 重复："
                             f"{owners[spec.name]} 和 {spec.function.__qualname__}")
        owners[spec.name] = spec.function.__qualname__
    return specs


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
