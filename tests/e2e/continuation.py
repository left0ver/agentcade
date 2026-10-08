import asyncio
import time

from a2a.utils.errors import InternalError
from typesafe_sdk import TypeSafeAPIConnectionError, TypeSafeAPIError
from watcherobot.errors import CommandError

from robot_agents.main_agent import MainAgent


class ContinuingMainAgent(MainAgent):
    def __init__(self, settings, games, report):
        super().__init__(settings, games)
        self.report = report

    async def receive_utterance(self, text, context_id, revision):
        for attempt in range(1, 4):
            before = self.status()
            try:
                return await super().receive_utterance(text, context_id, revision)
            except (TypeSafeAPIError, TypeSafeAPIConnectionError, InternalError) as error:
                retryable = (
                    isinstance(error, TypeSafeAPIConnectionError)
                    or isinstance(error, TypeSafeAPIError) and error.status in {429, 500, 502, 503, 504, 529}
                    or isinstance(error, InternalError) and "api.typesafe.ai" in str(error)
                    and any(code in str(error) for code in ("429", "500", "502", "503", "504", "529"))
                )
                unchanged = self.status() == before and self.outputs.empty()
                retry = retryable and unchanged and attempt < 3
                self.report["issues"].append({
                    "stage": "receive_utterance", "text": text, "attempt": attempt,
                    "error": str(error), "retry": retry, "time": time.monotonic(),
                })
                if not retry:
                    raise
                await asyncio.sleep(attempt * 2)


class ContinuationOutputMixin:
    async def device_operation(self, name, operation, *args):
        try:
            return await operation(*args)
        except CommandError as error:
            if error.reason != "executor_failed":
                raise
            self.report["issues"].append({
                "stage": name, "error": str(error), "time": time.monotonic(),
            })
            self.app.logger.error("E2E 记录设备错误并继续流程：%s：%s", name, error)

    async def clear(self):
        await self.device_operation("clear", super().clear)

    async def _show_vector(self, path):
        await self.device_operation("show_vector", super()._show_vector, path)

    async def update_vector(self, path):
        try:
            await self.device_operation("update_vector", super().update_vector, path)
        except CommandError as error:
            if error.reason != "invalid_argument":
                raise
            self.report["issues"].append({
                "stage": "update_vector", "error": str(error),
                "recovery": "重新提交当前完整图案", "time": time.monotonic(),
            })
            self.app.logger.error("E2E 图案更新失败，重新提交当前完整图案：%s", error)
            await self._show_vector(path)

    async def gesture(self, name):
        await self.device_operation("gesture", super().gesture, name)
