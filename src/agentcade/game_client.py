import importlib
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.types import Message, Role, SendMessageRequest, TaskState
from pydantic import Field, TypeAdapter

import observability
from agentcade.config import SOURCE
from agentcade.game_server import serve_game
from agentcade.protocol import (
    PROTOCOL_VERSION,
    GameDescriptor,
    GameInput,
    GameReply,
    Model,
    from_parts,
    to_part,
)


class LocalGame(Model):
    kind: Literal["local"] = "local"
    id: str
    factory: str


class RemoteGame(Model):
    kind: Literal["remote"]
    id: str
    url: str
    timeout_seconds: int = Field(default=210, gt=0)


Catalog = TypeAdapter(list[Annotated[LocalGame | RemoteGame, Field(discriminator="kind")]])


class GameConnection:
    def __init__(self, descriptor, client):
        self.descriptor, self.client = descriptor, client

    async def send(
        self, context_id: str, request: GameInput, *,
        on_reply: Callable[[GameReply], Awaitable[None]] | None = None,
    ) -> GameReply:
        """发送一条输入。中间回复逐条交给 on_reply，返回这次请求的最后一条回复。"""
        message = Message(
            message_id=uuid4().hex, context_id=context_id, role=Role.ROLE_USER,
            parts=[to_part(request)],
        )
        final = None
        with observability.a2a_request(
            self.descriptor, context_id, message.message_id, request,
        ) as exchange:
            async for event in self.client.send_message(SendMessageRequest(message=message)):
                if event.HasField("task"):
                    update = event.task
                elif event.HasField("status_update"):
                    update = event.status_update
                else:
                    raise ValueError("游戏接口返回了协议之外的事件")
                state = update.status.state
                if state not in {TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_SUBMITTED,
                                 TaskState.TASK_STATE_COMPLETED}:
                    raise RuntimeError(f"游戏任务未完成：{TaskState.Name(state)}")
                if update.context_id != context_id:
                    raise ValueError("游戏响应会话不匹配")
                # Task 快照只报告状态，回复都通过状态更新携带的消息传递。
                if event.HasField("task") or not update.status.HasField("message"):
                    continue
                reply = from_parts(GameReply, update.status.message.parts)
                if state == TaskState.TASK_STATE_COMPLETED:
                    final = reply
                    continue
                if reply.session != "active":
                    raise ValueError("只有最后一条回复可以结束会话")
                exchange.update(reply)
                if on_reply is not None:
                    await on_reply(reply)
            if final is None:
                raise ValueError("游戏 Agent 没有返回最后一条回复")
            exchange.result = final
        return final


def discover(directory: Path) -> list[LocalGame]:
    """游戏目录下每个带 SKILL.md 和 agent.py 的子目录是一个本地游戏，按目录名排序。"""
    return [
        LocalGame(id=child.name, factory=f"{directory.name}.{child.name}.agent:GameAgent")
        for child in sorted(directory.iterdir())
        if (child / "SKILL.md").is_file() and (child / "agent.py").is_file()
    ]


@asynccontextmanager
async def connect_games(path: Path, device, directory: Path = SOURCE / "games"):
    # 本地游戏放进游戏目录就会被发现；注册文件只用来接入远程游戏或目录以外的游戏。
    entries = discover(directory)
    if path.is_file():
        entries += Catalog.validate_json(path.read_text(encoding="utf-8"))
    if len({item.id for item in entries}) != len(entries):
        raise ValueError("游戏 ID 不能重复")
    async with AsyncExitStack() as resources:
        games = {}
        for entry in entries:
            limits = httpx.Limits()
            if isinstance(entry, LocalGame):
                module, name = entry.factory.split(":", 1)
                factory = getattr(importlib.import_module(module), name)
                descriptor = factory.skill.descriptor
                if descriptor.id != entry.id:
                    raise ValueError(f"游戏目录名或注册 ID 与 SKILL.md 声明的 ID 不一致：{entry.id}")
                settings = factory.config.from_environment()
                timeout = settings.llm.timeout_seconds * 3 + 30
                url = await resources.enter_async_context(serve_game(factory, settings, device))
                # 本地请求完成后关闭连接，避免复用正被服务端空闲超时关闭的连接。
                limits = httpx.Limits(max_keepalive_connections=0)
            else:
                url = entry.url
                timeout = entry.timeout_seconds
            http = await resources.enter_async_context(httpx.AsyncClient(
                timeout=timeout, trust_env=False, limits=limits,
            ))
            card = await A2ACardResolver(http, url).get_agent_card()
            if not card.capabilities.streaming:
                raise ValueError("游戏 Agent 必须支持流式响应")
            skill = next((item for item in card.skills if item.id == entry.id), None)
            if skill is None or PROTOCOL_VERSION not in skill.tags:
                raise ValueError("Agent Card 缺少游戏 ID 或公共协议版本")
            if isinstance(entry, RemoteGame):
                descriptor = GameDescriptor(
                    id=entry.id, name=card.name, description=card.description,
                    aliases=list(skill.examples),
                    required_capabilities=[tag.removeprefix("capability:")
                                           for tag in skill.tags if tag.startswith("capability:")],
                )
            client = await create_client(card, client_config=ClientConfig(
                streaming=True, httpx_client=http, supported_protocol_bindings=["JSONRPC"],
            ))
            resources.push_async_callback(client.close)
            games[entry.id] = GameConnection(descriptor, client)
        yield games
