import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://llm-2lb3ys6gg99neqej.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "openai"
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    model_name: str = ""
    command: str = ""
    timeout_seconds: int = 180
    extra_body: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provider not in {"openai", "claude_code", "codex"}:
            raise ValueError("LLM_PROVIDER 必须是 openai、claude_code 或 codex")
        if self.timeout_seconds <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS 必须大于零")
        if not isinstance(self.extra_body, dict):
            raise TypeError("LLM_EXTRA_BODY_JSON 必须是 JSON 对象")
        if {"model", "messages", "response_format", "stream"} & self.extra_body.keys():
            raise ValueError("LLM_EXTRA_BODY_JSON 不能覆盖模型、消息、响应格式或流式模式")
        if self.provider == "openai":
            parts = urlsplit(self.base_url)
            if (
                parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment
            ):
                raise ValueError("LLM_BASE_URL 必须是有效的 HTTP 或 HTTPS 接口地址")
            if not self.api_key.strip() or not self.model_name.strip():
                raise ValueError("OpenAI 兼容接口需要 LLM_API_KEY 和 LLM_MODEL_NAME")
        elif self.extra_body:
            raise ValueError("LLM_EXTRA_BODY_JSON 仅用于 OpenAI 兼容接口")


@dataclass(frozen=True)
class Settings:
    dashscope_api_key: str = field(repr=False)
    typesafe_api_key: str = field(repr=False)
    llm: LLMSettings
    drawing_estimate_seconds: int = 60
    base_url: str = DEFAULT_BASE_URL
    asr_model: str = "qwen-audio-3.1-asr-flash-streaming"
    tts_model: str = "qwen-audio-3.1-tts-flash"
    tts_voice: str = "yuxiaoyun_v3.1"
    jev_model: str = "jev-latest"
    confidence_threshold: float = 0.65

    def __post_init__(self) -> None:
        for name in (
            "dashscope_api_key",
            "typesafe_api_key",
            "asr_model",
            "tts_model",
            "tts_voice",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"配置 {name} 不能为空")
        if self.drawing_estimate_seconds <= 0:
            raise ValueError("预计等待时间必须大于零")
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("DASHSCOPE_BASE_URL 必须是有效的 HTTPS 地址")
        if not 0.5 <= self.confidence_threshold <= 1.0:
            raise ValueError("JEV_CONFIDENCE_THRESHOLD 必须位于 0.5 与 1.0 之间")

    @property
    def websocket_url(self) -> str:
        parts = urlsplit(self.base_url)
        return urlunsplit(("wss", parts.netloc, "/api-ws/v1/inference", "", ""))

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(ROOT / ".env", override=False)
        return cls(
            dashscope_api_key=os.environ["DASHSCOPE_API_KEY"],
            typesafe_api_key=os.environ["TYPESAFE_API_KEY"],
            llm=LLMSettings(
                provider=os.getenv("LLM_PROVIDER", "openai"),
                api_key=os.getenv("LLM_API_KEY", ""),
                base_url=os.getenv("LLM_BASE_URL", ""),
                model_name=os.getenv("LLM_MODEL_NAME", ""),
                command=os.getenv("LLM_COMMAND", ""),
                timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
                extra_body=json.loads(os.getenv("LLM_EXTRA_BODY_JSON", "{}")),
            ),
            drawing_estimate_seconds=int(os.getenv("DRAWING_ESTIMATE_SECONDS", "60")),
            base_url=os.getenv("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL),
            asr_model=os.getenv("QWEN_ASR_MODEL", "qwen-audio-3.1-asr-flash-streaming"),
            tts_model=os.getenv("QWEN_TTS_MODEL", "qwen-audio-3.1-tts-flash"),
            tts_voice=os.getenv("QWEN_TTS_VOICE", "yuxiaoyun_v3.1"),
            jev_model=os.getenv("JEV_MODEL", "jev-latest"),
            confidence_threshold=float(os.getenv("JEV_CONFIDENCE_THRESHOLD", "0.65")),
        )
