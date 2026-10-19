"""本机设置页面的后端：让用户在浏览器里填写各个 Agent 需要的服务配置。

表单内容来自每个 Agent 目录里的 settings.yaml：主 Agent 一栏，games/ 下
每个游戏一栏，各存各的文件，互相不共用。保存时只改动用户改过的那几项，文件里其余内容原样保留。新增游戏只要声明自己的配置项，页面就会多出一栏。

页面上还有一栏“游戏”：查看各个游戏的状态，安装远程游戏，停用、启用和卸载，由 GameHub 完成。
"""
import asyncio
import importlib
import io
import os
import stat
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from dotenv.parser import parse_stream
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from agentcade import registry
from agentcade.config import SOURCE, AgentSettings, Settings, config_file
from agentcade.game_client import GameHub, discover
from observability.server import PAGE

MAIN = "agentcade"
SETTINGS_PAGE = PAGE.with_name("settings.html")
MAX_VALUE_LENGTH = 4096
status = {"waiting": False}


@dataclass(frozen=True)
class Section:
    """设置页面上的一栏，对应一个 Agent 的配置。"""

    id: str
    title: str
    config: type[AgentSettings]

    @property
    def local(self) -> Path:
        return self.config.env_file()

    def stored(self) -> dict[str, str]:
        path = config_file(self.local)
        if not path.is_file():
            return {}
        return {key: value or "" for key, value in dotenv_values(path, interpolate=False).items()}


def sections() -> list[Section]:
    found = [Section(MAIN, "主 Agent（小懒）", Settings)]
    for game in discover(SOURCE / "games"):
        module, name = game.factory.split(":", 1)
        factory = getattr(importlib.import_module(module), name)
        found.append(Section(f"games/{game.id}", factory.skill.descriptor.name, factory.config))
    return found


def explain(error: Exception) -> str:
    if isinstance(error, FileNotFoundError):
        return "还没有填写"
    if isinstance(error, KeyError):
        return f"缺少 {error.args[0]}"
    return str(error) or type(error).__name__


def disabled() -> set[str]:
    """停用的内置游戏对应的那几栏，它们的配置不影响启动。"""
    entries = registry.load()
    return {f"games/{entry.id}" for entry in entries.games
            if entry.source == "builtin" and not entry.enabled}


def problems(listed: list[Section] | None = None) -> dict[str, str]:
    """按应用启动时的方式加载每一份配置，返回加载不了的那几栏和原因；停用的游戏不检查。"""
    issues = {}
    skipped = disabled()
    for section in listed or sections():
        if section.id in skipped:
            continue
        try:
            section.config.from_environment()
        except (OSError, KeyError, ValueError, TypeError) as error:
            # 配置缺失或者取值不对都算没填好，原因显示在设置页面对应的那一栏。
            issues[section.id] = explain(error)
    return issues


def port() -> int:
    """页面要在配置填好之前就能打开，端口读不到时用默认值。"""
    with suppress(Exception):
        return int(sections()[0].stored().get("OBSERVABILITY_PORT") or 8790)
    return 8790


def describe() -> dict:
    listed = sections()
    issues = problems(listed)
    skipped = disabled()
    result = []
    for section in listed:
        stored = section.stored()
        result.append({
            "id": section.id, "title": section.title, "problem": issues.get(section.id),
            "disabled": section.id in skipped,
            # 这一栏实际读写的文件：源码目录里有 .env 时是它，否则在用户配置目录里。
            "file": str(config_file(section.local)),
            "fields": [{
                "key": option.key, "label": option.label, "secret": option.secret,
                "advanced": option.advanced, "saved": bool(stored.get(option.key)),
                "value": "" if option.secret else stored.get(option.key) or option.default,
            } for option in section.config.options()],
        })
    return {"waiting": status["waiting"], "sections": result}


