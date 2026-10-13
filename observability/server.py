import asyncio
import json
import os
import socket
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, Response, StreamingResponse
from starlette.routing import Route

from observability.events import bus

PAGE = Path(__file__).with_name("web") / "index.html"
# 项目的 logo 和小懒的头像，页面直接用这里的文件。
ASSETS = Path(__file__).resolve().parent.parent / "assets"


class EmbeddedServer(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()


@asynccontextmanager
async def serve(port, events=bus, routes=()):
    listener = socket.socket()
    closing = asyncio.Event()

    async def stream(seq):
        yield f"retry: 2000\nevent: run\ndata: {json.dumps({'run_id': events.run_id})}\n\n"
        live = None
        while True:
            changed = events.changed
            if live != events.live_version:
                live = events.live_version
                yield f"event: live\ndata: {json.dumps(events.live, ensure_ascii=False)}\n\n"
            for event in events.after(seq):
                seq = event.seq
                yield f"id: {events.run_id}:{seq}\ndata: {event.data}\n\n"
            if closing.is_set():
                return
            try:
                async with asyncio.timeout(15):
                    await changed.wait()
            except TimeoutError:
                yield ": keepalive\n\n"

    async def records(request):
        # 页面重连时带上最后收到的事件 ID，同一次运行内只补发之后的事件。
        cursor = request.headers.get("last-event-id") or request.query_params.get("after", "")
        run_id, _, seq = cursor.partition(":")
        return StreamingResponse(
            stream(int(seq) if run_id == events.run_id and seq.isdecimal() else 0),
            media_type="text/event-stream", headers={"Cache-Control": "no-store"},
        )

    async def page(request):
        return FileResponse(PAGE, headers={"Cache-Control": "no-store"})

    async def asset(request):
        path = ASSETS / request.path_params["name"]
        if path.parent != ASSETS or path.suffix != ".svg" or not path.is_file():
            return Response(status_code=404)
        return FileResponse(path)

    try:
        # Windows 上这个选项允许多个进程绑定同一端口，只在 POSIX 系统上用于重启后立即复用端口。
        if os.name == "posix":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # 其他服务监听全部网卡时，本机地址仍然可以绑定成功并截走发给它的本机连接，
            # 所以先确认这个端口上没有服务在接受连接。
            with socket.socket() as probe:
                probe.settimeout(0.5)
                if port and probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise OSError("端口上已有其他服务")
            listener.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(
                f"可观测性端口 {port} 无法使用，请修改 OBSERVABILITY_PORT：{error}"
            ) from error
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        # 事件包含对话内容，只接受以本机地址发起的访问，其他域名解析到本机时拒绝。
        application = Starlette(
            routes=[Route("/", page), Route("/events", records), Route("/assets/{name}", asset), *routes],
            middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])],
        )
        server = EmbeddedServer(uvicorn.Config(
            application, log_level="error", lifespan="off", timeout_graceful_shutdown=2,
        ))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        await serving
                        raise RuntimeError("可观测性服务没有启动")
                    await asyncio.sleep(0.01)
            yield url
        finally:
            closing.set()
            events.wake()
            server.should_exit = True
            await serving
    finally:
        listener.close()
