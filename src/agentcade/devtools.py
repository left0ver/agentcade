"""开发游戏用的命令行工具：

    agentcade-game serve <游戏目录>      单独运行游戏服务，给别人的机器人连
    agentcade-game check <地址或目录>    合规检查：名片和完整的一局
    agentcade-game chat  <地址或目录>    文字模拟器：不用机器人，打字和游戏对话

游戏目录里要有 SKILL.md 和 agent.py（入口类 GameAgent）。配置先读目录里的 .env，
再用同名环境变量覆盖，部署到服务器时把密钥放在环境变量里。
"""
import argparse
import asyncio
import importlib
import logging
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.utils.errors import A2AError
from pydantic import ValidationError

from agentcade import registry
from agentcade.config import SOURCE
from agentcade.game_client import (
    GameConnection,
    check_interface,
    explain,
    protocol_skill,
    remote_descriptor,
)
from agentcade.game_server import MAX_SESSIONS, serve_game
from agentcade.protocol import CAPABILITIES, GameInput

# 开局之后多久还没有说第一句话，就建议先说一句“稍等”。
FIRST_REPLY_SECONDS = 8
# 连接和收发消息时可能遇到的错误，检查报告里按一句话原因显示。
ERRORS = (A2AError, httpx.HTTPError, ValueError, RuntimeError, TimeoutError)


def load_factory(directory: Path):
    """导入游戏目录里的 GameAgent。内置游戏按 games.<id> 导入，其他目录把上一级加入导入路径。"""
    directory = directory.resolve()
    if not (directory / "SKILL.md").is_file() or not (directory / "agent.py").is_file():
        raise SystemExit(f"{directory} 不是游戏目录：需要 SKILL.md 和 agent.py")
    if directory.parent == SOURCE / "games":
        module = f"games.{directory.name}.agent"
    else:
        sys.path.insert(0, str(directory.parent))
        module = f"{directory.name}.agent"
    return importlib.import_module(module).GameAgent


def public_url(value: str | None) -> str | None:
    """名片里对外的地址：参数优先，其次 PUBLIC_URL，在 Hugging Face Space 上用 SPACE_HOST。"""
    if value or os.environ.get("PUBLIC_URL"):
        return (value or os.environ["PUBLIC_URL"]).rstrip("/")
    if os.environ.get("SPACE_HOST"):
        return f"https://{os.environ['SPACE_HOST']}"
    return None


async def open_target(stack: AsyncExitStack, target: str) -> str:
    """目标是目录时在本机起一个服务，返回地址；是地址时检查格式后原样返回。"""
    if Path(target).is_dir():
        try:
            factory = load_factory(Path(target))
        except ValidationError as error:
            raise ValueError(f"SKILL.md 不合规：{explain(error)}") from error
        return await stack.enter_async_context(serve_game(factory, factory.config.from_process()))
    return registry.check_url(target)


async def connect(stack: AsyncExitStack, url: str, timeout: float):
    http = await stack.enter_async_context(httpx.AsyncClient(timeout=timeout, trust_env=False))
    card = await A2ACardResolver(http, url).get_agent_card()
    check_interface(card, url)
    descriptor = remote_descriptor(card, protocol_skill(card))
    client = await create_client(card, client_config=ClientConfig(
        streaming=True, httpx_client=http, supported_protocol_bindings=["JSONRPC"],
    ))
    stack.push_async_callback(client.close)
    return GameConnection(descriptor, client, descriptor.required_capabilities), card


def describe(directive) -> str:
    """设备指令写成简短的中文，模拟器和检查报告里显示。"""
    if directive.type == "gesture":
        return {"nod": "点头", "shake_head": "摇头"}[directive.name]
    if directive.type == "expression":
        return {"happy": "开心", "sorry": "抱歉"}[directive.name]
    if directive.type == "draw":
        return f"画图 {len(directive.paths)} 步"
    if directive.type == "progress":
        return f"显示进度 {directive.seconds} 秒"
    return "恢复待机"


# ---------- serve ----------