def assignment(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"{key}='{escaped}'\n"


def rewrite(text: str, changes: dict[str, str]) -> str:
    """只改要改的那几项，其余内容（注释、空行、没声明的项）原样保留。

    文件里已有的项原地替换，没有的追加到末尾；值为空表示删掉这一项。
    """
    bindings = list(parse_stream(io.StringIO(text)))
    # 同一项写了多次时最后一次生效，改的也是最后一次。
    last = {binding.key: index for index, binding in enumerate(bindings) if binding.key in changes}
    parts = []
    for index, binding in enumerate(bindings):
        original = binding.original.string
        if last.get(binding.key) != index:
            parts.append(original)
            continue
        # 解析器把这一项前面的空行算在它的原文里，替换时留着。
        parts.append(original[:len(original) - len(original.lstrip("\n"))])
        if changes[binding.key]:
            parts.append(assignment(binding.key, changes[binding.key]))
    result = "".join(parts)
    added = [key for key, value in changes.items() if key not in last and value]
    if added and result and not result.endswith("\n"):
        result += "\n"
    return result + "".join(assignment(key, changes[key]) for key in added)


def write_changes(path: Path, changes: dict[str, str]) -> None:
    before = path.read_text(encoding="utf-8") if path.is_file() else ""
    after = rewrite(before, changes)
    expected = dict(dotenv_values(stream=io.StringIO(before), interpolate=False))
    for key, value in changes.items():
        if value:
            expected[key] = value
        else:
            expected.pop(key, None)
    # 写之前按读取时的方式解析一遍，确认每一项读出来和要保存的一致，别的项没有被碰到。
    if dict(dotenv_values(stream=io.StringIO(after), interpolate=False)) != expected:
        raise ValueError("有一项的内容无法保存，请去掉其中的特殊字符")
    # 文件里有密钥：新文件只允许当前用户读写，已有的文件保持原来的权限。
    mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() else 0o600
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w", encoding="utf-8") as file:
        file.write(after)
    os.chmod(temporary, mode)
    # 写完再替换，避免留下写了一半的文件。
    temporary.replace(path)


def save(submitted: dict) -> None:
    for section in sections():
        entry = submitted.get(section.id)
        if not isinstance(entry, dict):
            continue
        stored, changes = section.stored(), {}
        for option in section.config.options():
            value = entry.get(option.key, "")
            if not isinstance(value, str) or len(value) > MAX_VALUE_LENGTH or "\n" in value or "\r" in value:
                raise ValueError(f"{option.key} 的内容不合适：只能是一行文字")
            value = value.strip()
            # 密钥不回显，留空表示沿用已经保存的那一个。
            if option.secret and not value:
                continue
            # 清空的项回到默认值；和现在生效的值一样就不动文件。
            value = value or option.default
            if value != (stored.get(option.key) or option.default):
                changes[option.key] = value
        if changes:
            write_changes(config_file(section.local), changes)


async def ready(app, url):
    """配置齐了就返回主 Agent 的配置；没齐时提示用户去设置页面填写，填好后自动继续。"""
    announced = False
    while not app.shutdown_requested:
        issues = problems()
        if not issues:
            status["waiting"] = False
            return Settings.from_environment()
        if not announced:
            announced = status["waiting"] = True
            titles = {section.id: section.title for section in sections()}
            app.logger.warning(
                "%s的服务配置还没有填好。请在浏览器里打开 %s/settings 填写并保存，应用会自动继续启动。",
                "、".join(titles.get(identifier, identifier) for identifier in issues), url,
            )
            with suppress(Exception):
                webbrowser.open(f"{url}/settings")
        await asyncio.sleep(1)
    return None


def trusted(request) -> bool:
    """只接受这个页面自己发来的 JSON 请求，别的网站不能借用户的浏览器改这里的配置或装游戏。"""
    origin = request.headers.get("origin")
    return (request.headers.get("content-type", "").split(";")[0].strip() == "application/json"
            and not (origin and urlsplit(origin).netloc != request.headers.get("host")))


def games_data(hub: GameHub) -> dict:
    return {"running": hub.started, "games": hub.statuses()}


def routes(hub: GameHub) -> list[Route]:
    async def page(request):
        return FileResponse(SETTINGS_PAGE, headers={"Cache-Control": "no-store"})

    async def data(request):
        if request.method == "GET":
            return JSONResponse(describe(), headers={"Cache-Control": "no-store"})
        if not trusted(request):
            return JSONResponse({"error": "请求来源不对"}, status_code=403)
        try:
            body = await request.json()
            save(body if isinstance(body, dict) else {})
        except (ValueError, OSError) as error:
            return JSONResponse({"error": explain(error)}, status_code=400)
        return JSONResponse(describe())

    async def games(request):
        if request.method == "GET":
            return JSONResponse(games_data(hub), headers={"Cache-Control": "no-store"})
        if not trusted(request):
            return JSONResponse({"error": "请求来源不对"}, status_code=403)
        try:
            body = await request.json()
            body = body if isinstance(body, dict) else {}
            action, game_id = body.get("action"), body.get("id")
            if action == "preview":
                return JSONResponse({"preview": await hub.preview(str(body.get("url", "")))})
            if action == "install":
                granted = body.get("granted") or []
                if not isinstance(granted, list) or not all(isinstance(name, str) for name in granted):
                    raise ValueError("请求内容不对")
                await hub.install(str(body.get("url", "")), granted)
            elif action in {"enable", "disable"}:
                await hub.set_enabled(str(game_id), action == "enable")
            elif action == "grant":
                names = body.get("capabilities") or []
                if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                    raise ValueError("请求内容不对")
                await hub.grant(str(game_id), names)
            elif action == "uninstall":
                await hub.uninstall(str(game_id))
            else:
                raise ValueError("不认识的操作")
        except KeyError:
            return JSONResponse({"error": "没有这个游戏"}, status_code=404)
        except (ValueError, OSError) as error:
            return JSONResponse({"error": explain(error)}, status_code=400)
        return JSONResponse(games_data(hub))

    return [
        Route("/settings", page), Route("/settings/data", data, methods=["GET", "POST"]),
        Route("/settings/games", games, methods=["GET", "POST"]),
    ]
