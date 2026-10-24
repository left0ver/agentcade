import asyncio
import importlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.client.errors import AgentCardResolutionError
from a2a.types import AgentCard, AgentInterface, Message, Role, SendMessageRequest, TaskState
from pydantic import ValidationError

import observability
from agentcade import registry
from agentcade.catalog import CatalogStore
from agentcade.config import SOURCE
from agentcade.game_server import serve_game
from agentcade.protocol import (
    SUPPORTED_PROTOCOLS,
    GameDescriptor,
    GameInput,
    GameReply,
    Model,
    from_parts,
    to_part,
)

logger = logging.getLogger(__name__)

# 名片标签里像协议版本的写法，用来认出小懒不支持的版本。
PROTOCOL_TAG = re.compile(r"^[a-z][a-z0-9-]*/\d+$")


class LocalGame(Model):
    kind: Literal["local"] = "local"
    id: str
    factory: str


class GameConnection:
    def __init__(self, descriptor, client, granted, protocol):
        self.descriptor, self.client = descriptor, client
        # 用户同意这个游戏使用的机器人能力；内置游戏是它声明的全部能力。
        self.granted = set(granted)
        # 和这个游戏通信用的协议版本，消息里都写这个版本。
        self.protocol = protocol

    async def send(
        self, context_id: str, request: GameInput, *,
        on_reply: Callable[[GameReply], Awaitable[None]] | None = None,
        request_id: str | None = None,
    ) -> GameReply:
        """发送一条输入。中间回复逐条交给 on_reply，返回这次请求的最后一条回复。"""
        request = request.model_copy(update={"protocol": self.protocol})
        message = Message(
            message_id=request_id or uuid4().hex, context_id=context_id, role=Role.ROLE_USER,
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
                if reply.protocol != self.protocol:
                    raise ValueError(f"游戏回复用的协议是 {reply.protocol}，这个游戏说好的是 {self.protocol}")
                if state == TaskState.TASK_STATE_COMPLETED:
                    if not reply.text:
                        raise ValueError("最后一条回复必须有要播报的话")
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


def explain(error: BaseException) -> str:
    """给设置页面看的一句话原因。"""
    if isinstance(error, AgentCardResolutionError):
        if error.status_code:
            return f"读取游戏名片失败（HTTP {error.status_code}）"
        cause = error.__cause__
        if isinstance(cause, httpx.TimeoutException):
            return "连接超时"
        if isinstance(cause, httpx.RequestError):
            return "连不上这个地址"
        return "游戏名片的格式不对"
    if isinstance(error, httpx.TimeoutException):
        return "连接超时"
    if isinstance(error, httpx.RequestError):
        return "连不上这个地址"
    if isinstance(error, ValidationError):
        item = error.errors()[0]
        return f"{'.'.join(map(str, item['loc']))}：{item['msg']}"
    if isinstance(error, FileNotFoundError):
        return "还没有填写配置"
    if isinstance(error, KeyError):
        return f"缺少 {error.args[0]}"
    return (str(error) or type(error).__name__)[:200]


def protocol_of(skill) -> str | None:
    """游戏和小懒都支持的最新协议版本，没有时返回 None。"""
    return next((version for version in SUPPORTED_PROTOCOLS if version in skill.tags), None)


def protocol_skill(card, game_id: str | None = None):
    """名片里按公共协议提供的那个游戏。"""
    if not card.capabilities.streaming:
        raise ValueError("游戏服务必须支持流式响应")
    skills = [skill for skill in card.skills if protocol_of(skill)]
    if game_id is not None:
        skills = [skill for skill in skills if skill.id == game_id]
    if len(skills) != 1:
        supported = "、".join(SUPPORTED_PROTOCOLS)
        others = sorted({tag for skill in card.skills for tag in skill.tags if PROTOCOL_TAG.match(tag)})
        if not skills and others:
            raise ValueError(f"游戏用的协议是 {'、'.join(others)}，小懒支持的是 {supported}，游戏要升级")
        raise ValueError(f"名片里要有且只有一个带 {supported} 标签的游戏")
    return skills[0]


def remote_descriptor(card, skill) -> GameDescriptor:
    return GameDescriptor(
        id=skill.id, name=card.name, description=card.description,
        summary=skill.description, aliases=list(skill.examples),
        required_capabilities=[tag.removeprefix("capability:")
                               for tag in skill.tags if tag.startswith("capability:")],
        version=card.version or "1.0.0", author=card.provider.organization,
        homepage=card.provider.url or card.documentation_url,
    )


def declared_url(card) -> str:
    """名片里声明的 JSONRPC 消息地址。"""
    for interface in card.supported_interfaces:
        if interface.protocol_binding == "JSONRPC":
            return interface.url
    raise ValueError("名片里没有 JSONRPC 消息地址")


def message_url(card, url: str) -> str:
    """实际发送消息的地址：名片里的消息地址换到用户填写的地址下，路径沿用名片里的。

    名片里的地址由开发者配置，可能配错（比如 PUBLIC_URL 写成 http 或者内网地址），也可能指向
    别的服务器；用户只同意把说的话发给自己填写的地址，所以消息一律发到那里。
    """
    entered = urlsplit(url)
    path = urlsplit(declared_url(card)).path or "/"
    # 游戏部署在子路径下时，名片里的路径可能少了这一段，例如没有设置 PUBLIC_URL。
    if entered.path and path != entered.path and not path.startswith(entered.path + "/"):
        path = entered.path + path
    return urlunsplit((entered.scheme, entered.netloc, path, "", ""))


def pinned(card, url: str):
    """复制一份名片，只保留发到用户填写的地址的那个消息地址，用来创建 A2A 客户端。"""
    interface = AgentInterface()
    interface.CopyFrom(next(item for item in card.supported_interfaces
                            if item.protocol_binding == "JSONRPC"))
    interface.url = message_url(card, url)
    copy = AgentCard()
    copy.CopyFrom(card)
    del copy.supported_interfaces[:]
    copy.supported_interfaces.append(interface)
    return copy


class GameHub:
    """管理全部游戏的连接。

    启动时逐个连接，某个游戏失败只跳过它并记下原因；运行中可以安装、停用、启用和卸载，
    主 Agent 用的 games 就是这里的同一个字典，改动在下一句话生效。
    """

    def __init__(self, directory: Path = SOURCE / "games", path: Path = registry.PATH,
                 catalog: CatalogStore | None = None):
        self.directory, self.path = directory, path
        self.catalog = catalog or CatalogStore()
        self.local = {game.id: game for game in discover(directory)}
        self.games: dict[str, GameConnection] = {}
        self.errors: dict[str, str] = {}
        self.stacks: dict[str, AsyncExitStack] = {}
        self.started = False
        self.lock = asyncio.Lock()
        # 由应用设置：机器人是否支持某项能力、某个游戏是不是正在玩。
        self.supports: Callable[[str], bool] = lambda capability: True
        self.in_use: Callable[[str], bool] = lambda game_id: False

    def factory(self, game_id: str):
        module, name = self.local[game_id].factory.split(":", 1)
        return getattr(importlib.import_module(module), name)

    async def start(self) -> None:
        async with self.lock:
            self.started = True
            entries = registry.load(self.path)
            for game_id in self.local:
                if entries.enabled(game_id):
                    await self.connect_local(game_id)
            for entry in entries.remote():
                if entry.enabled:
                    await self.connect_remote(entry)
            self.reorder(entries)

    async def close(self) -> None:
        async with self.lock:
            for game_id in list(self.stacks):
                await self.disconnect(game_id)
            self.started = False

    async def open(self, game_id: str, opener) -> None:
        stack = AsyncExitStack()
        try:
            connection = await opener(stack)
            missing = [name for name in connection.descriptor.required_capabilities
                       if not self.supports(name)]
            if missing:
                raise ValueError("机器人不支持：" + "、".join(missing))
        except Exception as error:
            await stack.aclose()
            self.errors[game_id] = explain(error)
            logger.warning("游戏 %s 没有接入：%s", game_id, self.errors[game_id], exc_info=True)
            return
        self.errors.pop(game_id, None)
        self.stacks[game_id] = stack
        self.games[game_id] = connection

    async def client(self, stack, card, http):
        client = await create_client(card, client_config=ClientConfig(
            streaming=True, httpx_client=http, supported_protocol_bindings=["JSONRPC"],
        ))
        stack.push_async_callback(client.close)
        return client

    async def connect_local(self, game_id: str) -> None:
        async def opener(stack):
            factory = self.factory(game_id)
            descriptor = factory.skill.descriptor
            if descriptor.id != game_id:
                raise ValueError(f"游戏目录名和 SKILL.md 声明的 ID 不一致：{game_id}")
            settings = factory.config.from_environment()
            url = await stack.enter_async_context(serve_game(factory, settings))
            # 本地请求完成后关闭连接，避免复用正被服务端空闲超时关闭的连接。
            http = await stack.enter_async_context(httpx.AsyncClient(
                timeout=settings.llm.timeout_seconds * 3 + 30, trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=0),
            ))
            card = await A2ACardResolver(http, url).get_agent_card()
            skill = protocol_skill(card, game_id)
            client = await self.client(stack, card, http)
            return GameConnection(descriptor, client, descriptor.required_capabilities, protocol_of(skill))

        await self.open(game_id, opener)

    async def connect_remote(self, entry: registry.Entry) -> None:
        async def opener(stack):
            http = await stack.enter_async_context(httpx.AsyncClient(
                timeout=entry.timeout_seconds, trust_env=False,
            ))
            card = await A2ACardResolver(http, entry.url).get_agent_card()
            skill = protocol_skill(card, entry.id)
            descriptor = remote_descriptor(card, skill)
            client = await self.client(stack, pinned(card, entry.url), http)
            return GameConnection(descriptor, client, entry.granted, protocol_of(skill))

        await self.open(entry.id, opener)

    async def disconnect(self, game_id: str) -> None:
        self.games.pop(game_id, None)
        self.errors.pop(game_id, None)
        stack = self.stacks.pop(game_id, None)
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                logger.exception("断开游戏 %s 失败", game_id)

    def reorder(self, entries: registry.Registry) -> None:
        """内置游戏在前、按目录名排序，远程游戏按安装顺序排在后面。主 Agent 持有同一个字典，原地调整。"""
        order = [*self.local, *(entry.id for entry in entries.remote())]
        items = sorted(self.games.items(), key=lambda item: order.index(item[0]))
        self.games.clear()
        self.games.update(items)

    def require_idle(self, game_id: str, action: str) -> None:
        if self.in_use(game_id):
            raise ValueError(f"正在玩这个游戏，先说“结束游戏”再{action}")

    async def preview(self, url: str) -> dict:
        """读取一个远程游戏的名片，检查能不能安装，返回给用户确认的内容。"""
        url = registry.check_url(url)
        async with httpx.AsyncClient(timeout=15, trust_env=False) as http:
            try:
                card = await A2ACardResolver(http, url).get_agent_card()
            except Exception as error:
                raise ValueError(f"读取不到这个地址的游戏：{explain(error)}") from error
        try:
            declared_url(card)
            descriptor = remote_descriptor(card, protocol_skill(card))
        except ValidationError as error:
            raise ValueError(f"游戏名片不合规：{explain(error)}") from error
        if descriptor.id in self.local or registry.load(self.path).get(descriptor.id):
            raise ValueError(f"已经有 ID 为 {descriptor.id} 的游戏了")
        await self.catalog.refresh()
        review = self.catalog.current().review(url, descriptor.version, descriptor.required_capabilities)
        return {
            "url": url, "id": descriptor.id, "name": descriptor.name, "summary": descriptor.summary,
            "description": descriptor.description, "version": descriptor.version,
            "author": descriptor.author, "homepage": descriptor.homepage,
            "host": urlsplit(url).hostname, "unencrypted": registry.unencrypted(url), "capabilities": [
                {"name": name, "label": registry.PERMISSIONS.get(name, name),
                 "supported": self.supports(name)}
                for name in descriptor.required_capabilities
            ], "review": review,
        }

    async def install(self, url: str, granted: list[str]) -> str:
        async with self.lock:
            info = await self.preview(url)
            if info["review"]["state"] == "removed":
                raise ValueError(f"这个游戏已经从目录下架：{info['review']['note']}")
            declared = {item["name"] for item in info["capabilities"]}
            entry = registry.Entry(
                id=info["id"], source="remote", url=info["url"], name=info["name"],
                summary=info["summary"], granted=sorted(declared & set(granted)),
                installed_at=time.time(),
            )
            entries = registry.load(self.path)
            entries.games.append(entry)
            registry.save(entries, self.path)
            if self.started:
                await self.connect_remote(entry)
                self.reorder(entries)
            return entry.id

    async def set_enabled(self, game_id: str, enabled: bool) -> None:
        async with self.lock:
            entries = registry.load(self.path)
            entry = entries.get(game_id)
            if entry is None:
                if game_id not in self.local:
                    raise KeyError(game_id)
                entry = registry.Entry(id=game_id, source="builtin")
                entries.games.append(entry)
            if not enabled:
                self.require_idle(game_id, "停用")
            entry.enabled = enabled
            # 内置游戏默认启用，重新启用时不用留记录。
            if entry.source == "builtin" and enabled:
                entries.games.remove(entry)
            registry.save(entries, self.path)
            if not self.started:
                return
            if not enabled:
                await self.disconnect(game_id)
            elif game_id not in self.games:
                if entry.source == "builtin":
                    await self.connect_local(game_id)
                else:
                    await self.connect_remote(entry)
                self.reorder(entries)

    async def grant(self, game_id: str, capabilities: list[str]) -> None:
        """用户确认远程游戏新要的能力。"""
        async with self.lock:
            entries = registry.load(self.path)
            entry = entries.get(game_id)
            if entry is None or entry.source != "remote":
                raise KeyError(game_id)
            entry.granted = sorted(set(entry.granted) | set(capabilities))
            registry.save(entries, self.path)
            connection = self.games.get(game_id)
            if connection is not None:
                connection.granted = set(entry.granted)

    async def uninstall(self, game_id: str) -> None:
        async with self.lock:
            entries = registry.load(self.path)
            entry = entries.get(game_id)
            if entry is None or entry.source != "remote":
                raise KeyError(game_id)
            self.require_idle(game_id, "卸载")
            entries.games.remove(entry)
            registry.save(entries, self.path)
            await self.disconnect(game_id)

    def statuses(self) -> list[dict]:
        """设置页面上的游戏列表。"""
        entries = registry.load(self.path)
        result = []
        for game_id in self.local:
            descriptor = self.factory(game_id).skill.descriptor
            result.append(self.status(game_id, entries.enabled(game_id), {
                "source": "builtin", "name": descriptor.name, "summary": descriptor.summary,
                "capabilities": descriptor.required_capabilities,
                "granted": descriptor.required_capabilities, "review": None,
            }))
        catalog = self.catalog.current()
        for entry in entries.remote():
            connection = self.games.get(entry.id)
            declared = connection.descriptor.required_capabilities if connection else entry.granted
            # 没连上时不知道名片的版本和能力，只看目录里有没有、下没下架。
            version = connection.descriptor.version if connection else None
            result.append(self.status(entry.id, entry.enabled, {
                "source": "remote", "url": entry.url, "host": urlsplit(entry.url).hostname,
                "name": connection.descriptor.name if connection else entry.name or entry.id,
                "summary": connection.descriptor.summary if connection else entry.summary or "",
                "capabilities": declared, "granted": entry.granted, "version": version,
                "review": catalog.review(entry.url, version, declared if connection else None),
                "unencrypted": registry.unencrypted(entry.url),
            }))
        return result

    def listings(self) -> dict:
        """设置页面“发现更多游戏”的内容：目录里没下架的游戏，标出已经装了的。"""
        entries = registry.load(self.path)
        installed = {entry.url for entry in entries.remote()}
        taken = {*self.local, *(entry.id for entry in entries.games)}
        games = []
        for listing in self.catalog.current().games:
            if listing.status != "listed":
                continue
            games.append({
                **listing.model_dump(mode="json", exclude={"status", "note"}),
                "host": urlsplit(listing.url).hostname, "unencrypted": registry.unencrypted(listing.url),
                "installed": listing.url in installed,
                # 装了另一个同 ID 的游戏（比如粘贴地址装的），这个就装不了。
                "taken": listing.url not in installed and listing.id in taken,
                "permissions": [registry.PERMISSIONS.get(name, name) for name in listing.capabilities],
            })
        store = self.catalog
        return {"games": games, "source": store.source, "fetched_at": store.fetched_at,
                "error": store.error}

    def status(self, game_id: str, enabled: bool, details: dict) -> dict:
        if not enabled:
            state = "disabled"
        elif game_id in self.games:
            state = "connected"
        elif game_id in self.errors:
            state = "error"
        else:
            state = "waiting"
        pending = [name for name in details["capabilities"] if name not in details["granted"]]
        return {
            "id": game_id, "state": state, "error": self.errors.get(game_id),
            "playing": self.in_use(game_id), **details,
            "pending": [{"name": name, "label": registry.PERMISSIONS.get(name, name)}
                        for name in pending],
            # 游戏升级后不再要的能力，用户以前同意过也不列出来。
            "permissions": [registry.PERMISSIONS.get(name, name) for name in details["granted"]
                            if name in details["capabilities"]],
        }