async def serve(args) -> None:
    factory = load_factory(Path(args.directory))
    settings = factory.config.from_process()
    async with serve_game(factory, settings, host=args.host, port=args.port,
                          public_url=public_url(args.public_url),
                          max_sessions=args.max_sessions) as url:
        name = factory.skill.descriptor.name
        print(f"{name} 正在运行，监听 {args.host}:{args.port}", flush=True)
        print(f"用户在设置页面里填写的地址：{url}", flush=True)
        await asyncio.Event().wait()


# ---------- check ----------

class Report:
    SYMBOLS: ClassVar[dict[str, str]] = {"ok": "✓", "warn": "⚠", "fail": "✗"}

    def __init__(self):
        self.failed = False

    def section(self, title: str) -> None:
        print(title, flush=True)

    def add(self, level: str, text: str) -> None:
        self.failed |= level == "fail"
        print(f"  {self.SYMBOLS[level]} {text}", flush=True)


async def exchange(connection, context_id, request, report, label, timeout):
    """发一条请求，记录每条回复的时间，检查回复的内容和设备指令。"""
    started = time.monotonic()
    replies = []

    async def collect(reply):
        replies.append((time.monotonic() - started, reply))

    try:
        final = await asyncio.wait_for(
            connection.send(context_id, request, on_reply=collect), timeout,
        )
    except ERRORS as error:
        report.add("fail", f"{label}：请求失败，{explain(error)}")
        return None
    replies.append((time.monotonic() - started, final))
    spoken = next((elapsed for elapsed, reply in replies if reply.text), None)
    report.add("ok", f"{label}：{len(replies)} 条回复，最后一条有话，用时 {replies[-1][0]:.1f} 秒")
    for _, reply in replies:
        actions = "、".join(describe(item) for item in reply.directives)
        print(f"      {'[' + actions + '] ' if actions else ''}{reply.text}", flush=True)
    if spoken is not None and spoken > FIRST_REPLY_SECONDS:
        report.add("warn", f"{label}：{spoken:.1f} 秒后才说第一句话，处理慢时建议先说一句“稍等”")
    declared = set(connection.descriptor.required_capabilities)
    undeclared = sorted({
        CAPABILITIES[item.type] for _, reply in replies for item in reply.directives
        if item.type in CAPABILITIES and CAPABILITIES[item.type] not in declared
    })
    if undeclared:
        report.add("fail", f"{label}：用了没有声明的能力 {'、'.join(undeclared)}，这些指令会被跳过")
    return final


async def check(args) -> int:
    report = Report()
    async with AsyncExitStack() as stack:
        report.section("名片")
        try:
            url = await open_target(stack, args.target)
        except ValueError as error:
            report.add("fail", str(error))
            return 1
        if urlsplit(url).scheme == "https" or registry.local_host(urlsplit(url).hostname or ""):
            report.add("ok", f"地址 {url}")
        try:
            connection, _ = await connect(stack, url, args.timeout)
        except ValidationError as error:
            report.add("fail", f"名片不合规：{explain(error)}")
            return 1
        except ERRORS as error:
            report.add("fail", f"名片有问题：{explain(error)}")
            return 1
        descriptor = connection.descriptor
        report.add("ok", f"能读到 {url}/.well-known/agent-card.json，支持流式响应")
        report.add("ok", f"游戏 {descriptor.id}“{descriptor.name}”，简短介绍 {len(descriptor.summary)} 字："
                         f"{descriptor.summary}")
        report.add("ok", "名片里的消息地址可以访问")
        unknown = [name for name in descriptor.required_capabilities if name not in registry.PERMISSIONS]
        if unknown:
            report.add("warn", f"声明了核心不认识的能力 {'、'.join(unknown)}，安装时会按原名显示")
        if not descriptor.author:
            report.add("warn", "名片里没有作者，用户安装时看不到是谁做的；可以在 SKILL.md 里写 author")

        report.section("一局游戏")
        context_id = uuid4().hex
        start = await exchange(connection, context_id, GameInput(
            kind="start", text=args.start or f"我想玩{descriptor.name}",
        ), report, "开局", args.timeout)
        if start is None:
            return 1
        if start.session != "active":
            report.add("warn", "开局后这一局就结束了")
        else:
            await exchange(connection, context_id, GameInput(kind="message", text=args.say),
                           report, f"说“{args.say}”", args.timeout)
            end = await exchange(connection, context_id, GameInput(kind="end", text="结束游戏", reason="user"),
                                 report, "结束", args.timeout)
            if end is not None:
                report.add("ok" if end.session == "ended" else "fail",
                           "结束后会话状态是 ended" if end.session == "ended"
                           else "结束后会话状态应该是 ended")
        stale = await exchange(connection, uuid4().hex, GameInput(kind="message", text="你好"),
                               report, "发给不存在的一局", args.timeout)
        if stale is not None and stale.session != "ended":
            report.add("fail", "发给不存在的一局时，应该回复这一局已经结束（session 为 ended）")
    print("\n" + ("有不合规的地方，修改后再检查一次。" if report.failed else "检查通过。"), flush=True)
    return 1 if report.failed else 0


