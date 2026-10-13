"""复现：WatcheRobot 每打开再关闭一次麦克风，设备空闲内存减少约 24 KB，并且不归还。

运行方法
--------
电脑上已经安装 watcherobot SDK，SDK Daemon 在运行，机器人已配对并在线，然后执行：

    python mic_leak_repro.py

脚本会自己生成一个临时 Application，交给 SDK Daemon 运行，结束后删除。全程不播放声音，
大约 2 分钟。只用到 SDK 的公开接口：robot.microphone.open_pcm()、robot.resource_snapshot，
以及用 robot.expression_runtime.start() 把屏幕换成静态画面（设备默认的待机动画会让内存读数抖动）。

现象（固件 ESP32 v0.4.4，SDK 0.1.10，2026-10-13 实测）
------------------------------------------------------
0. 两个固件版本都能复现：v0.4.4，以及网页烧录的 v0.5.7-test.1（21 次开关少 491,600 字节）。
   下面的数字除特别说明外来自 v0.4.4。
1. 每执行一次 ctrl.microphone.open 加 ctrl.microphone.close，设备上报的 PSRAM 空闲内存减少
   24,580 字节，internal 减少约 60 字节，之后不会恢复。20 次减少 491,600 字节，非常稳定。
2. 泄漏和打开多久无关：麦克风连续打开 150 秒、收了 4.8 MB 音频，期间空闲内存不变，
   关闭之后同样只减少约 24,580 字节。
3. 空闲、播放 PCM、显示表情、更新矢量图案、头部动作都不会让空闲内存减少。
4. 24,580 = 24,576 + 4，正好是一块 24 KB 的分配加一个堆块头，看起来是打开麦克风时的
   某一块固定分配在关闭时没有释放。

影响
----
重启后 PSRAM 空闲约 6.17 MB，大约 250 次麦克风开关之后耗尽。剩余约 0.7 MB 时设备开始出错：
新打开的麦克风会话立刻被设备结束（SDK 报 microphone session is closed），
ctrl.expression.runtime.start 被拒绝（executor_failed），只能给设备断电重启。
设备在麦克风打开时会拒绝 ctrl.audio.stream.begin（busy），所以语音对话类应用每说一句话
都必须开关一次麦克风，连续对话 60 到 80 分钟就会触发。

预期
----
关闭麦克风后，空闲内存回到打开之前的水平。
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CYCLES = 10            # 每一轮开关麦克风的次数
HOLD_SECONDS = 60      # 对照：麦克风只打开一次并保持这么久
STABLE_SECONDS = 5     # 设备上报的空闲内存连续这么久不变，才当作稳定值
LEAK_THRESHOLD = 1000  # 平均每次开关减少超过这么多字节，判定为复现

MANIFEST = {
    "schema_version": 2,
    "id": "local.watcherobot_mic_leak_repro",
    "name": "麦克风内存泄漏复现",
    "version": "0.1.0",
    "requires_watcherobot": ">=0.1.10,<0.2",
    "dependencies": [],
    "supported_host_platforms": ["windows", "macos"],
    "description": "反复开关麦克风，观察设备空闲内存。",
    "author": "Local Developer",
}


def say(text=""):
    print(text, flush=True)


def free_memory(robot):
    """设备最近一次上报的空闲内存，单位字节。设备还没有上报时返回 None。"""
    memory = robot.resource_snapshot.get("memory") or {}
    psram = (memory.get("psram") or {}).get("free_bytes")
    internal = (memory.get("internal") or {}).get("free_bytes")
    return None if psram is None else {"psram": psram, "internal": internal or 0}


def stable_memory(robot, timeout=30):
    """等设备上报的空闲内存稳定下来再读数，避开打开、关闭瞬间的临时占用。"""
    deadline = time.monotonic() + timeout
    last, since, seen = free_memory(robot), time.monotonic(), []
    while time.monotonic() < deadline:
        time.sleep(1)
        current = free_memory(robot)
        if current is not None:
            seen.append(current)
        if current != last:
            last, since = current, time.monotonic()
        elif current is not None and time.monotonic() - since >= STABLE_SECONDS:
            return current
    if not seen:
        raise SystemExit("设备没有上报内存（robot.resource_snapshot 为空），无法继续。")
    # 读数一直有小幅抖动时，取最近空闲最多的一次：临时占用只会让读数变小。
    return max(seen[-10:], key=lambda item: item["psram"])


def use_microphone(robot, seconds, samples=None):
    """打开麦克风，读 seconds 秒音频，然后关闭。返回收到的 PCM 字节数。"""
    received = 0
    next_sample = time.monotonic() + 5
    with robot.microphone.open_pcm() as microphone:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                received += len(microphone.read(timeout=0.2).data)
            except TimeoutError:
                pass
            if samples is not None and time.monotonic() >= next_sample:
                current = free_memory(robot)
                if current is not None:
                    samples.append(current["psram"])
                next_sample += 10
    return received


def reproduce(robot):
    from watcherobot.errors import WatcheRobotError

    say(f"固件版本：{robot.device_info.get('firmware_version', '未知')}")
    try:
        # 设备默认的待机动画会反复申请和释放内存，先换成静态的程序化表情再量。
        robot.expression_runtime.start("standby", auto_blink=False)
    except (WatcheRobotError, TimeoutError) as error:
        say(f"提示：没能切换到静态画面（{error}），内存读数可能有抖动。")
    say("等设备内存稳定……")
    baseline = stable_memory(robot)
    say(f"开始时空闲内存：PSRAM {baseline['psram']:,} 字节，internal {baseline['internal']:,} 字节")
    say()

    time.sleep(15)
    idle = stable_memory(robot)
    say(f"[1] 空闲 15 秒：PSRAM 减少 {baseline['psram'] - idle['psram']:,} 字节")

    for _ in range(CYCLES):
        use_microphone(robot, 0.3)
    first = stable_memory(robot)
    first_loss = idle["psram"] - first["psram"]
    say(f"[2] 麦克风打开再关闭 {CYCLES} 次：PSRAM 减少 {first_loss:,} 字节，"
        f"平均每次 {first_loss / CYCLES:,.0f}；internal 减少 {idle['internal'] - first['internal']:,} 字节")

    samples = []
    received = use_microphone(robot, HOLD_SECONDS, samples)
    held = stable_memory(robot)
    drift = max(samples) - min(samples) if samples else 0
    say(f"[3] 麦克风只打开一次并保持 {HOLD_SECONDS} 秒（收到 {received / 1_000_000:.1f} MB 音频）："
        f"打开期间空闲内存最大变化 {drift:,} 字节；关闭后比打开前减少 {first['psram'] - held['psram']:,} 字节")

    for _ in range(CYCLES):
        use_microphone(robot, 0.3)
    second = stable_memory(robot)
    second_loss = held["psram"] - second["psram"]
    say(f"[4] 再打开再关闭 {CYCLES} 次：PSRAM 减少 {second_loss:,} 字节，平均每次 {second_loss / CYCLES:,.0f}")

    time.sleep(15)
    final = stable_memory(robot)
    say(f"[5] 结束后再空闲 15 秒：PSRAM 恢复 {final['psram'] - second['psram']:,} 字节")
    say()

    per_cycle = (first_loss + second_loss) / (2 * CYCLES)
    total = baseline["psram"] - final["psram"]
    say(f"整个过程共开关麦克风 {2 * CYCLES + 1} 次，PSRAM 空闲内存从 {baseline['psram']:,} 降到 "
        f"{final['psram']:,}，少了 {total:,} 字节。")
    if per_cycle >= LEAK_THRESHOLD:
        say(f"结论：已复现。每开关一次麦克风，空闲内存减少约 {per_cycle:,.0f} 字节，空闲后不恢复；"
            f"按当前剩余内存还能再开关约 {final['psram'] // max(int(per_cycle), 1)} 次。")
    else:
        say(f"结论：没有复现。平均每次开关减少 {per_cycle:,.0f} 字节。")


async def run_inside_application():
    from watcherobot.application import ApplicationContext

    async with ApplicationContext.from_environment() as app:
        # SDK 的设备指令是阻塞调用，放到工作线程里顺序执行。
        await asyncio.to_thread(reproduce, app.robot)


def launch():
    """生成临时 Application（app.json 加本文件），通过 SDK Daemon 运行，并把输出转到当前终端。"""
    folder = Path(tempfile.mkdtemp(prefix="mic_leak_repro_"))
    try:
        (folder / "app.json").write_text(json.dumps(MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.copyfile(Path(__file__).resolve(), folder / "app.py")
        process = subprocess.Popen(
            [sys.executable, "-c", "from watcherobot.cli import main; raise SystemExit(main())",
             "app", "run", str(folder)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        for line in process.stdout:
            line = line.rstrip("\n")
            for prefix in ("Application stdout: ", "Application stderr: "):
                line = line.removeprefix(prefix)
            say(line)
        return process.wait()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    # Daemon 启动 Application 时会带上 WATCHER_APP_ID；直接执行本文件时没有它，先把自己交给 Daemon。
    if os.environ.get("WATCHER_APP_ID"):
        asyncio.run(run_inside_application())
    else:
        sys.exit(launch())
