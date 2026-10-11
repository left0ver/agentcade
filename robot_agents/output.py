import asyncio

from watcherobot.errors import CommandError

from observability.events import bus, failure, scope
from robot_agents.audio_stream import PcmAudioPlayer, sdk_command
from robot_agents.expressions import expression_vector
from robot_agents.eyes import HIDDEN_EYES, STANDBY_EYES
from robot_agents.progress import ProgressDisplay
from robot_agents.speech import QwenSpeech


class OutputCoordinator:
    def __init__(self, app, settings, microphone):
        self.app, self.robot, self.input = app, app.robot, microphone
        self.speech = QwenSpeech(settings)
        self.last_text = ""
        self.audio = PcmAudioPlayer(self.robot.audio)
        self.display_lock = asyncio.Lock()
        self.progress = ProgressDisplay(self._show_vector, self.display_lock)
        self.speaking = False
        self.output_lock = asyncio.Lock()

    async def execute(self, output):
        async with self.output_lock:
            try:
                await self.present(output)
            except BaseException as error:
                bus.emit("device.output", {"output": output, **failure(error)})
                raise
            else:
                bus.emit("device.output", {"output": output, "completed": True})

    async def show_vector(self, path):
        async with self.display_lock:
            await self._show_vector(path)

    async def _show_vector(self, path):
        await sdk_command(
            self.robot.expression_runtime.start, "standby", **HIDDEN_EYES,
            tag="none", custom_vector_path=path,
        )

    async def clear(self):
        async with self.display_lock:
            await sdk_command(self.robot.expression_runtime.start, "standby", **STANDBY_EYES)

    async def update_vector(self, path):
        async with self.display_lock:
            for attempt in range(3):
                try:
                    await sdk_command(
                        self.robot.expression_runtime.update, custom_vector_path=path,
                    )
                    return
                except CommandError as error:
                    if (error.message_type != "ctrl.expression.runtime.update"
                            or error.reason != "invalid_argument" or attempt == 2):
                        raise
                    self.app.logger.warning(
                        "表情更新返回 invalid_argument，0.2 秒后重试（%s/2）", attempt + 1,
                    )
                    await asyncio.sleep(0.2)

    async def gesture(self, name):
        positions = {
            "nod": ((90, 115), (90, 105), (90, 115)),
            "shake_head": ((90, 115), (105, 115), (75, 115), (90, 115)),
        }[name]
        completed = False
        try:
            for pan, tilt in positions:
                job = await sdk_command(
                    self.robot.motion.move_to, pan_deg=pan, tilt_deg=tilt,
                    duration_ms=350, profile="ease_in_out",
                )
                await sdk_command(job.wait, 3.0)
            completed = True
        finally:
            if not completed:
                await sdk_command(self.robot.motion.stop)

    async def stop_progress(self):
        await self.progress.cancel()

    async def present(self, output):
        if output.paths and not output.clear:
            await self.progress.finish()
        elif output.clear or output.expression or output.progress_seconds:
            await self.stop_progress()
        if output.clear:
            await self.clear()
        if output.paths:
            await self.show_vector(output.paths[0])
            for path in output.paths[1:]:
                await asyncio.sleep(0.16)
                await self.update_vector(path)
        self.speaking = True
        await self.input.pause(owner=self)
        progress_token = None
        try:
            audio = await self.speech.synthesize(output.text) if output.text else None
            if output.progress_seconds:
                progress_token = await self.progress.start()
            elif output.expression:
                await self.show_vector(expression_vector({
                    "happy": "009", "sorry": "141", "thinking": "111",
                }[output.expression]))
            async with asyncio.TaskGroup() as group:
                async def on_start():
                    if output.gesture:
                        group.create_task(self.gesture(output.gesture))

                if output.text:
                    self.last_text = output.text
                    self.app.logger.info("机器人：%s", output.text)
                    metrics = await self.audio.play(
                        audio, on_start=on_start,
                    )
                    self.app.logger.info(
                        "语音播放：任务 %s，启动 %.2f 秒，完成 %.2f 秒，PCM %s 字节",
                        metrics.playback_id, metrics.started_seconds, metrics.completed_seconds,
                        metrics.pcm_bytes,
                    )
                else:
                    await on_start()
            if progress_token is not None:
                self.progress.advance(progress_token, output.progress_seconds)
        finally:
            self.speaking = False
            self.input.resume(owner=self)

    async def run(self, agent):
        while True:
            request = await agent.outputs.get()
            try:
                with scope(**request.trace_context):
                    for output in request.outputs:
                        await self.execute(output)
                await agent.output_done(request)
            finally:
                if not request.completed.done():
                    request.completed.cancel()
                agent.outputs.task_done()

    async def close(self):
        await self.stop_progress()
