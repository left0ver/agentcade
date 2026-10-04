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
    deepseek_api_key: str = field(default="", repr=False)
    drawing_provider: str = "deepseek"
    drawing_timeout_seconds: int = 180
    drawing_estimate_seconds: int = 60
    claude_code_command: str = "claude"
    claude_code_model: str = ""
    codex_command: str = "codex"
    codex_model: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
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
        if self.drawing_provider not in {"deepseek", "claude_code", "codex"}:
            raise ValueError("DRAWING_PROVIDER 必须是 deepseek、claude_code 或 codex")
        if self.drawing_timeout_seconds <= 0 or self.drawing_estimate_seconds <= 0:
            raise ValueError("绘图超时和预计等待时间必须大于零")
        urls = [("DASHSCOPE_BASE_URL", self.base_url)]
        if self.drawing_provider == "deepseek":
            if not self.deepseek_api_key.strip() or not self.deepseek_model.strip():
                raise ValueError("DeepSeek provider 需要 DEEPSEEK_API_KEY 和 DEEPSEEK_MODEL")
            urls.append(("DEEPSEEK_BASE_URL", self.deepseek_base_url))
        elif not getattr(self, f"{self.drawing_provider}_command").strip():
            raise ValueError("本机绘图工具的命令路径不能为空")
        for name, url in urls:
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                raise ValueError(f"{name} 必须是有效的 HTTPS 地址")
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
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            drawing_provider=os.getenv("DRAWING_PROVIDER", "deepseek"),
            drawing_timeout_seconds=int(os.getenv("DRAWING_TIMEOUT_SECONDS", "180")),
            drawing_estimate_seconds=int(os.getenv("DRAWING_ESTIMATE_SECONDS", "60")),
            claude_code_command=os.getenv("CLAUDE_CODE_COMMAND", "claude"),
            claude_code_model=os.getenv("CLAUDE_CODE_MODEL", ""),
            codex_command=os.getenv("CODEX_COMMAND", "codex"),
            codex_model=os.getenv("CODEX_MODEL", ""),
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            base_url=os.getenv("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL),
            asr_model=os.getenv("QWEN_ASR_MODEL", "qwen-audio-3.1-asr-flash-streaming"),
            tts_model=os.getenv("QWEN_TTS_MODEL", "qwen-audio-3.1-tts-flash"),
            tts_voice=os.getenv("QWEN_TTS_VOICE", "yuxiaoyun_v3.1"),
            jev_model=os.getenv("JEV_MODEL", "jev-latest"),
            confidence_threshold=float(os.getenv("JEV_CONFIDENCE_THRESHOLD", "0.65")),
        )
