import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://llm-2lb3ys6gg99neqej.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


@dataclass(frozen=True)
class Settings:
    dashscope_api_key: str = field(repr=False)
    typesafe_api_key: str = field(repr=False)
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
            base_url=os.getenv("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL),
            asr_model=os.getenv("QWEN_ASR_MODEL", "qwen-audio-3.1-asr-flash-streaming"),
            tts_model=os.getenv("QWEN_TTS_MODEL", "qwen-audio-3.1-tts-flash"),
            tts_voice=os.getenv("QWEN_TTS_VOICE", "yuxiaoyun_v3.1"),
            jev_model=os.getenv("JEV_MODEL", "jev-latest"),
            confidence_threshold=float(os.getenv("JEV_CONFIDENCE_THRESHOLD", "0.65")),
        )
