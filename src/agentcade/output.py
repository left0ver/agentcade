import asyncio
import logging

from watcherobot.errors import CommandError
from watcherobot.job import Job

from agentcade.audio_stream import PcmAudioPlayer, sdk_command
from agentcade.eyes import STANDBY_EYES
from agentcade.robot_calls import describe_call
from agentcade.speech import QwenSpeech
from observability.events import bus, describe, failure, scope

logger = logging.getLogger(__name__)

SYNTHESIS_ATTEMPTS = 3
# 游戏的 SDK 调用返回 Job（动画、动作、音效）时，等它做完再执行下一条，最多等这么久；
# 超时就先执行后面的，Job 在机器人上继续。灯效可以一直循环，不等它。
JOB_WAIT_SECONDS = 10
NO_WAIT = {"lights"}
# 这些模块的调用会发出声音或者转动电机，执行时暂停收音；麦克风这时重新打开，机器人还会取消正在做的动作。
NOISY = {"motion", "audio", "behavior"}
# 连续几次有声音或动作的调用之间不恢复收音，免得反复开关麦克风。
HUSH_GRACE_SECONDS = 0.5
# 这些模块会改变屏幕，一局结束后屏幕要回到待机。
SCREEN = {"expression_runtime", "expressions", "animation", "behavior", "works"}
# 头部的正前方（水平角, 俯仰角），一局结束后转回这里。
NEUTRAL = (90, 115)


class Hush:
    """游戏做动作、放音效时暂停收音；连续的几次调用合成一次暂停。"""

    def __init__(self, microphone):
        self.input = microphone
        self.held = False
        self.timer: asyncio.Task | None = None

    async def hold(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if not self.held:
            await self.input.pause(owner=self)
            self.held = True

    def release(self):
        if self.held and self.timer is None:
            self.timer = asyncio.create_task(self.release_later())

    async def release_later(self):
        await asyncio.sleep(HUSH_GRACE_SECONDS)
        self.timer = None
        self.resume()

    def resume(self):
        if self.held:
            self.held = False
            self.input.resume(owner=self)

    def close(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.resume()


class OutputCoordinator:
    """按顺序执行主 Agent 的输出：说话、游戏的 SDK 调用，以及一局结束后的复位。"""

    def __init__(self, app, settings, microphone):
        self.app, self.robot, self.input = app, app.robot, microphone
        self.speech = QwenSpeech(settings)
        self.last_text = ""
        self.audio = PcmAudioPlayer(self.robot.audio)
        self.speaking = False
        self.output_lock = asyncio.Lock()
        self.hush = Hush(microphone)
        # 这一局游戏调用过的 SDK 模块，结束时只复位这些。
        self.touched: set[str] = set()

    async def execute(self, output):
        async with self.output_lock:
            try:
                performed = await self.present(output)
            except BaseException as error:
                bus.emit("device.output", {"output": output, **failure(error)})
                raise
            else:
                if performed is not False:
                    bus.emit("device.output", {"output": output, "completed": True})

    async def clear(self):
        """屏幕回到待机的眼睛。"""
        await sdk_command(self.robot.expression_runtime.start, "standby", **STANDBY_EYES)

    async def perform(self, call):
        """执行游戏的一次 SDK 调用。参数由 SDK 自己检查；返回 Job 的等它做完。"""
        domain, method = call.call.split(".")
        function = getattr(getattr(self.robot, domain), method)
        self.touched.add(domain)
        noisy = domain in NOISY
        if noisy:
            await self.hush.hold()
        try:
            for attempt in range(3):
                try:
                    result = await sdk_command(function, *call.args, **call.kwargs)
                    break
                except CommandError as error:
                    # 固件偶尔对连续的表情更新返回 invalid_argument，稍等重试。
                    if (error.message_type != "ctrl.expression.runtime.update"
                            or error.reason != "invalid_argument" or attempt == 2):
                        raise
                    logger.warning("表情更新返回 invalid_argument，0.2 秒后重试（%s/2）", attempt + 1)
                    await asyncio.sleep(0.2)
            if isinstance(result, Job) and domain not in NO_WAIT:
                try:
                    await sdk_command(result.wait, JOB_WAIT_SECONDS)
                except TimeoutError:
                    logger.info("%s 超过 %s 秒还没做完，先执行后面的", call.call, JOB_WAIT_SECONDS)
        finally:
            if noisy:
                self.hush.release()

    async def reset(self):
        """一局结束后把游戏用过的设备复位：停掉动画和行为、关灯、头转回正前方、屏幕回到待机。

        游戏没有调用过 SDK 时什么也不做，返回 False。
        """
        touched, self.touched = self.touched, set()
        if not touched:
            return False
        steps = []
        if "animation" in touched:
            steps.append(("停止动画", self.robot.animation.stop, {}, False))
        if "behavior" in touched:
            steps.append(("停止行为", self.robot.behavior.stop, {}, False))
        if "lights" in touched:
            steps.append(("关灯", self.robot.lights.off, {}, False))
        if "motion" in touched:
            pan, tilt = NEUTRAL
            steps.append(("头转回正前方", self.robot.motion.move_to,
                          {"pan_deg": pan, "tilt_deg": tilt, "duration_ms": 400}, True))
        # 复位紧跟在最后一句话后面，说完收音会重新打开，转头时要暂停收音。
        for name, function, arguments, noisy in steps:
            if noisy:
                await self.hush.hold()
            try:
                result = await sdk_command(function, **arguments)
                if isinstance(result, Job):
                    await sdk_command(result.wait, JOB_WAIT_SECONDS)
            except Exception as error:
                # 复位做不完只影响外观，接着复位其他的。
                logger.warning("一局结束后%s失败：%s", name, describe(error), exc_info=True)
            finally:
                if noisy:
                    self.hush.release()
        if touched & SCREEN:
            await self.clear()

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
        if output.call is not None:
            return await self.perform(output.call)
        if output.reset:
            return await self.reset()
        if not output.text:
            return False
        self.speaking = True
        await self.input.pause(owner=self)
        try:
            audio = await self.synthesize(output.text)
            self.last_text = output.text
            self.app.logger.info("机器人：%s", output.text)
            metrics = await self.audio.play(audio, on_start=lambda seconds: self.started(output, seconds))
            self.app.logger.info(
                "语音播放：任务 %s，启动 %.2f 秒，完成 %.2f 秒，PCM %s 字节",
                metrics.playback_id, metrics.started_seconds, metrics.completed_seconds,
                metrics.pcm_bytes,
            )
        finally:
            self.speaking = False
            self.input.resume(owner=self)

    async def started(self, output, seconds):
        # 对话页面按这段语音的时长把文字逐步显示出来，和机器人说话的进度一致。
        bus.emit("speech.start", {"text": output.text, "duration_ms": round(seconds * 1000)})

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
                            label = (output.text[:30] if output.text
                                     else describe_call(output.call) if output.call else "复位")
                            logger.warning("输出失败，已跳过“%s”：%s", label, describe(error), exc_info=True)
                            continue
                        completed.append(output)
                request.outputs = completed
                await agent.output_done(request)
            finally:
                if not request.completed.done():
                    request.completed.cancel()
                agent.outputs.task_done()

    async def close(self):
        self.hush.close()
        # 退出时还在玩的那一局不会再有结束的回复，直接复位。
        async with self.output_lock:
            await self.reset()
