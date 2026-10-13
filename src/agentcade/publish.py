"""uv run publish：登录发布平台，把应用源码发布到自己名下的公开仓库。

发布分两步，中间会停下来确认：
1. 没登录时先执行 watcherobot app login，按终端提示在浏览器里完成授权；
2. 确认之后执行 watcherobot app publish，源码从这一步开始公开。

加 --submit 时在发布之后提交到官方名单审核，合并后应用才会出现在应用广场里。
"""
import argparse
import json
import subprocess
import sys

from watcherobot.distribution.source_files import collect_application_source_files

from agentcade.config import ROOT


def watcherobot(*arguments: str, capture: bool = False) -> subprocess.CompletedProcess:
    # 用当前环境里的 SDK，和 uv run watcherobot 是同一个命令。
    return subprocess.run(
        [sys.executable, "-c", "from watcherobot.cli import main; raise SystemExit(main())", *arguments],
        cwd=ROOT, capture_output=capture, text=True, check=False,
    )


def logged_in(provider: str) -> bool:
    output = watcherobot("app", "login", "--provider", provider, "--status", "--jsonl", capture=True).stdout
    for line in reversed(output.splitlines()):
        if line.startswith("{"):
            event = json.loads(line)
            return event.get("type") == "result" and bool(event.get("data", {}).get("logged_in"))
    return False


def step(title: str) -> None:
    print(f"\n== {title} ==", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="publish", description="登录并发布应用源码。")
    parser.add_argument("--provider", default="huggingface", choices=["huggingface", "gitee"])
    parser.add_argument("--submit", action="store_true", help="发布之后提交到官方名单审核")
    options = parser.parse_args()
    provider = ("--provider", options.provider)

    step("检查应用")
    if watcherobot("app", "check", ".").returncode:
        return 2
    manifest = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))

    step(f"登录 {options.provider}")
    if logged_in(options.provider):
        print("已经登录。")
    elif watcherobot("app", "login", *provider).returncode or not logged_in(options.provider):
        print("登录没有完成，已停止。")
        return 3

    step("发布源码")
    files = collect_application_source_files(ROOT)
    print(f"将把 {len(files)} 个文件发布到你名下的公开仓库 WatcherRobot-{manifest['id']}，"
          f"版本 {manifest['version']}。发布后任何人都能看到这些源码。")
    if input("继续吗？输入 y 确认：").strip().lower() != "y":
        print("已取消，没有发布。")
        return 130
    if watcherobot("app", "publish", *provider, ".").returncode:
        return 4

    if not options.submit:
        print("\n源码已发布。要出现在应用广场里，还需要提交审核：uv run publish --submit")
        return 0
    step("提交审核")
    return 4 if watcherobot("app", "submit", *provider, ".").returncode else 0


if __name__ == "__main__":
    raise SystemExit(main())
