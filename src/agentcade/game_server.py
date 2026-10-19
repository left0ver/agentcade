import asyncio
import logging
import socket
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field

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
    AgentProvider,
    AgentSkill,
    Task,
    TaskState,
    TaskStatus,
)
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

import observability
from agentcade.protocol import PROTOCOL_VERSION, GameInput, GameReply, from_parts, to_part
from agentcade.task_store import GameTaskStore

logger = logging.getLogger(__name__)


# 一局超过这么久没有新的输入就关闭，例如玩家的应用中途退出、没有发来结束。
SESSION_IDLE_SECONDS = 30 * 60
# 同时进行的局数上限，单独部署给很多人玩时保护服务器。
MAX_SESSIONS = 32


@dataclass(eq=False)
class Session:
    """一局游戏：一个游戏 Agent，按到达顺序逐条处理这一局的输入。"""

    agent: object
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn: asyncio.Task | None = None
    used: float = field(default_factory=time.monotonic)


class GameExecutor(AgentExecutor):
    """每个 context_id 是一局，各局互不影响，所以同一个游戏服务可以同时给很多人玩。"""

    def __init__(self, factory, settings, *, max_sessions=MAX_SESSIONS,
                 idle_seconds=SESSION_IDLE_SECONDS):
        self.factory, self.settings = factory, settings
        self.max_sessions, self.idle_seconds = max_sessions, idle_seconds
        self.sessions: dict[str, Session] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        if message is None:
            raise ValueError("游戏请求缺少消息")
        request = from_parts(GameInput, message.parts)
        context_id = context.context_id
        updater = TaskUpdater(event_queue, context.task_id, context_id)
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING), history=[message],
        ))

        async def emit(reply):
            await updater.update_status(
                TaskState.TASK_STATE_WORKING, updater.new_agent_message([to_part(reply)]),
            )

        async def finish(text):
            # 这一局没有开始或者已经关闭，回复本局结束，主 Agent 据此清除自己的会话。
            await updater.complete(updater.new_agent_message([
                to_part(GameReply(text=text, session="ended")),
            ]))

        await self.sweep()
        if request.kind == "start":
            # 同一局重新开局时替换原来的那一局。
            await self.release(context_id)
            if len(self.sessions) >= self.max_sessions:
                await finish("现在玩的人太多了，等一会儿再来吧。")
                return
            self.sessions[context_id] = Session(self.factory(self.settings))
        session = self.sessions.get(context_id)
        if session is None:
            await finish("好，这局已经结束了。" if request.kind == "end"
                         else "这一局已经结束了，想玩的话再说一次开始吧。")
            return
        if request.kind == "end":
            await self.cancel_turn(session)
        async with session.lock:
            if self.sessions.get(context_id) is not session:
                await finish("这一局已经结束了，想玩的话再说一次开始吧。")
                return
            session.used = time.monotonic()
            work = session.turn = asyncio.create_task(
                self.run(session.agent, request, emit, message.message_id),
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
                session.turn = None
                session.used = time.monotonic()
            if reply.session == "ended":
                await self.release(context_id)
        await updater.complete(updater.new_agent_message([to_part(reply)]))

    async def run(self, agent, request, emit, request_id):
        with observability.game_request(self.factory.skill.descriptor.id, request_id):
            return await agent.handle(request, emit)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ValueError("游戏使用 end 消息结束会话")

    async def cancel_turn(self, session):
        work = session.turn
        if work is not None and not work.done():
            work.cancel()
            await asyncio.wait({work})

    async def release(self, context_id):
        session = self.sessions.pop(context_id, None)
        if session is None:
            return
        await self.cancel_turn(session)
        try:
            await session.agent.close()
        except Exception:
            logger.exception("关闭游戏 Agent 失败")

    async def sweep(self):
        """关闭长时间没有输入、也没有在处理的那几局。"""
        now = time.monotonic()
        for context_id, session in list(self.sessions.items()):
            if now - session.used > self.idle_seconds and not session.lock.locked():
                logger.info("一局游戏 %s 超过 %s 秒没有输入，已关闭", context_id, self.idle_seconds)
                await self.release(context_id)

    async def close(self):
        for context_id in list(self.sessions):
            await self.release(context_id)


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
async def serve_game(factory, settings, *, host="127.0.0.1", port=0, public_url=None,
                     max_sessions=MAX_SESSIONS, task_history_limit=64):
    """运行一个游戏的 A2A 服务，返回它的地址。

    嵌在应用里时监听本机随机端口；单独部署时指定 host、port，并用 public_url 告诉别人
    从哪里访问，名片里的消息地址就用它，否则用户从别的机器连不上。
    """
    descriptor = factory.skill.descriptor
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    local = "127.0.0.1" if host in {"0.0.0.0", ""} else host
    url = (public_url or f"http://{local}:{listener.getsockname()[1]}").rstrip("/")
    provider = (AgentProvider(organization=descriptor.author, url=descriptor.homepage)
                if descriptor.author or descriptor.homepage else None)
    card = AgentCard(
        name=descriptor.name, description=descriptor.description, version=descriptor.version,
        provider=provider,
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["application/json"], default_output_modes=["application/json"],
        skills=[AgentSkill(
            id=descriptor.id, name=descriptor.name, description=descriptor.summary,
            tags=[PROTOCOL_VERSION, *[
                f"capability:{name}" for name in descriptor.required_capabilities
            ]], examples=descriptor.aliases,
        )],
        supported_interfaces=[AgentInterface(
            protocol_binding="JSONRPC", protocol_version="1.0", url=url + "/a2a",
        )],
    )
    executor = GameExecutor(factory, settings, max_sessions=max_sessions)
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

    async def home(request):
        # 浏览器打开根地址时确认服务在运行；游戏的说明在名片里。
        return PlainTextResponse(f"{descriptor.name} · 游戏服务运行中\n"
                                 f"名片：{url}/.well-known/agent-card.json\n")

    app = Starlette(routes=create_agent_card_routes(card) + [
        Route("/", home), Route("/a2a", rpc, methods=["POST"]),
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
