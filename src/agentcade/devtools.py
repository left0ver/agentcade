"""开发游戏用的命令行工具：

    agentcade-game serve <游戏目录>      单独运行游戏服务，给别人的机器人连
    agentcade-game check <地址或目录>    合规检查：名片、SKILL.md 和工具、完整的一局
    agentcade-game chat  <地址或目录>    文字模拟器：不用机器人，打字和游戏对话
    agentcade-game play  [--game ...]    整条链路的文字模式：经过主 Agent 和所有游戏，不用机器人
    agentcade-game catalog <目录文件>   检查游戏目录 catalog/games.json，CI 和审核时用

游戏目录里要有 SKILL.md 和 agent.py（入口类 GameAgent）。配置先读目录里的 .env，
再用同名环境变量覆盖，部署到服务器时把密钥放在环境变量里。
"""
import argparse
import asyncio
import importlib
import json
import logging
import os
import re
import sys
import time
from contextlib import AsyncExitStack, suppress
from difflib import get_close_matches
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.utils.errors import A2AError
from dotenv import dotenv_values
from pydantic import ValidationError

import observability
from agentcade import catalog, registry
from agentcade.config import SOURCE, LLMSettings, Settings, config_file, resolve_options
from agentcade.game_client import (
    GameConnection,
    GameHub,
    declared_url,
    discover,
    explain,
    message_url,
    pinned,
    protocol_skill,
    remote_descriptor,
)
from agentcade.game_server import MAX_SESSIONS, serve_game
from agentcade.main_agent import MainAgent, greeting
from agentcade.protocol import GameInput
from agentcade.robot_calls import SDK_CALLS, describe_call
from agentcade.robot_calls import problem as call_problem
from agentcade.skill_agent import SkillAgent
from agentcade.tools import collect_tools
from observability.events import encode

# 开局之后多久还没有说第一句话，就建议先说一句“稍等”。
FIRST_REPLY_SECONDS = 8
# 连接和收发消息时可能遇到的错误，检查报告里按一句话原因显示。
ERRORS = (A2AError, httpx.HTTPError, ValueError, RuntimeError, TimeoutError)
# SKILL.md 里写成“调用 工具名”的地方，check 核对这个工具存在。
TOOL_CALL = re.compile(r"调用\s*`?([A-Za-z_][A-Za-z0-9_-]*)")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def load_factory(directory: Path):
    """导入游戏目录里的 GameAgent。内置游戏按 games.<id> 导入，其他目录把上一级加入导入路径。"""
    directory = directory.resolve()
    if not (directory / "SKILL.md").is_file() or not (directory / "agent.py").is_file():
        raise SystemExit(f"{directory} 不是游戏目录：需要 SKILL.md 和 agent.py")
    if directory.parent == SOURCE / "games":
        module = f"games.{directory.name}.agent"
    else:
        if str(directory.parent) not in sys.path:
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
    descriptor = remote_descriptor(card, protocol_skill(card))
    # 和小懒一样，消息只发到填写的地址。
    client = await create_client(pinned(card, url), client_config=ClientConfig(
        streaming=True, httpx_client=http, supported_protocol_bindings=["JSONRPC"],
    ))
    stack.push_async_callback(client.close)
    return GameConnection(descriptor, client, descriptor.required_capabilities), card


def describe(call) -> str:
    """设备指令写成 SDK 的调用形式，参数多时只显示几个，模拟器和检查报告里显示。"""
    return describe_call(call, limit=4)


class Tracer:
    """--trace：游戏 Agent 每一轮的提示词、工具调用和结果，一边发生一边打印。

    系统提示词每个游戏只完整打印一次，之后只打印当前状态。只看得到在这个进程里运行的游戏，
    也就是本地目录和内置游戏；地址上的游戏在它自己的服务器上运行。
    """

    NAMES: ClassVar[dict[str, str]] = {"main": "主 Agent"}

    def __init__(self):
        self.shown: set[str] = set()

    @staticmethod
    def dump(value) -> str:
        return json.dumps(value, ensure_ascii=False, default=encode)

    def __call__(self, record) -> None:
        kind, payload = record["type"], record["payload"]
        actor = self.NAMES.get(record["actor"], record["actor"])
        lines = []
        if kind == "agent.prompt":
            if actor not in self.shown:
                self.shown.add(actor)
                lines += [f"{actor} 的系统提示词：", *(f"  {line}" for line in payload["system"].splitlines())]
            else:
                lines.append(f"{actor} 当前状态 {self.dump(payload['state'])}")
            lines.append(f"{actor} 收到 {self.dump(payload['message'])}")
        elif kind == "tool.call":
            lines.append(f"{actor} 调用 {payload['tool']} {self.dump(payload['arguments'])}")
        elif kind == "tool.result":
            outcome = self.dump(payload["result"]) if payload["ok"] else payload.get("error")
            lines.append(f"{actor} {payload['tool']} → {outcome}（{payload['duration_ms']} 毫秒）")
        for line in lines:
            print(f"  ┆ {line}", flush=True)


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


