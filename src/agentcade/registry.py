"""已安装的游戏清单：内置游戏是否启用，以及用户添加的远程游戏和同意给它的能力。

清单放在用户目录，和源码分开，升级或重装应用都不会丢。内置游戏从 src/games/ 自动发现，
默认启用，只有停用了才会记在清单里。
"""
import ipaddress
import logging
import os
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentcade.config import user_config_dir

logger = logging.getLogger(__name__)

PATH = user_config_dir().parent / "data" / "games.json"

# 设置页面上给用户看的能力说明，对应游戏可以调用的机器人 SDK 方法（见 robot_calls.SDK_CALLS）；
# 没有列出的能力按原名显示。
PERMISSIONS = {
    "motion": "转动机器人的头，比如点头、摇头",
    "expression.runtime.v3": "在机器人屏幕上显示表情和图案",
    "light": "控制机器人的灯光",
    "animation": "在机器人屏幕上播放自带的动画",
    "behavior": "让机器人做自带的组合动作",
    "audio": "播放机器人自带的音效",
    "resource.expression.official": "播放机器人的官方表情",
    "resource.work.play": "播放机器人里保存的作品",
    "resource.work.expression.play": "播放机器人里保存的表情作品",
}


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    source: Literal["builtin", "remote"]
    enabled: bool = True
    # 远程游戏的地址、安装时的名称和简短介绍（连不上时页面用来显示）、用户同意的能力和安装时间。
    url: str | None = None
    name: str | None = None
    summary: str | None = None
    granted: list[str] = Field(default_factory=list)
    installed_at: float | None = None
    timeout_seconds: int = Field(default=210, gt=0)

    @model_validator(mode="after")
    def validate_source(self):
        if (self.source == "remote") != (self.url is not None):
            raise ValueError("只有远程游戏有地址，并且必须有")
        return self


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    games: list[Entry] = Field(default_factory=list)

    def get(self, game_id: str) -> Entry | None:
        return next((entry for entry in self.games if entry.id == game_id), None)

    def remote(self) -> list[Entry]:
        return [entry for entry in self.games if entry.source == "remote"]

    def enabled(self, game_id: str) -> bool:
        entry = self.get(game_id)
        return entry is None or entry.enabled


def load(path: Path = PATH) -> Registry:
    if not path.is_file():
        return Registry()
    try:
        return Registry.model_validate_json(path.read_bytes())
    except Exception:
        # 清单损坏时换个名字留着，按空清单继续，内置游戏照常可用。
        broken = path.with_name(f"{path.name}.broken-{int(time.time())}")
        logger.warning("游戏清单 %s 读取失败，已改名为 %s", path, broken.name, exc_info=True)
        path.replace(broken)
        return Registry()


def save(registry: Registry, path: Path = PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".tmp")
    pending.write_text(registry.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    os.replace(pending, path)


def local_host(host: str) -> bool:
    """本机或局域网地址。"""
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def check_url(url: str) -> str:
    """检查用户填的游戏地址，返回去掉末尾斜杠的地址。可以是 https 域名，也可以是 http 的 IP 加端口。"""
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("游戏地址要是 http:// 或 https:// 开头的网址，"
                         "例如 https://example.com/my-game、http://1.2.3.4:7860")
    try:
        _ = parts.port  # 端口不是 0 到 65535 的数字时抛错
    except ValueError:
        raise ValueError(f"游戏地址的端口不对：{parts.netloc}") from None
    return url


def unencrypted(url: str) -> bool:
    """公网上用 http 的地址：玩游戏时说的话和游戏的回复都明文传输，安装时提醒用户。"""
    parts = urlsplit(url)
    return parts.scheme == "http" and not local_host(parts.hostname or "")
