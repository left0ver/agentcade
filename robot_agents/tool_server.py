import asyncio
import json
import socket
from contextlib import asynccontextmanager, nullcontext
from uuid import uuid4

import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.routing import Mount

from robot_agents.tools import ToolRegistry


class ToolServer(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()


@asynccontextmanager
async def serve_tools(registry: ToolRegistry):
    mcp = Server("desktop_agent")

    @mcp.list_tools()
    async def list_tools():
        return [Tool(name=item.name, description=item.description,
                     inputSchema=item.inputs.model_json_schema())
                for item in registry.tools.values()]

    @mcp.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        result = await registry.call(name, arguments)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    manager = StreamableHTTPSessionManager(mcp, json_response=True, stateless=True)
    path = "/" + uuid4().hex
    application = Starlette(routes=[Mount(path, app=manager.handle_request)])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = ToolServer(uvicorn.Config(application, log_level="error", lifespan="off"))
    registry.failure = asyncio.get_running_loop().create_future()
    async with manager.run():
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("MCP 服务没有启动")
                await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}{path}/"
        finally:
            server.should_exit = True
            server.force_exit = True
            await task
            listener.close()
            registry.failure = None