def check_tools(factory, report: Report) -> None:
    """核对 SKILL.md 正文和 @tool 声明的工具：让大模型调用的工具要存在，每个工具都要提到。"""
    builtin = {spec.name for spec in collect_tools(SkillAgent)}
    names = [spec.name for spec in factory.tool_specs]
    instructions = factory.skill.instructions
    unknown = [name for name in dict.fromkeys(TOOL_CALL.findall(instructions)) if name not in names]
    for name in unknown:
        guess = get_close_matches(name, names, n=1)
        report.add("fail", f"SKILL.md 让大模型调用 {name}，但没有这个工具"
                           + (f"，是不是 {guess[0]}？" if guess else ""))
    mentioned = set(IDENTIFIER.findall(instructions))
    games = [name for name in names if name not in builtin]
    unmentioned = [name for name in games if name not in mentioned]
    if unmentioned:
        report.add("warn", f"工具 {'、'.join(unmentioned)} 在 SKILL.md 里没有提到，"
                           "写清楚什么时候调用它、按结果怎么说，大模型才会用好它")
    if not unknown and not unmentioned:
        report.add("ok", f"SKILL.md 提到了全部 {len(games)} 个工具：{'、'.join(games)}" if games
                   else "游戏没有声明工具")


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
        actions = "] [".join(describe(item) for item in reply.directives)
        print(f"      {'[' + actions + '] ' if actions else ''}{reply.text}", flush=True)
    if spoken is not None and spoken > FIRST_REPLY_SECONDS:
        report.add("warn", f"{label}：{spoken:.1f} 秒后才说第一句话，处理慢时建议先说一句“稍等”")
    declared = set(connection.descriptor.required_capabilities)
    problems = []
    for _, reply in replies:
        for call in reply.directives:
            reason = call_problem(call)
            if reason is None and SDK_CALLS[call.call] not in declared:
                reason = f"robot.{call.call} 需要能力 {SDK_CALLS[call.call]}，SKILL.md 里没有声明"
            if reason:
                problems.append(reason)
    for reason in dict.fromkeys(problems):
        report.add("fail", f"{label}：{reason}，小懒会跳过这条指令")
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
        if registry.unencrypted(url):
            report.add("warn", f"地址 {url} 用的是 http，传输不加密，用户安装时会看到提醒；有域名的话建议配上 https")
        else:
            report.add("ok", f"地址 {url}")
        try:
            connection, card = await connect(stack, url, args.timeout)
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
        declared, target = declared_url(card), message_url(card, url)
        if declared != target:
            report.add("warn", f"名片里的消息地址是 {declared}，和填写的地址对不上，小懒会改发到 {target}；"
                               f"可以把 PUBLIC_URL 设成 {url}，让名片里的地址也对")
        report.add("ok", f"消息发到 {target}")
        unknown = [name for name in descriptor.required_capabilities if name not in registry.PERMISSIONS]
        if unknown:
            report.add("warn", f"声明了核心不认识的能力 {'、'.join(unknown)}，安装时会按原名显示")
        if not descriptor.author:
            report.add("warn", "名片里没有作者，用户安装时看不到是谁做的；可以在 SKILL.md 里写 author")

        # 地址上的游戏看不到代码，只核对本地目录里的游戏。
        if Path(args.target).is_dir():
            report.section("SKILL.md 和工具")
            check_tools(load_factory(Path(args.target)), report)

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
    if not report.failed and not registry.local_host(urlsplit(url).hostname or ""):
        if descriptor.author:
            print("\n想让别人在设置页面的“发现更多游戏”里看到它，把下面这一条加进 catalog/games.json "
                  "的 games，再提一个 PR，见 catalog/README.md：", flush=True)
            print(json.dumps(catalog.entry(url, descriptor), ensure_ascii=False, indent=2), flush=True)
        else:
            print("\n提交到游戏目录之前，先在 SKILL.md 里写上 author。", flush=True)
    return 1 if report.failed else 0


