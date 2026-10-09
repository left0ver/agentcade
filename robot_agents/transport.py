import asyncio
import importlib
import socket
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from pathlib import Path
from typing import Annotated, Literal

import httpx
import uvicorn
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_data_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import TaskUpdater
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
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.routing import Route

import observability
from robot_agents.protocol import (
    PROTOCOL_VERSION,
    GameDescriptor,
    GameNotify,
    GameRequest,
    GameResult,
    Model,
)
from robot_agents.task_store import GameTaskStore


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
        self.pending: dict[tuple[str, str], asyncio.Task] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        if message is None or len(message.parts) != 1 or not message.parts[0].HasField("data"):
            raise ValueError("游戏请求必须包含一个结构化数据 Part")
        request = GameRequest.model_validate(MessageToDict(message.parts[0].data))
        key = (context.context_id, request.request_id)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING), history=[message],
        ))

        async def notify(result):
            await updater.start_work(updater.new_agent_message([
                new_data_part(result.model_dump(mode="json")),
            ]))

        async with self.lock:
            if key in self.cache:
                results = self.cache[key]
                work = None
                replay = True
            elif key in self.pending:
                work = self.pending[key]
                replay = True
            else:
                if request.action == "start":
                    if self.agent is not None:
                        raise RuntimeError("当前游戏尚未退出")
                    self.agent = self.factory(self.settings)
                    self.context_id = context.context_id
                if self.agent is None or self.context_id != context.context_id:
                    raise ValueError("游戏会话已经结束或不匹配")
                if request.action == "end":
                    await self.cancel_pending()
                work = asyncio.create_task(self.run_request(key, request, notify))
                self.pending[key] = work
                replay = False
        if work is not None:
            try:
                results = await asyncio.shield(work)
            except asyncio.CancelledError:
                if not work.cancelled():
                    raise
                await updater.cancel()
                return
        if replay:
            for result in results[:-1]:
                await notify(result)
        await updater.add_artifact([new_data_part(results[-1].model_dump(mode="json"))])
        await updater.complete()

    async def run_request(self, key, request, notify):
        results = []

        async def publish(result):
            results.append(result)
            await notify(result)

        try:
            if self.agent is None or self.context_id != key[0]:
                raise ValueError("游戏会话已经结束或不匹配")
            agent = self.agent
            with observability.game_request(self.factory.descriptor.id, request.request_id):
                result = await agent.handle(request, publish)
            results.append(result)
            if not result.state.active:
                self.agent = None
                self.context_id = None
                await agent.close()
            self.remember(key, results)
            return results
        finally:
            self.pending.pop(key, None)

    def remember(self, key, result):
        self.cache[key] = result
        while len(self.cache) > 256:
            self.cache.popitem(last=False)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ValueError("游戏使用 end 消息结束会话")

    async def cancel_pending(self):
        pending = list(self.pending.values())
        for task in pending:
            task.cancel()
        if pending:
            results = await asyncio.gather(*pending, return_exceptions=True)
            self.pending.clear()
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result

    async def close(self):
        await self.cancel_pending()
        if self.agent is not None:
            await self.agent.close()
            self.agent = None