# ---------- chat ----------

async def chat(args) -> None:
    async with AsyncExitStack() as stack:
        url = await open_target(stack, args.target)
        connection, _ = await connect(stack, url, args.timeout)
        name = connection.descriptor.name
        print(f"{name}（{url}）\n输入要说的话；/end 结束这一局，/quit 退出。\n", flush=True)

        def show(reply):
            if reply.directives:
                print("  [" + "] [".join(describe(item) for item in reply.directives) + "]", flush=True)
            if reply.text:
                print(f"小懒：{reply.text}", flush=True)

        async def say(context_id, request):
            async def intermediate(reply):
                show(reply)
            final = await connection.send(context_id, request, on_reply=intermediate)
            show(final)
            return final.session == "active"

        context_id = None
        while True:
            if context_id is None:
                context_id = uuid4().hex
                if not await say(context_id, GameInput(kind="start", text=args.start or f"我想玩{name}")):
                    context_id = None
            try:
                line = (await asyncio.to_thread(input, "你> ")).strip()
            except EOFError:
                line = "/quit"
            if line in {"/end", "/quit"}:
                if context_id is not None:
                    await say(context_id, GameInput(kind="end", text="结束游戏", reason="user"))
                    context_id = None
                if line == "/quit":
                    return
                print("（这一局结束了，按回车重新开一局，/quit 退出）", flush=True)
                if (await asyncio.to_thread(input, "")).strip() == "/quit":
                    return
                continue
            if not line:
                continue
            if not await say(context_id, GameInput(kind="message", text=line)):
                context_id = None
                print("（这一局结束了，接着说话会重新开一局）", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentcade-game", description="开发和部署 Agentcade 游戏")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("serve", help="单独运行游戏服务")
    run.add_argument("directory", help="游戏目录，里面有 SKILL.md 和 agent.py")
    run.add_argument("--host", default="0.0.0.0")
    run.add_argument("--port", type=int, default=7860)
    run.add_argument("--public-url", help="用户访问的地址，默认取 PUBLIC_URL，或者 Space 的 SPACE_HOST")
    run.add_argument("--max-sessions", type=int, default=MAX_SESSIONS, help="同时进行的局数上限")

    for name, text in (("check", "合规检查"), ("chat", "文字模拟器")):
        command = commands.add_parser(name, help=text)
        command.add_argument("target", help="游戏地址，或者本地游戏目录")
        command.add_argument("--start", help="开局时用户说的话，默认“我想玩<游戏名>”")
        command.add_argument("--timeout", type=float, default=300, help="每个请求最长等待的秒数")
        if name == "check":
            command.add_argument("--say", default="你好", help="开局后说的一句话")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.command == "serve" else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "serve":
            asyncio.run(serve(args))
        elif args.command == "check":
            sys.exit(asyncio.run(check(args)))
        else:
            asyncio.run(chat(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