# ---------- catalog ----------

async def verify_listing(listing, report, timeout) -> None:
    """读取游戏名片，核对目录条目和名片一致。"""
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as http:
        try:
            card = await A2ACardResolver(http, listing.url).get_agent_card()
            declared_url(card)
            descriptor = remote_descriptor(card, protocol_skill(card))
        except (*ERRORS, ValidationError) as error:
            report.add("fail", f"{listing.id}：读取名片失败，{explain(error)}")
            return
    expected = {"ID": listing.id, "名称": listing.name, "简短介绍": listing.summary,
                "版本": listing.version, "能力": sorted(listing.capabilities)}
    actual = {"ID": descriptor.id, "名称": descriptor.name, "简短介绍": descriptor.summary,
              "版本": descriptor.version, "能力": sorted(descriptor.required_capabilities)}
    different = [key for key in expected if expected[key] != actual[key]]
    if different:
        report.add("fail", f"{listing.id}：条目和名片不一致，" + "；".join(
            f"{key}条目里是 {expected[key]}，名片里是 {actual[key]}" for key in different))
    else:
        report.add("ok", f"{listing.id}：名片和条目一致（版本 {descriptor.version}）")


async def check_catalog(args) -> int:
    report = Report()
    report.section("目录文件")
    try:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        report.add("fail", f"读不了 {args.file}：{error}")
        return 1
    problems = catalog.validate(data, {game.id for game in discover(SOURCE / "games")})
    for problem in problems:
        report.add("fail", problem)
    if problems:
        return 1
    listed, _ = catalog.parse(data)
    report.add("ok", f"格式正确，收录 {len(listed.games)} 个游戏")
    if not args.online:
        return 0
    # 有改动前的目录时只核对新增和改过的条目，没有时核对全部没下架的条目。
    before = {}
    if args.base:
        with suppress(OSError, ValueError):
            old, _ = catalog.parse(json.loads(Path(args.base).read_text(encoding="utf-8")))
            before = {listing.id: listing for listing in old.games}
    changed = [listing for listing in listed.games
               if listing.status == "listed" and before.get(listing.id) != listing]
    report.section("名片")
    if not changed:
        report.add("ok", "没有新增或改过的游戏")
    for listing in changed:
        await verify_listing(listing, report, args.timeout)
    return 1 if report.failed else 0


# ---------- chat ----------

async def chat(args) -> None:
    if args.trace:
        observability.bus.listeners.append(Tracer())
        if not Path(args.target).is_dir():
            print("（--trace 只看得到本地目录里的游戏，这个游戏在它自己的服务器上运行）", flush=True)
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
                try:
                    if (await asyncio.to_thread(input, "")).strip() == "/quit":
                        return
                except EOFError:
                    # 从文件或管道读入的对话读完了。
                    return
                continue
            if not line:
                continue
            if not await say(context_id, GameInput(kind="message", text=line)):
                context_id = None
                print("（这一局结束了，接着说话会重新开一局）", flush=True)


# ---------- play ----------

def main_settings():
    """文字模式只用到主 Agent 的大模型配置：读主 Agent 的配置文件（有的话），再用同名环境变量覆盖。"""
    options = tuple(option for option in Settings.options() if option.key.startswith("LLM_"))
    path = config_file(Settings.env_file())
    stored = dict(dotenv_values(path, interpolate=False)) if path.is_file() else {}
    stored.update({option.key: os.environ[option.key] for option in options
                   if os.environ.get(option.key)})
    try:
        return SimpleNamespace(llm=LLMSettings.from_values(resolve_options(options, stored)))
    except ValueError as error:
        raise SystemExit(f"主 Agent 的大模型配置不全：{error}。填在 {path}，或者设置同名环境变量") from None


