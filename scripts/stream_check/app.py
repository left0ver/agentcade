import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from watcherobot.application import ApplicationContext

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from guess_animal.audio_stream import RtcAudioPlayer
from guess_animal.config import ROOT, Settings
from guess_animal.speech import QwenSpeech


async def main() -> None:
    settings = Settings.from_environment()
    speech = QwenSpeech(settings)
    report = {"complete": False, "playbacks": []}
    output = ROOT / ".artifacts" / "stream_check.json"
    output.parent.mkdir(exist_ok=True)
    async with ApplicationContext.from_environment() as app:
        player = RtcAudioPlayer(app.rtc)
        try:
            for index, text in enumerate((
                "现在开始流式语音测试。音频生成一部分，就会播放一部分。它生活在海洋里，它喜欢跃出水面。",
                "答对了！",
            )):
                metrics = await player.play(speech.synthesize_stream(text))
                if not metrics.source_pcm_peak or not metrics.device_i2s_bytes:
                    raise AssertionError("语音输入或设备输出无效")
                if index == 0 and not metrics.device_pcm_peak:
                    raise AssertionError("长句播放期间没有收到有效语音的设备统计")
                if metrics.first_frame_seconds >= metrics.input_finished_seconds:
                    raise AssertionError("音频没有在输入结束之前开始发送")
                if (
                    metrics.first_device_audio_seconds is not None
                    and metrics.first_device_audio_seconds < metrics.first_frame_seconds
                ):
                    raise AssertionError("设备音频统计时间早于当前播放")
                report["playbacks"].append({"text": text, **asdict(metrics)})
                app.logger.info("真实流式播放完成：%s", asdict(metrics))
                if app.rtc.snapshot()["active"]:
                    raise AssertionError("播放完成后必须释放 RTC 会话")

            started = asyncio.Event()

            async def on_start() -> None:
                started.set()

            async with asyncio.TaskGroup() as group:
                playback = group.create_task(player.play(
                    speech.synthesize_stream("现在检查中途取消。这段语音将会在播放期间被停止。"),
                    on_start=on_start,
                ))
                await started.wait()
                await asyncio.sleep(1)
                cancelled_at = time.monotonic()
                playback.cancel()
            if not playback.cancelled() or app.rtc.snapshot()["active"]:
                raise AssertionError("取消播放没有释放 RTC 会话")
            report["cancel_seconds"] = time.monotonic() - cancelled_at

            metrics = await player.play(speech.synthesize_stream("取消完成，正在检查麦克风。"))
            report["after_cancel"] = asdict(metrics)
            microphone = await asyncio.to_thread(app.robot.microphone.open_pcm)
            try:
                pcm_bytes = 0
                for _ in range(30):
                    frame = await asyncio.to_thread(microphone.read, timeout=2)
                    pcm_bytes += len(frame.data)
                if not pcm_bytes or microphone.dropped_frames or microphone.decode_failures:
                    raise AssertionError("流式播放后麦克风未正常恢复")
                report["microphone_bytes"] = pcm_bytes
            finally:
                await asyncio.to_thread(microphone.close)
            report["complete"] = True
        finally:
            report["last_device_stats"] = app.rtc.snapshot()["stats"]
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
