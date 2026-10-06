import asyncio
import importlib
import socket
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import httpx
import uvicorn
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_data_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Role,
    SendMessageRequest,
    Task,
    TaskState,
    TaskStatus,
)
from google.protobuf.json_format import MessageToDict
from pydantic import Field, TypeAdapter
from starlette.applications import Starlette

from robot_agents.protocol import (
    PROTOCOL_VERSION,
    GameDescriptor,
    GameRequest,
    GameResult,
    Model,
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


class GameExecutor(AgentExecutor):
    def __init__(self, factory, settings):
        self.factory, self.settings = factory, settings
        self.agent = None
        self.context_id = None
        self.lock = asyncio.Lock()
        self.cache = OrderedDict()
        self.preparing: asyncio.Task | None = None

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        if message is None or len(message.parts) != 1 or not message.parts[0].HasField("data"):
            raise ValueError("游戏请求必须包含一个结构化数据 Part")
        request = GameRequest.model_validate(MessageToDict(message.parts[0].data))
        key = (context.context_id, request.request_id)
        # 生成任务独立等待，结束请求仍然能够进入并释放资源。
        async with self.lock:
            if key in self.cache:
                result = self.cache[key]
            else:
                if request.action == "start":
                    if self.agent is not None:
                        raise RuntimeError("当前游戏尚未退出")
                    self.agent = self.factory(self.settings)
                    self.context_id = context.context_id
                if self.agent is None or self.context_id != context.context_id:
                    raise ValueError("游戏会话已经结束或不匹配")
                if request.action == "end" and self.preparing is not None:
                    self.preparing.cancel()
                    await asyncio.gather(self.preparing, return_exceptions=True)
                    self.preparing = None
                if request.action == "prepare":
                    if self.preparing is not None:
                        raise RuntimeError("题目生成任务已经存在")
                    self.preparing = asyncio.create_task(self.agent.handle(request))
                    result = None
                else:
                    result = await self.agent.handle(request)
                    if not result.state.active:
                        await self.agent.close()
                        self.agent = None
                        self.context_id = None
                if result is not None:
                    self.remember(key, result)
        if result is None:
            preparing = self.preparing
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            await event_queue.enqueue_event(Task(
                id=context.task_id, context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING), history=[message],
            ))
            try:
                result = await preparing
            except asyncio.CancelledError:
                await updater.cancel()
                return
            async with self.lock:
                if self.preparing is preparing:
                    self.preparing = None
                self.remember(key, result)
            await updater.add_artifact([new_data_part(result.model_dump(mode="json"))])
            await updater.complete()
            return
        await event_queue.enqueue_event(Message(
            message_id=uuid4().hex, context_id=context.context_id,
            role=Role.ROLE_AGENT, parts=[new_data_part(result.model_dump(mode="json"))],
        ))

    def remember(self, key, result):
        self.cache[key] = result
        while len(self.cache) > 256:
            self.cache.popitem(last=False)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ValueError("游戏使用 end 消息结束会话")

    async def close(self):
        if self.preparing is not None:
            self.preparing.cancel()
            await asyncio.gather(self.preparing, return_exceptions=True)
        if self.agent is not None:
            await self.agent.close()
            self.agent = None


class EmbeddedServer(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()


@asynccontextmanager
async def serve_game(factory, settings):
    descriptor = factory.descriptor
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    card = AgentCard(
        name=descriptor.name, description=descriptor.description, version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["application/json"], default_output_modes=["application/json"],
        skills=[AgentSkill(
            id=descriptor.id, name=descriptor.name, description=descriptor.description,
            tags=[PROTOCOL_VERSION, *[
                f"capability:{name}" for name in descriptor.required_capabilities
            ]], examples=descriptor.aliases,
        )],
        supported_interfaces=[AgentInterface(
            protocol_binding="JSONRPC", protocol_version="1.0", url=url + "/a2a",
        )],
    )
    executor = GameExecutor(factory, settings)
    handler = DefaultRequestHandler(
        agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card,
    )
    app = Starlette(routes=(
        create_agent_card_routes(card)
        + create_jsonrpc_routes(handler, rpc_url="/a2a")
    ))
    server = EmbeddedServer(uvicorn.Config(
        app, log_level="error", lifespan="off", timeout_graceful_shutdown=10,
    ))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("A2A 服务没有启动")
                await asyncio.sleep(0.01)
        yield url
    finally:
        await executor.close()
        server.should_exit = True
        await serving
        listener.close()


class GameConnection:
    def __init__(self, descriptor, client):
        self.descriptor, self.client = descriptor, client

    async def send(self, context_id: str, request: GameRequest) -> GameResult:
        message = Message(
            message_id=request.request_id, context_id=context_id, role=Role.ROLE_USER,
            parts=[new_data_part(request.model_dump(mode="json"))],
        )
        result = None
        async for event in self.client.send_message(SendMessageRequest(message=message)):
            if event.HasField("message"):
                reply = event.message
                parts = reply.parts
            elif event.HasField("task"):
                reply = event.task
                if reply.status.state != TaskState.TASK_STATE_COMPLETED:
                    raise RuntimeError(f"游戏任务未完成：{TaskState.Name(reply.status.state)}")
                parts = [part for artifact in reply.artifacts for part in artifact.parts]
            else:
                raise ValueError("游戏接口需要返回结构化 Message 或已完成的 Task")
            if reply.context_id != context_id or len(parts) != 1:
                raise ValueError("游戏响应会话或数据数量不匹配")
            if not parts[0].HasField("data") or result is not None:
                raise ValueError("游戏响应必须包含唯一结构化结果")
            result = GameResult.model_validate(MessageToDict(parts[0].data))
        if result is None:
            raise ValueError("游戏 Agent 没有返回结果")
        return result


@asynccontextmanager
async def connect_games(path: Path):
    entries = Catalog.validate_json(path.read_text(encoding="utf-8"))
    if len({item.id for item in entries}) != len(entries):
        raise ValueError("游戏注册 ID 不能重复")
    async with AsyncExitStack() as resources:
        games = {}
        for entry in entries:
            if isinstance(entry, LocalGame):
                module, name = entry.factory.split(":", 1)
                factory = getattr(importlib.import_module(module), name)
                descriptor = GameDescriptor.model_validate(factory.descriptor.model_dump())
                if descriptor.id != entry.id:
                    raise ValueError("注册 ID 与游戏声明不一致")
                settings = factory.load_settings()
                timeout = settings.llm.timeout_seconds + 30
                url = await resources.enter_async_context(serve_game(factory, settings))
            else:
                url = entry.url
                timeout = entry.timeout_seconds
            http = await resources.enter_async_context(httpx.AsyncClient(
                timeout=timeout, trust_env=False,
            ))
            card = await A2ACardResolver(http, url).get_agent_card()
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
                streaming=False, httpx_client=http, supported_protocol_bindings=["JSONRPC"],
            ))
            resources.push_async_callback(client.close)
            games[entry.id] = GameConnection(descriptor, client)
        yield games
