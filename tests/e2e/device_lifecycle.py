import asyncio
from contextlib import aclosing

from robot_agents.audio_stream import PcmAudioPlayer
from robot_agents.microphone import MicrophoneInput
from robot_agents.speech import QwenSpeech


async def check_audio_transitions(app, settings, report):
    microphone = MicrophoneInput(app.robot)
    player = PcmAudioPlayer(app.robot.audio)
    speech = QwenSpeech(settings)
    long_pcm = await speech.synthesize("这段语音用于检查播放取消之后的资源释放和麦克风恢复。")
    short_pcm = await speech.synthesize("播放已经恢复。")
    report["audio_transitions"] = []

    async def consume():
        async with aclosing(microphone.audio()) as frames:
            async for _ in frames:
                pass

    try:
        async with asyncio.timeout(180), asyncio.TaskGroup() as group:
            capturing = group.create_task(microphone.capture())
            consuming = group.create_task(consume())
            for index in range(12):
                await microphone.receiving.wait()
                await microphone.pause(owner=player)
                started = asyncio.Event()

                async def on_start(started=started):
                    started.set()

                async with asyncio.TaskGroup() as playback_group:
                    playing = playback_group.create_task(player.play(long_pcm, on_start=on_start))
                    await started.wait()
                    await asyncio.sleep((0.05, 0.3, 1)[index % 3])
                    playing.cancel()
                assert playing.cancelled() and not player.playing
                completed = await player.play(short_pcm)
                microphone.resume(owner=player)
                await microphone.receiving.wait()
                report["audio_transitions"].append({
                    "iteration": index + 1, "playback_id": completed.playback_id,
                    "microphone_bytes": microphone.received_pcm_bytes,
                })
            capturing.cancel()
            consuming.cancel()
    finally:
        report["transition_microphone_sessions"] = list(microphone.sessions)
