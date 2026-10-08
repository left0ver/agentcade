import asyncio
import json

from openai import AsyncOpenAI

from robot_agents.config import LLMSettings
from robot_agents.tools import ToolRegistry


def format_system_prompt(system: str, schema: dict) -> str:
    return (
        system
        + "\n返回值必须符合以下完整 JSON Schema，包含其中所有 $defs 和 $ref 定义。"
        + "请输出完整的 JSON 对象，并闭合所有数组和对象。\n```json\n"
        + json.dumps(schema, ensure_ascii=False, indent=2)
        + "\n```\n"
    )


class OpenAIProvider:
    def __init__(self, settings: LLMSettings):
        self.model = settings.model_name
        self.timeout = settings.timeout_seconds
        self.extra_body = settings.extra_body
        self.client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=self.timeout,
            max_retries=0,
        )

    async def generate(self, system: str, request: dict, schema: dict) -> str:
        async with asyncio.timeout(self.timeout):
            result = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": format_system_prompt(system, schema)},
                    {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                extra_body=self.extra_body,
            )
        choice = result.choices[0]
        if choice.finish_reason != "stop" or not choice.message.content:
            raise RuntimeError(f"LLM 未返回完整内容，结束原因：{choice.finish_reason}")
        return choice.message.content

    async def run_tools(self, system: str, request: dict, tools: ToolRegistry) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ]
        while True:
            async with asyncio.timeout(self.timeout):
                response = await self.client.chat.completions.create(
                    model=self.model, messages=messages,
                    tools=[{"type": "function", "function": item}
                           for item in tools.definitions()],
                    parallel_tool_calls=False, extra_body=self.extra_body,
                )
            choice = response.choices[0]
            message = choice.message
            if choice.finish_reason == "stop":
                return message.content or ""
            if choice.finish_reason != "tool_calls" or not message.tool_calls:
                raise RuntimeError(f"模型工具调用没有完成：{choice.finish_reason}")
            messages.append(message.model_dump(exclude_none=True))
            for call in message.tool_calls:
                result = await tools.call(call.function.name, json.loads(call.function.arguments))
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result, ensure_ascii=False)})
                if tools.completed.is_set():
                    return ""

    async def close(self) -> None:
        await self.client.close()