def describe_output(output) -> str:
    """输出模块要执行的一项内容写成文字：设备指令放在方括号里。"""
    if output.call is not None:
        return f"[{describe(output.call)}]"
    if output.reset:
        return "[一局结束，复位游戏用过的设备]"
    return f"小懒：{output.text}"


async def show_outputs(agent) -> None:
    """代替语音合成和机器人：把主 Agent 要播报的话和设备指令打印出来，马上算作完成。"""
    while True:
        request = await agent.outputs.get()
        try:
            for output in request.outputs:
                print(describe_output(output), flush=True)
            await agent.output_done(request)
        finally:
            if not request.completed.done():
                request.completed.cancel()
            agent.outputs.task_done()


async def settle(agent) -> None:
    """等游戏把还没发完的回复都发完、都打印出来。"""
    while agent.busy:
        await asyncio.sleep(0.1)
    await agent.outputs.join()


async def play(args) -> None:
    if args.trace:
        observability.bus.listeners.append(Tracer())
    async with AsyncExitStack() as stack:
        hub = GameHub()
        # 设备指令都显示成文字，机器人的能力全部算作支持。
        hub.supports = lambda capability: True
        stack.push_async_callback(hub.close)
        await hub.start()
        for game_id, reason in hub.errors.items():
            print(f"（{game_id} 没有接入：{reason}）", flush=True)
        # 正在开发的游戏直接接入，和已有的游戏同名时代替它。
        for target in args.game:
            url = await open_target(stack, target)
            connection, _ = await connect(stack, url, args.timeout)
            hub.games[connection.descriptor.id] = connection
        agent = MainAgent(main_settings(), hub.games)
        hub.in_use = lambda game_id: agent.active is not None and agent.active.game_id == game_id
        stack.push_async_callback(agent.close)
        printer = asyncio.create_task(show_outputs(agent))
        stack.callback(printer.cancel)

        async def tick():
            while True:
                await agent.tick()
                await asyncio.sleep(0.1)

        ticker = asyncio.create_task(tick())
        stack.callback(ticker.cancel)
        piped = not sys.stdin.isatty()
        print("输入要说的话；/wait 等游戏把话说完，/quit 退出。\n", flush=True)
        await agent.reply(greeting(hub.games))
        await settle(agent)
        while True:
            try:
                line = (await asyncio.to_thread(input, "" if piped else "你> ")).strip()
            except EOFError:
                # 脚本读完了，等游戏把最后的话说完再退出。
                await settle(agent)
                return
            if piped and line:
                print(f"你> {line}", flush=True)
            if line == "/quit":
                return
            if line == "/wait":
                await settle(agent)
                continue
            if not line:
                continue
            turn_id = observability.user_input(line, time.monotonic())
            with observability.turn(turn_id):
                completion = await agent.receive(line)
            # 和机器人一样，第一句话说完才接着听下一句。
            if completion is not None:
                await asyncio.shield(completion)


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
        else:
            command.add_argument("--trace", action="store_true",
                                 help="打印游戏的提示词、工具调用和结果，只对本地目录里的游戏有效")

    whole = commands.add_parser("play", help="整条链路的文字模式：经过主 Agent 和所有游戏")
    whole.add_argument("--game", action="append", default=[],
                       help="另外接入的游戏地址或者本地目录，可以写多次；和已有游戏同名时代替它")
    whole.add_argument("--timeout", type=float, default=300, help="每个请求最长等待的秒数")
    whole.add_argument("--trace", action="store_true",
                       help="打印主 Agent 的工具调用，以及内置游戏和本地目录里游戏的提示词、工具调用和结果")

    listed = commands.add_parser("catalog", help="检查游戏目录文件")
    listed.add_argument("file", help="目录文件，一般是 catalog/games.json")
    listed.add_argument("--online", action="store_true", help="读取游戏名片，核对条目和名片一致")
    listed.add_argument("--base", help="改动前的目录文件，只核对新增和改过的条目")
    listed.add_argument("--timeout", type=float, default=60, help="读取每个名片最长等待的秒数")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.command == "serve" else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "serve":
            asyncio.run(serve(args))
        elif args.command == "check":
            sys.exit(asyncio.run(check(args)))
        elif args.command == "catalog":
            sys.exit(asyncio.run(check_catalog(args)))
        elif args.command == "play":
            asyncio.run(play(args))
        else:
            asyncio.run(chat(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
