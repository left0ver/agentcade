import asyncio
import json
import os
import shutil
import signal
import tomllib
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from openai import AsyncOpenAI
from pydantic import BaseModel

from guess_animal.config import ROOT, Settings


class LLMProvider(Protocol):
    async def generate(self, system: str, request: dict, schema: dict) -> str: ...

    async def close(self) -> None: ...


def format_system_prompt(system: str, schema: dict) -> str:
    return (
        system
        + "\n返回值必须符合以下完整 JSON Schema，包含其中所有 $defs 和 $ref 定义。"
        + "请输出完整的 JSON 对象，并闭合所有数组和对象。\n```json\n"
        + json.dumps(schema, ensure_ascii=False, indent=2)
        + "\n```\n"
    )


class OpenAIProvider:
    def __init__(self, settings: Settings):
        self.model = settings.llm.model_name
        self.timeout = settings.llm.timeout_seconds
        self.extra_body = settings.llm.extra_body
        self.client = AsyncOpenAI(
            api_key=settings.llm.api_key,
            base_url=settings.llm.base_url,
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

    async def close(self) -> None:
        await self.client.close()


async def run_cli(command: list[str], prompt: str, directory: Path, timeout: int) -> str:
    environment = os.environ.copy()
    for key in ("DASHSCOPE_API_KEY", "TYPESAFE_API_KEY", "LLM_API_KEY"):
        environment.pop(key, None)
    temporary = directory / "temporary"
    temporary.mkdir()
    environment.update(TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
    stdout_path = directory / "stdout.log"
    stderr_path = directory / "stderr.log"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            cwd=directory,
            env=environment,
            start_new_session=os.name == "posix",
        )
        try:
            async with asyncio.timeout(timeout):
                await process.communicate(prompt.encode("utf-8"))
        finally:
            if process.returncode is None:
                # 取消或超时时终止本次调用；进程可能恰好已经退出。
                with suppress(ProcessLookupError):
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                await process.wait()
    if process.returncode != 0:
        raise RuntimeError(
            f"LLM 工具退出码为 {process.returncode}，调用日志：{stdout_path}、{stderr_path}"
        )
    return stdout_path.read_text(encoding="utf-8")


class ClaudeResult(BaseModel):
    type: Literal["result"]
    subtype: str
    is_error: bool
    structured_output: dict[str, Any] | None = None


def codex_server_overrides() -> list[str]:
    config_path = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    if not config_path.exists():
        return []
    with config_path.open("rb") as source:
        config = tomllib.load(source)
    arguments = []
    for name in config.get("mcp_servers", {}):
        arguments.extend(("--config", f"mcp_servers.{name}.enabled=false"))
    for name in config.get("plugins", {}):
        arguments.extend(("--config", f"plugins.{name}.enabled=false"))
    return arguments


class LocalCLIProvider:
    def __init__(self, settings: Settings):
        self.name = settings.llm.provider
        command = settings.llm.command or {"claude_code": "claude", "codex": "codex"}[self.name]
        self.command = shutil.which(command)
        if self.command is None:
            raise FileNotFoundError(f"找不到 {self.name} 命令：{command}")
        self.model = settings.llm.model_name
        self.timeout = settings.llm.timeout_seconds

    async def generate(self, system: str, request: dict, schema: dict) -> str:
        directory = ROOT / ".artifacts" / "provider_runs" / self.name / uuid4().hex
        directory.mkdir(parents=True)
        prompt = (
            format_system_prompt(system, schema)
            + "\n直接根据以下要求生成 JSON 数据。全部信息已经提供，无需调用工具或读取、修改文件。\n"
            + json.dumps(request, ensure_ascii=False)
        )
        (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        schema_path = directory / "schema.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        if self.name == "claude_code":
            command = [
                self.command,
                "--print",
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(schema, ensure_ascii=False),
                "--tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--disable-slash-commands",
                "--no-session-persistence",
                "--permission-mode",
                "dontAsk",
            ]
            if self.model:
                command.extend(("--model", self.model))
            output = await run_cli(command, prompt, directory, self.timeout)
            result = ClaudeResult.model_validate_json(output)
            if result.is_error or result.subtype != "success" or result.structured_output is None:
                raise RuntimeError(f"Claude Code 未返回完整内容，调用日志：{directory}")
            return json.dumps(result.structured_output, ensure_ascii=False)
        output_path = directory / "result.json"
        command = [
            self.command,
            "exec",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--config",
            'approval_policy="never"',
            "--config",
            "features.shell_tool=false",
            "--config",
            "features.multi_agent=false",
            "--config",
            "features.apps=false",
            "--config",
            "project_doc_max_bytes=0",
            "--config",
            'web_search="disabled"',
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        command.extend(codex_server_overrides())
        if self.model:
            command.extend(("--model", self.model))
        command.append("-")
        await run_cli(command, prompt, directory, self.timeout)
        return output_path.read_text(encoding="utf-8")

    async def close(self) -> None:
        pass


def create_provider(settings: Settings) -> LLMProvider:
    if settings.llm.provider == "openai":
        return OpenAIProvider(settings)
    return LocalCLIProvider(settings)