class EmbeddedServer(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()


class GameEventSourceResponse(EventSourceResponse):
    def __init__(self, response, shutdown):
        super().__init__(response.body_iterator, headers=response.headers)
        self.shutdown = shutdown

    async def _listen_for_exit_signal(self):
        # 每个内嵌服务独立管理 SSE 退出，关闭一个游戏不会影响其他游戏。
        await self.shutdown.wait()


@asynccontextmanager
async def serve_game(factory, settings, *, task_history_limit=64):
    descriptor = factory.descriptor
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    card = AgentCard(
        name=descriptor.name, description=descriptor.description, version="1.0.0",
        capabilities=AgentCapabilities(streaming=True),
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
        agent_executor=executor, task_store=GameTaskStore(max_completed=task_history_limit),
        agent_card=card,
    )
    shutdown = asyncio.Event()
    rpc_route, = create_jsonrpc_routes(handler, rpc_url="/a2a")

    async def rpc(request):
        response = await rpc_route.endpoint(request)
        if isinstance(response, EventSourceResponse):
            return GameEventSourceResponse(response, shutdown)
        return response

    app = Starlette(routes=create_agent_card_routes(card) + [
        Route("/a2a", rpc, methods=["POST"]),
    ])
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
        server.should_exit = True
        try:
            await executor.close()
        finally:
            shutdown.set()
            try:
                await serving
            finally:
                try:
                    await handler.aclose()
                finally:
                    listener.close()


class GameConnection:
    def __init__(self, descriptor, client):
        self.descriptor, self.client = descriptor, client

    async def send(
        self, context_id: str, request: GameRequest, *, on_update: GameNotify | None = None,
    ) -> GameResult:
        message = Message(
            message_id=request.request_id, context_id=context_id, role=Role.ROLE_USER,
            parts=[new_data_part(request.model_dump(mode="json"))],
        )
        result = None
        completed = False
        with observability.a2a_request(self.descriptor, context_id, request) as exchange:
            async for event in self.client.send_message(SendMessageRequest(message=message)):
                if event.HasField("message"):
                    reply = event.message
                    parts = reply.parts
                    completed = True
                elif event.HasField("task"):
                    reply = event.task
                    self.check_status(reply.status.state)
                    if reply.context_id != context_id:
                        raise ValueError("游戏响应会话不匹配")
                    if reply.status.state != TaskState.TASK_STATE_COMPLETED:
                        continue
                    completed = True
                    parts = [part for artifact in reply.artifacts for part in artifact.parts]
                elif event.HasField("status_update"):
                    reply = event.status_update
                    if reply.context_id != context_id:
                        raise ValueError("游戏响应会话不匹配")
                    self.check_status(reply.status.state)
                    if reply.status.state == TaskState.TASK_STATE_COMPLETED:
                        completed = True
                        continue
                    if not reply.status.HasField("message"):
                        continue
                    parts = reply.status.message.parts
                elif event.HasField("artifact_update"):
                    reply = event.artifact_update
                    if reply.append:
                        raise ValueError("游戏结果必须包含完整结构化数据")
                    parts = reply.artifact.parts
                else:
                    raise ValueError("游戏接口返回了未知事件")
                if reply.context_id != context_id or len(parts) != 1:
                    raise ValueError("游戏响应会话或数据数量不匹配")
                if not parts[0].HasField("data"):
                    raise ValueError("游戏响应必须包含唯一结构化结果")
                result = GameResult.model_validate(MessageToDict(parts[0].data))
                if event.HasField("status_update"):
                    exchange.update(result)
                    if on_update is not None:
                        await on_update(result)
            if result is None or not completed:
                raise ValueError("游戏 Agent 没有返回完整结果")
            exchange.result = result
        if on_update is not None:
            await on_update(result)
        return result

    @staticmethod
    def check_status(state):
        if state not in {TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_SUBMITTED,
                         TaskState.TASK_STATE_COMPLETED}:
            raise RuntimeError(f"游戏任务未完成：{TaskState.Name(state)}")


@asynccontextmanager
async def connect_games(path: Path):
    entries = Catalog.validate_json(path.read_text(encoding="utf-8"))
    if len({item.id for item in entries}) != len(entries):
        raise ValueError("游戏注册 ID 不能重复")
    async with AsyncExitStack() as resources:
        games = {}
        for entry in entries:
            limits = httpx.Limits()
            if isinstance(entry, LocalGame):
                module, name = entry.factory.split(":", 1)
                factory = getattr(importlib.import_module(module), name)
                descriptor = GameDescriptor.model_validate(factory.descriptor.model_dump())
                if descriptor.id != entry.id:
                    raise ValueError("注册 ID 与游戏声明不一致")
                settings = factory.load_settings()
                timeout = settings.llm.timeout_seconds * 3 + 30
                url = await resources.enter_async_context(serve_game(factory, settings))
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
