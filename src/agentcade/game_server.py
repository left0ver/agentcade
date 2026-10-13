import asyncio
import logging
import socket
from contextlib import asynccontextmanager, nullcontext

import uvicorn
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
    Task,
    TaskState,
    TaskStatus,
)
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.routing import Route

import observability
from agentcade.protocol import PROTOCOL_VERSION, GameInput, GameReply, from_parts, to_part
from agentcade.task_store import GameTaskStore

logger = logging.getLogger(__name__)


class GameExecutor(AgentExecutor):
    def __init__(self, factory, settings, device):
        self.factory, self.settings, self.device = factory, settings, device
        self.agent = None
        self.context_id = None
        self.lock = asyncio.Lock()
        self.turn: asyncio.Task | None = None

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        if message is None:
            raise ValueError("游戏请求缺少消息")
        request = from_parts(GameInput, message.parts)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING), history=[message],
        ))

        async def emit(reply):
            await updater.update_status(
                TaskState.TASK_STATE_WORKING, updater.new_agent_message([to_part(reply)]),
            )

        if request.kind != "message":
            await self.cancel_turn()
        # 同一局的请求按到达顺序逐条处理。
        async with self.lock:
            if request.kind == "start":
                # 上一局没有正常结束时（例如请求中途失败），新的一局直接替换它。
                await self.release()
                self.agent = self.factory(self.settings, self.device)
                self.context_id = context.context_id
            if self.agent is None or self.context_id != context.context_id:
                # 这一局已经不存在，回复本局结束，主 Agent 据此清除自己的会话。
                text = ("好，这局已经结束了。" if request.kind == "end"
                        else "这一局已经结束了，想玩的话再说一次开始吧。")
                await updater.complete(updater.new_agent_message([
                    to_part(GameReply(text=text, session="ended")),
                ]))
                return
            agent = self.agent
            work = self.turn = asyncio.create_task(
                self.run(agent, request, emit, message.message_id),
            )
            try:
                reply = await work
            except asyncio.CancelledError:
                # end 请求取消了这一轮；框架取消整个请求时继续向外传播。
                if asyncio.current_task().cancelling():
                    raise
                await updater.cancel()
                return
            finally:
                self.turn = None
            if reply.session == "ended":
                await self.release()
        await updater.complete(updater.new_agent_message([to_part(reply)]))

    async def run(self, agent, request, emit, request_id):
        with observability.game_request(self.factory.skill.descriptor.id, request_id):
            return await agent.handle(request, emit)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ValueError("游戏使用 end 消息结束会话")

    async def cancel_turn(self):
        work = self.turn
        if work is not None and not work.done():
            work.cancel()
            await asyncio.wait({work})

    async def release(self):
        agent, self.agent, self.context_id = self.agent, None, None
        if agent is not None:
            try:
                await agent.close()
            except Exception:
                logger.exception("关闭游戏 Agent 失败")

    async def close(self):
        await self.cancel_turn()
        await self.release()


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
async def serve_game(factory, settings, device, *, task_history_limit=64):
    descriptor = factory.skill.descriptor
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
    executor = GameExecutor(factory, settings, device)
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
