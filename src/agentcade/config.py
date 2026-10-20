import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Self
from urllib.parse import urlsplit, urlunsplit

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

# 源码都在 src 下；应用目录是它的上一级，放着 app.json 和 assets。
SOURCE = Path(__file__).resolve().parent.parent
ROOT = SOURCE.parent
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class Option(BaseModel):
    """一项配置的声明，写在每个 Agent 目录的 settings.yaml 里。

    设置页面按这份声明生成表单，读取配置时也按它补默认值、检查必填项。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    key: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")  # 环境变量名
    label: str = Field(min_length=1)  # 设置页面上显示的说明
    default: str = ""  # 没有默认值的是必填项
    secret: bool = False  # 密钥，页面上不回显
    advanced: bool = False  # 一般不用改，页面上收在“更多设置”里


class OptionFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    options: tuple[Option, ...]


def load_options(path: Path) -> tuple[Option, ...]:
    """读取一个 Agent 目录里的配置项声明。"""
    options = OptionFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).options
    if len({option.key for option in options}) != len(options):
        raise ValueError(f"配置项不能重名：{path}")
    return options


def user_config_dir() -> Path:
    """安装后的应用保存用户配置的目录，在源码目录之外，升级和重装应用时不会丢。"""
    manifest = ROOT / "app.json"
    # 作为库安装时（单独部署游戏服务、CI）没有 app.json，也用不到用户目录。
    app_id = json.loads(manifest.read_text(encoding="utf-8"))["id"] if manifest.is_file() else "agentcade"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "watcherobot" / "applications" / app_id / "config"


def config_file(path: Path) -> Path:
    """Agent 目录里有 .env 时用它（本地开发），没有时用用户配置目录里对应的文件。"""
    if path.is_file() or not path.is_relative_to(SOURCE):
        return path
    return user_config_dir() / f"{path.parent.relative_to(SOURCE).as_posix()}.env"


def read_environment(path: Path) -> dict[str, str]:
    path = config_file(path)
    if not path.is_file():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    values = dotenv_values(path, interpolate=False)
    if any(value is None for value in values.values()):
        raise ValueError(f"配置项必须使用 KEY=VALUE 格式：{path}")
    return dict(values)


def read_options(options: tuple[Option, ...], path: Path) -> dict[str, str]:
    """按声明读取一个 Agent 的配置：没写的用默认值，必填项没填就报错。"""
    return resolve_options(options, read_environment(path))


def resolve_options(options: tuple[Option, ...], stored: dict[str, str]) -> dict[str, str]:
    values = {}
    for option in options:
        values[option.key] = (stored.get(option.key) or "").strip() or option.default
        if not values[option.key]:
            raise ValueError(f"还没有填写 {option.key}")
    return values


class AgentSettings:
    """主 Agent 和游戏 Agent 的配置都按这个约定写：

    - 目录里的 settings.yaml 列出这个 Agent 读取的全部环境变量；
    - 配置类用 DIRECTORY 指明自己的目录，实际的值在这个目录的 .env 里，
      没有 .env 时在用户配置目录里；
    - from_values 把读到的值转换成配置对象。

    每个 Agent 只读自己的那一份，互相不共用。
    """

    DIRECTORY: ClassVar[Path]

    @classmethod
    def options(cls) -> tuple[Option, ...]:
        return load_options(cls.DIRECTORY / "settings.yaml")

    @classmethod
    def env_file(cls) -> Path:
        return cls.DIRECTORY / ".env"

    @classmethod
    def from_environment(cls, path: Path | None = None) -> Self:
        return cls.from_values(read_options(cls.options(), path or cls.env_file()))

    @classmethod
    def from_process(cls) -> Self:
        """单独部署的游戏服务读取配置：先读目录里的 .env（有的话），再用同名环境变量覆盖。

        服务器上一般没有 .env，密钥放在环境变量里，例如 Hugging Face Space 的 Secrets。
        只读取 settings.yaml 声明过的那几项。
        """
        options = cls.options()
        path = cls.env_file()
        stored = dict(dotenv_values(path, interpolate=False)) if path.is_file() else {}
        stored.update({option.key: os.environ[option.key] for option in options
                       if os.environ.get(option.key)})
        return cls.from_values(resolve_options(options, stored))

    @classmethod
    def from_values(cls, values: dict[str, str]) -> Self:
        raise NotImplementedError


@dataclass(frozen=True)
class LLMSettings:
    api_key: str = field(repr=False)
    base_url: str
    model_name: str
    timeout_seconds: int
    extra_body: dict

    @classmethod
    def from_values(cls, values: dict[str, str]) -> "LLMSettings":
        return cls(
            api_key=values["LLM_API_KEY"],
            base_url=values["LLM_BASE_URL"],
            model_name=values["LLM_MODEL_NAME"],
            timeout_seconds=int(values["LLM_TIMEOUT_SECONDS"]),
            extra_body=json.loads(values["LLM_EXTRA_BODY_JSON"]),
        )

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS 必须大于零")
        if not isinstance(self.extra_body, dict):
            raise TypeError("LLM_EXTRA_BODY_JSON 必须是 JSON 对象")
        if {"model", "messages", "response_format", "stream", "tools", "tool_choice",
            "parallel_tool_calls"} & self.extra_body.keys():
            raise ValueError("LLM_EXTRA_BODY_JSON 不能覆盖模型、消息、响应格式、流式模式或工具参数")
        parts = urlsplit(self.base_url)
        if (
            parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
        ):
            raise ValueError("LLM_BASE_URL 必须是有效的 HTTP 或 HTTPS 接口地址")
        if not self.api_key.strip() or not self.model_name.strip():
            raise ValueError("OpenAI 兼容接口需要 LLM_API_KEY 和 LLM_MODEL_NAME")


@dataclass(frozen=True)
class Settings(AgentSettings):
    DIRECTORY: ClassVar[Path] = Path(__file__).parent

    dashscope_api_key: str = field(repr=False)
    llm: LLMSettings
    base_url: str
    asr_model: str
    tts_model: str
    tts_voice: str
    speech_timeout_seconds: int
    asr_silence_ms: int
    input_gain: float
    observability_port: int

    def __post_init__(self) -> None:
        for name in (
            "dashscope_api_key",
            "asr_model", "tts_model", "tts_voice",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"配置 {name} 不能为空")
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("DASHSCOPE_BASE_URL 必须是有效的 HTTPS 地址")
        if self.speech_timeout_seconds <= 0:
            raise ValueError("SPEECH_TIMEOUT_SECONDS 必须大于零")
        if not 200 <= self.asr_silence_ms <= 6000:
            raise ValueError("ASR_SILENCE_MS 必须在 200 至 6000 之间")
        if not 1 <= self.input_gain <= 8:
            raise ValueError("ASR_INPUT_GAIN 必须在 1 至 8 之间")
        if not 1 <= self.observability_port <= 65535:
            raise ValueError("OBSERVABILITY_PORT 必须在 1 至 65535 之间")

    @property
    def websocket_url(self) -> str:
        parts = urlsplit(self.base_url)
        return urlunsplit((
            "wss", parts.netloc, "/api-ws/v1/inference", "", "",
        ))

    @classmethod
    def from_values(cls, values: dict[str, str]) -> "Settings":
        return cls(
            dashscope_api_key=values["DASHSCOPE_API_KEY"],
            llm=LLMSettings.from_values(values),
            base_url=values["DASHSCOPE_BASE_URL"],
            asr_model=values["QWEN_ASR_MODEL"],
            tts_model=values["QWEN_TTS_MODEL"],
            tts_voice=values["QWEN_TTS_VOICE"],
            speech_timeout_seconds=int(values["SPEECH_TIMEOUT_SECONDS"]),
            asr_silence_ms=int(values["ASR_SILENCE_MS"]),
            input_gain=float(values["ASR_INPUT_GAIN"]),
            observability_port=int(values["OBSERVABILITY_PORT"]),
        )
