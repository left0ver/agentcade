import asyncio
import json
import logging

from openai import AsyncOpenAI

from agentcade.config import LLMSettings
from agentcade.tools import ToolRegistry

logger = logging.getLogger(__name__)


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
            max_retries=2,
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

    async def run_tools(
        self, messages: list[dict], tools: ToolRegistry, *, max_steps: int = 12,
    ) -> str:
        """执行 ReAct 循环：把模型和工具消息追加到 messages，返回模型的最终文本。"""
        definitions = tools.definitions()
        tool_options = ({"tools": [{"type": "function", "function": item} for item in definitions],
                         "parallel_tool_calls": False} if definitions else {})
        for _ in range(max_steps):
            async with asyncio.timeout(self.timeout):
                response = await self.client.chat.completions.create(
                    model=self.model, messages=messages,
                    extra_body=self.extra_body, **tool_options,
                )
            choice = response.choices[0]
            message = choice.message
            if choice.finish_reason == "stop":
                messages.append({"role": "assistant", "content": message.content or ""})
                return message.content or ""
            if choice.finish_reason != "tool_calls" or not message.tool_calls:
                raise RuntimeError(f"模型工具调用没有完成：{choice.finish_reason}")
            messages.append(message.model_dump(exclude_none=True))
            for call in message.tool_calls:
                try:
                    result = await tools.call(
                        call.function.name, json.loads(call.function.arguments),
                    )
                except Exception as error:
                    # 参数不合法或工具执行失败时把错误交还给模型，由它重试或向用户说明。
                    logger.warning("工具 %s 执行失败：%s", call.function.name, error,
                                   exc_info=True)
                    result = {"error": f"{type(error).__name__}: {error}"[:500]}
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result, ensure_ascii=False)})
                if tools.completed.is_set():
                    return ""
        raise RuntimeError(f"模型连续 {max_steps} 轮调用工具后仍未给出回复")

    async def close(self) -> None:
        await self.client.close()
