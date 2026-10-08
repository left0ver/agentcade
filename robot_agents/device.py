import asyncio
import os
import time
from collections import deque

from watcherobot.application import ApplicationContext
from watcherobot.application.context import ApplicationEnvironmentError
from watcherobot.application.transport import DaemonApplicationTransport
from watcherobot.errors import WatcheRobotError


class DeviceTransport(DaemonApplicationTransport):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.device_ready = asyncio.Event()
        self.audio_sender_lock = asyncio.Lock()
        self.events = deque(maxlen=128)
        self.add_message_listener(self.observe_ready)

    def observe_ready(self, message):
        if message["type"] in {
            "evt.audio.buffer_status", "evt.microphone.state", "evt.sdk.ready",
            "evt.sdk.resource_snapshot", "sys.nack",
        }:
            self.events.append({"time": time.monotonic(), **message})
        if message["type"] == "evt.sdk.ready":
            self.device_ready.set()

    def start(self, *, timeout=15):
        super().start(timeout=timeout)

    async def _on_channels_connected(self):
        self.device_ready.clear()
        async with asyncio.timeout(self.command_timeout):
            await self._send_command("sys.sdk.ready.get", {}, self.command_timeout)
            await self.device_ready.wait()
        self._started_event.set()

    async def _send_audio_stream(self, pcm, stream_id, chunk_bytes):
        # 上一段发送任务完成取消清理后，下一段才可以修改共享流控状态。
        async with self.audio_sender_lock:
            await super()._send_audio_stream(pcm, stream_id, chunk_bytes)

    async def _take_audio_credit(self, stream_id):
        condition = self._audio_credit_condition
        if condition is None:
            raise WatcheRobotError("audio flow control is not initialized")
        # 在发送任务内部等待，取消时同时结束 Condition 等待。
        async with asyncio.timeout(self.command_timeout), condition:
            await condition.wait_for(
                lambda: self._audio_flow_stream_id != stream_id
                or self._audio_credits > 0 or self._audio_flow_error is not None
            )
            if self._audio_flow_stream_id != stream_id:
                raise WatcheRobotError("audio stream was replaced")
            if self._audio_flow_error is not None:
                raise WatcheRobotError(f"audio stream failed: {self._audio_flow_error}")
            self._audio_credits -= 1


class DeviceContext(ApplicationContext):
    @classmethod
    def from_environment(cls):
        required = (
            "WATCHER_APP_ID", "WATCHER_APP_RUN_CREDENTIAL",
            "WATCHER_APP_DESKTOP_WS_URL", "WATCHER_APP_DEVICE_WS_URL",
        )
        environment = {name: os.environ.get(name, "").strip() for name in required}
        missing = [name for name, value in environment.items() if not value]
        if missing:
            raise ApplicationEnvironmentError(
                "Application environment is incomplete: " + ", ".join(sorted(missing))
            )
        return cls(app_id=environment["WATCHER_APP_ID"], transport=DeviceTransport())
