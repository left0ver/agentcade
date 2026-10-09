import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://llm-2lb3ys6gg99neqej.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


def read_environment(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    values = dotenv_values(path, interpolate=False)
    if any(value is None for value in values.values()):
        raise ValueError(f"配置项必须使用 KEY=VALUE 格式：{path}")
    return dict(values)


@dataclass(frozen=True)
class LLMSettings:
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    model_name: str = ""
    timeout_seconds: int = 180
    extra_body: dict = field(default_factory=dict)

    @classmethod
    def from_values(cls, values: dict[str, str]) -> "LLMSettings":
        return cls(
            api_key=values.get("LLM_API_KEY", ""),
            base_url=values.get("LLM_BASE_URL", ""),
            model_name=values.get("LLM_MODEL_NAME", ""),
            timeout_seconds=int(values.get("LLM_TIMEOUT_SECONDS", "180")),
            extra_body=json.loads(values.get("LLM_EXTRA_BODY_JSON", "{}")),
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
class Settings:
    dashscope_api_key: str = field(repr=False)
    llm: LLMSettings
    game_catalog: Path = ROOT / "games.json"
    base_url: str = DEFAULT_BASE_URL
    asr_model: str = "qwen-audio-3.1-asr-flash-streaming"
    tts_model: str = "qwen-audio-3.1-tts-flash"
    tts_voice: str = "yuxiaoyun_v3.1"
    speech_timeout_seconds: int = 90
    asr_silence_ms: int = 800
    input_gain: float = 4.0
    observability_port: int = 8765

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
    def from_environment(cls, path: Path | None = None) -> "Settings":
        if path is None:
            path = Path(__file__).with_name(".env")
        values = read_environment(path)
        catalog = Path(values.get("GAME_CATALOG") or ROOT / "games.json")
        if not catalog.is_absolute():
            catalog = path.parent / catalog
        return cls(
            dashscope_api_key=values["DASHSCOPE_API_KEY"],
            llm=LLMSettings.from_values(values),
            game_catalog=catalog.resolve(),
            base_url=values.get("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL),
            asr_model=values.get("QWEN_ASR_MODEL", "qwen-audio-3.1-asr-flash-streaming"),
            tts_model=values.get("QWEN_TTS_MODEL", "qwen-audio-3.1-tts-flash"),
            tts_voice=values.get("QWEN_TTS_VOICE", "yuxiaoyun_v3.1"),
            speech_timeout_seconds=int(values.get("SPEECH_TIMEOUT_SECONDS", "90")),
            asr_silence_ms=int(values.get("ASR_SILENCE_MS", "800")),
            input_gain=float(values.get("ASR_INPUT_GAIN", "4")),
            observability_port=int(values.get("OBSERVABILITY_PORT", "8765")),
        )
