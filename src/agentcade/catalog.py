"""游戏目录：审核过的远程游戏列表，放在公开仓库的 catalog/games.json。

设置页面从这里列出可以安装的游戏，并按地址给已安装和要安装的远程游戏标出审核状态。
游戏运行在开发者自己的服务器上，随时可能更新；目录记的是审核时的版本和能力，
和名片对不上时提示用户。提交和审核的流程见 catalog/README.md。
"""
import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agentcade import registry

logger = logging.getLogger(__name__)

# 官方目录，连不上时换 jsDelivr 镜像。
OFFICIAL = (
    "https://raw.githubusercontent.com/left0ver/agentcade/main/catalog/games.json",
    "https://cdn.jsdelivr.net/gh/left0ver/agentcade@main/catalog/games.json",
)
CACHE = registry.PATH.with_name("catalog.json")
# 目录文件的格式版本，只在旧版小懒读不懂新格式时才增加。
FORMAT = 1
# 设置页面打开时读取目录，这段时间内再打开直接用上次读到的。
FRESH_SECONDS = 300
TIMEOUT_SECONDS = 8
MAX_BYTES = 1 << 20


class Listing(BaseModel):
    """目录里的一个游戏，内容和审核时的名片一致。不认识的字段忽略，留给以后的版本。"""

    model_config = ConfigDict(str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    name: str = Field(min_length=1, max_length=32)
    summary: str = Field(min_length=1, max_length=30)
    url: str
    author: str = Field(min_length=1, max_length=64)
    homepage: str = Field(default="", max_length=512)
    version: str = Field(min_length=1, max_length=32)
    capabilities: list[str] = Field(default_factory=list)
    reviewed_at: date
    # 下架的游戏不再显示在目录里，已经装了的用户会看到 note 写的原因。
    status: Literal["listed", "removed"] = "listed"
    note: str = Field(default="", max_length=200)

    @field_validator("url")
    @classmethod
    def normalize(cls, value: str) -> str:
        return registry.check_url(value)

    @model_validator(mode="after")
    def validate_note(self):
        if self.status == "removed" and not self.note:
            raise ValueError("下架的游戏要在 note 里写明原因")
        return self


def reason(error: ValidationError) -> str:
    item = error.errors()[0]
    message = item["msg"].removeprefix("Value error, ")
    location = ".".join(map(str, item["loc"]))
    return f"{location}：{message}" if location else message


class Catalog(BaseModel):
    games: list[Listing] = Field(default_factory=list)

    def find(self, url: str) -> Listing | None:
        # 按地址认游戏，不按 ID：没收录的游戏用了同一个 ID 也冒充不了。
        url = url.rstrip("/")
        return next((listing for listing in self.games if listing.url == url), None)

    def review(self, url: str, version: str | None = None,
               capabilities: list[str] | None = None) -> dict:
        """这个地址的游戏的审核状态：reviewed、changed、unreviewed 或 removed。

        知道名片的版本和能力时才比较；游戏没连上时只看目录里有没有、下没下架。
        """
        listing = self.find(url)
        if listing is None:
            return {"state": "unreviewed"}
        if listing.status == "removed":
            return {"state": "removed", "note": listing.note}
        extra = [name for name in capabilities or [] if name not in listing.capabilities]
        changed = (version is not None and version != listing.version) or bool(extra)
        return {
            "state": "changed" if changed else "reviewed", "version": listing.version,
            "reviewed_at": listing.reviewed_at.isoformat(),
            "extra": [{"name": name, "label": registry.PERMISSIONS.get(name, name)} for name in extra],
        }


class Document(BaseModel):
    """目录文件的外层结构；每个游戏的条目单独检查。"""

    format: int
    games: list[dict]


def read(data: object) -> Document:
    try:
        document = Document.model_validate(data)
    except ValidationError as error:
        raise ValueError(f"目录文件的格式不对，{reason(error)}") from error
    if document.format != FORMAT:
        raise ValueError(f"目录文件的格式版本是 {document.format}，这个版本的小懒只认 {FORMAT}，请升级小懒")
    return document


def parse(data: object) -> tuple[Catalog, list[str]]:
    """解析目录文件的内容。某个游戏的条目有问题时跳过它，不影响其他游戏，问题一并返回。"""
    games, problems = [], []
    for index, item in enumerate(read(data).games, 1):
        try:
            games.append(Listing.model_validate(item))
        except ValidationError as error:
            problems.append(f"第 {index} 个游戏 {item.get('id', '')}：{reason(error)}")
    return Catalog(games=games), problems


def validate(data: object, builtin: set[str]) -> list[str]:
    """审核用的严格检查：除了每个条目合规，还要没有多余字段、ID 和地址不重复、
    不和内置游戏重名、地址是公网的 https。返回发现的问题。"""
    try:
        catalog, problems = parse(data)
    except ValueError as error:
        return [str(error)]
    fields = set(Listing.model_fields)
    for index, item in enumerate(read(data).games, 1):
        unknown = sorted(set(item) - fields)
        if unknown:
            problems.append(f"第 {index} 个游戏 {item.get('id', '')}：不认识的字段 {'、'.join(unknown)}")
    seen_ids, seen_urls = set(), set()
    for listing in catalog.games:
        if listing.id in builtin:
            problems.append(f"{listing.id}：和内置游戏重名")
        if listing.id in seen_ids:
            problems.append(f"{listing.id}：ID 重复")
        if listing.url in seen_urls:
            problems.append(f"{listing.id}：地址 {listing.url} 重复")
        if urlsplit(listing.url).scheme != "https":
            problems.append(f"{listing.id}：目录里的游戏地址要用公网的 https")
        seen_ids.add(listing.id)
        seen_urls.add(listing.url)
    return problems


def entry(url: str, descriptor) -> dict:
    """按游戏名片生成一条目录条目，开发者提交时复制进 catalog/games.json。"""
    return {
        "id": descriptor.id, "name": descriptor.name, "summary": descriptor.summary, "url": url,
        "author": descriptor.author, "homepage": descriptor.homepage, "version": descriptor.version,
        "capabilities": descriptor.required_capabilities,
        "reviewed_at": datetime.now().astimezone().date().isoformat(), "status": "listed",
    }


def check_source(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"游戏目录的地址不对：{url}")
    if parts.scheme == "http" and not registry.local_host(parts.hostname):
        raise ValueError("游戏目录在公网上时要用 https")
    return url


class CatalogStore:
    """读取并缓存游戏目录。读不到时用上次缓存的内容，设置页面上说明原因。

    缓存放在用户目录，启动时不联网，只在设置页面打开、刷新或者查看游戏时才去读。
    """

    def __init__(self, cache: Path = CACHE):
        self.cache = cache
        # 由应用设置：从哪些地址读目录，依次尝试。
        self.sources: Callable[[], tuple[str, ...]] = lambda: OFFICIAL
        self.catalog: Catalog | None = None
        self.loaded_for: tuple[str, ...] = ()
        self.source: str | None = None
        self.fetched_at: float | None = None
        self.checked_at: float | None = None
        self.error: str | None = None
        self.lock = asyncio.Lock()

    def current(self) -> Catalog:
        sources = self.sources()
        if self.catalog is None or self.loaded_for != sources:
            self.load(sources)
        return self.catalog

    def load(self, sources: tuple[str, ...]) -> None:
        """读缓存。缓存来自别的目录地址时不用它（用户改了目录地址）。"""
        self.catalog, self.loaded_for = Catalog(), sources
        self.source = self.fetched_at = self.checked_at = self.error = None
        try:
            cached = json.loads(self.cache.read_text(encoding="utf-8"))
            if cached.get("sources") != list(sources):
                return
            self.catalog, _ = parse(cached["catalog"])
            self.source, self.fetched_at = cached["source"], cached["fetched_at"]
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("游戏目录的缓存 %s 读取失败，已忽略", self.cache, exc_info=True)

    def save(self, sources: tuple[str, ...], data: dict) -> None:
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        pending = self.cache.with_name(self.cache.name + ".tmp")
        pending.write_text(json.dumps({
            "sources": list(sources), "source": self.source, "fetched_at": self.fetched_at,
            "catalog": data,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(pending, self.cache)

    async def refresh(self, force: bool = False) -> None:
        """重新读取目录；不是强制刷新时，几分钟内读过就不再读。"""
        async with self.lock:
            self.current()
            if (not force and self.checked_at is not None
                    and time.monotonic() - self.checked_at < FRESH_SECONDS):
                return
            sources = self.loaded_for
            self.checked_at = time.monotonic()
            failures = []
            for url in sources:
                try:
                    data = await download(check_source(url))
                    catalog, problems = parse(data)
                except (httpx.HTTPError, ValueError) as error:
                    failures.append(f"{urlsplit(url).hostname}：{explain(error)}")
                    continue
                for problem in problems:
                    logger.warning("游戏目录 %s 里%s，已跳过", url, problem)
                self.catalog, self.source, self.fetched_at, self.error = catalog, url, time.time(), None
                self.save(sources, data)
                return
            self.error = "游戏目录暂时读不到（" + "；".join(failures) + "）"
            logger.warning("%s", self.error)


async def download(url: str) -> object:
    # 公网上的目录走系统代理，方便连 GitHub；本机和局域网的目录直接连。
    direct = registry.local_host(urlsplit(url).hostname or "")
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True,
                                 trust_env=not direct) as http:
        response = await http.get(url, headers={"Cache-Control": "no-cache"})
    response.raise_for_status()
    if len(response.content) > MAX_BYTES:
        raise ValueError("目录文件太大")
    return response.json()


def explain(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return "连接超时"
    if isinstance(error, httpx.RequestError):
        return "连不上"
    if isinstance(error, json.JSONDecodeError):
        return "不是 JSON 文件"
    return str(error)[:200]
