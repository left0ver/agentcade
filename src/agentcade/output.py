import asyncio
import logging

from watcherobot.errors import CommandError

from agentcade.audio_stream import PcmAudioPlayer, sdk_command
from agentcade.expressions import expression_vector
from agentcade.eyes import HIDDEN_EYES, STANDBY_EYES
from agentcade.progress import ProgressDisplay
from agentcade.protocol import Presentation
from agentcade.speech import QwenSpeech
from observability.events import bus, describe, failure, scope

logger = logging.getLogger(__name__)

SYNTHESIS_ATTEMPTS = 3
EXPRESSIONS = {"happy": "009", "sorry": "141"}
# 表情和结束时的图案留到接下来这句话说完；话一直没有来时，到这个时间自己恢复。
RESTORE_AFTER_SECONDS = 20
# 动作做到一半时开始播放，设备会把动作取消，所以开口前先等动作做完，最多等这么久。
GESTURE_WAIT_SECONDS = 4


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
        self.motion_lock = asyncio.Lock()
        self.moving: set[asyncio.Task] = set()
        # 游戏正在展示的图案（最后一帧），没有图案时为 None，对应待机画面。
        self.canvas: str | None = None
        self.dirty = False
        self.pending_restore = False
        self.restore_timer: asyncio.Task | None = None

    async def execute(self, output):
        async with self.output_lock:
            try:
                performed = await self.present(output)
            except BaseException as error:
                bus.emit("device.output", {"output": output, **failure(error)})
                raise
            else:
                # 留到下一句话说完才换的画面，到真正恢复的时候再记录。
                if performed is not False:
                    record = {"output": output, "completed": True}
                    if output.expression and not output.text:
                        # 带上屏幕实际显示的表情图案，页面用它画出机器人当时的样子。
                        record["face"] = expression_vector(EXPRESSIONS[output.expression])
                    bus.emit("device.output", record)

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
        self.dirty = False

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

    async def synthesize(self, text):
        for attempt in range(1, SYNTHESIS_ATTEMPTS + 1):
            try:
                return await self.speech.synthesize(text)
            except ValueError:
                raise
            except Exception as error:
                # 语音服务偶尔连接失败或超时，重试仍然失败再放弃这一条。
                if attempt == SYNTHESIS_ATTEMPTS:
                    raise
                logger.warning("语音合成失败，重试（%s/%s）：%s",
                               attempt, SYNTHESIS_ATTEMPTS - 1, describe(error))
                await asyncio.sleep(0.5 * attempt)

    async def present(self, output):
        if not output.text and (output.expression or output.gesture):
            await self.react(output)
            return
        if output.clear:
            return await self.leave()
        if output.paths:
            await self.progress.finish()
            self.release_hold()
            await self.show_vector(output.paths[0])
            for path in output.paths[1:]:
                await asyncio.sleep(0.16)
                await self.update_vector(path)
            self.canvas, self.dirty = output.paths[-1], True
        elif output.progress_seconds:
            await self.stop_progress()
            self.release_hold()
            self.canvas, self.dirty = None, True
        self.speaking = True
        await self.input.pause(owner=self)
        progress_token = None
        try:
            audio = await self.synthesize(output.text) if output.text else None
            if output.progress_seconds:
                progress_token = await self.progress.start()
            if output.text:
                await self.settle()
                self.last_text = output.text
                self.app.logger.info("机器人：%s", output.text)
                metrics = await self.audio.play(audio, on_start=lambda seconds: self.started(output, seconds))
                self.app.logger.info(
                    "语音播放：任务 %s，启动 %.2f 秒，完成 %.2f 秒，PCM %s 字节",
                    metrics.playback_id, metrics.started_seconds, metrics.completed_seconds,
                    metrics.pcm_bytes,
                )
            if progress_token is not None:
                self.progress.advance(progress_token, output.progress_seconds)
        finally:
            self.speaking = False
            self.input.resume(owner=self)
        if output.text:
            await self.restore()

    async def started(self, output, seconds):
        # 对话页面按这段语音的时长把文字逐步显示出来，和机器人说话的进度一致。
        bus.emit("speech.start", {"text": output.text, "duration_ms": round(seconds * 1000)})

    async def react(self, output):
        """对用户回答的即时反应：立刻显示表情、开始动作，不等动作做完；表情留到接下来这句话说完。"""
        if output.expression:
            await self.stop_progress()
            await self.show_vector(expression_vector(EXPRESSIONS[output.expression]))
            self.dirty = True
            self.hold()
        if output.gesture:
            task = asyncio.create_task(self.move(output.gesture))
            self.moving.add(task)
            task.add_done_callback(self.moving.discard)

    async def move(self, name):
        owner = object()
        await self.input.pause(owner=owner)
        try:
            async with self.motion_lock:
                await self.gesture(name)
        except Exception as error:
            # 动作只是点缀，没有做完不影响后面的播报。
            logger.warning("动作 %s 没有完成：%s", name, describe(error), exc_info=True)
        finally:
            self.input.resume(owner=owner)

    async def settle(self):
        """等正在做的动作做完。"""
        if self.moving:
            await asyncio.wait(set(self.moving), timeout=GESTURE_WAIT_SECONDS)

    async def leave(self):
        """游戏结束，屏幕回到待机画面。屏幕上有图案或表情时留到接下来这句话说完再换。

        已经换好画面返回 True，留到之后再换或者本来就是待机画面返回 False。
        """
        self.canvas = None
        if self.progress.active:
            await self.stop_progress()
            self.release_hold()
            await self.clear()
            return True
        if self.dirty:
            self.hold()
        return False

    def hold(self):
        self.pending_restore = True
        if self.restore_timer is None or self.restore_timer.done():
            self.restore_timer = asyncio.create_task(self.restore_later())

    def release_hold(self):
        self.pending_restore = False
        timer, self.restore_timer = self.restore_timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def restore_later(self):
        await asyncio.sleep(RESTORE_AFTER_SECONDS)
        async with self.output_lock:
            await self.restore()

    async def restore(self):
        """把屏幕恢复成游戏当前的图案，没有图案时恢复待机画面。"""
        if not self.pending_restore:
            return
        self.release_hold()
        try:
            if self.canvas:
                await self.show_vector(self.canvas)
                shown = Presentation(paths=[self.canvas])
            else:
                await self.clear()
                shown = Presentation(clear=True)
        except Exception as error:
            # 画面没有恢复只影响显示，不影响已经说完的这句话。
            logger.warning("恢复画面失败：%s", describe(error), exc_info=True)
            return
        bus.emit("device.output", {"output": shown, "completed": True})

    async def run(self, agent):
        while True:
            request = await agent.outputs.get()
            try:
                completed = []
                with scope(**request.trace_context):
                    for output in request.outputs:
                        try:
                            await self.execute(output)
                        except Exception as error:
                            # 一条输出失败只跳过这一条，后面的播报照常进行。
                            logger.warning("输出失败，已跳过“%s”：%s",
                                           output.text[:30], describe(error), exc_info=True)
                            continue
                        completed.append(output)
                request.outputs = completed
                await agent.output_done(request)
            finally:
                if not request.completed.done():
                    request.completed.cancel()
                agent.outputs.task_done()

    async def close(self):
        moving = list(self.moving)
        for task in moving:
            task.cancel()
        await asyncio.gather(*moving, return_exceptions=True)
        await self.stop_progress()
        # 退出后不会再有话，屏幕上还留着游戏画面时直接恢复待机。
        self.canvas = None
        await self.restore()
