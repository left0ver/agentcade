"""根据每个 Agent 目录里的 settings.yaml，重新生成同目录的 .env.example。

配置项只在 settings.yaml 里声明一次。改了声明之后运行：

    uv run python scripts/write_env_examples.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentcade.settings_page import sections


def example(section) -> str:
    lines = [
        f"# {section.title}：由 scripts/write_env_examples.py 根据 settings.yaml 生成，不要手改。",
        "# 本地开发时复制为本目录的 .env；安装后的应用在设置页面里填写。",
    ]
    for advanced, heading in ((False, "# 主要设置，没有默认值的必须填写。"), (True, "# 以下一般不用改。")):
        options = [option for option in section.config.options() if option.advanced is advanced]
        if not options:
            continue
        lines += ["", heading]
        for option in options:
            lines += [f"# {option.label}", f"{option.key}={option.default}"]
    return "\n".join(lines) + "\n"


def main():
    for section in sections():
        path = section.local.with_name(".env.example")
        path.write_text(example(section), encoding="utf-8")
        print("已生成", path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path)


if __name__ == "__main__":
    main()
