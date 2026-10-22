"""游戏可以调用的机器人 SDK 方法：白名单、参数检查，以及游戏里的 self.robot。

游戏不直接连机器人：self.robot 的写法和 SDK 一样，每次调用作为设备指令发给主 Agent，
主 Agent 检查后在机器人上执行同名方法。
"""
import inspect
import json
from collections.abc import Awaitable, Callable

from watcherobot import robot as sdk

from agentcade.protocol import SdkCall

# 开放给游戏的 SDK 方法和需要在 SKILL.md 里声明的能力（SDK 的能力名）。
# 麦克风、摄像头、录像、视觉和人脸跟踪涉及隐私，说话用的音频通道和删除作品会影响主 Agent 和用户的数据，都不开放。
SDK_CALLS = {
    "lights.set_color": "light",
    "lights.play_effect": "light",
    "lights.off": "light",
    "animation.play": "animation",
    "animation.prefetch": "animation",
    "animation.stop": "animation",
    "behavior.play": "behavior",
    "behavior.stop": "behavior",
    "motion.move_to": "motion",
    "motion.set_target": "motion",
    "motion.play_action": "motion",
    "motion.stop": "motion",
    "expression_runtime.start": "expression.runtime.v3",
    "expression_runtime.update": "expression.runtime.v3",
    "expression_runtime.stop": "expression.runtime.v3",
    "expressions.play_official": "resource.expression.official",
    "audio.play": "audio",
    "works.play": "resource.work.play",
    "works.play_expression": "resource.work.expression.play",
}

# 模块名对应的 SDK 类，用来按 SDK 的签名检查参数。
DOMAINS = {
    "lights": sdk.LightsDomain,
    "animation": sdk.AnimationDomain,
    "behavior": sdk.BehaviorDomain,
    "motion": sdk.MotionDomain,
    "expression_runtime": sdk.ExpressionRuntimeDomain,
    "expressions": sdk.ExpressionDomain,
    "audio": sdk.AudioDomain,
    "works": sdk.WorkDomain,
}


def problem(call: SdkCall) -> str | None:
    """调用不在白名单里或者参数和 SDK 的签名对不上时返回原因，没问题返回 None。"""
    if call.call not in SDK_CALLS:
        return f"游戏不能调用 robot.{call.call}"
    domain, method = call.call.split(".")
    try:
        inspect.signature(getattr(DOMAINS[domain], method)).bind(None, *call.args, **call.kwargs)
    except TypeError as error:
        return f"robot.{call.call} 的参数不对：{error}"
    return None


def brief(value) -> str:
    """把参数写短：很长的字符串（例如矢量图案）只显示长度。"""
    if isinstance(value, str) and len(value) > 40:
        return f"<{len(value)} 字>"
    return json.dumps(value, ensure_ascii=False)


def describe_call(call: SdkCall, limit: int | None = None) -> str:
    """写成 SDK 的调用形式。limit 限制显示的参数个数，长字符串（例如矢量图案）排在前面。"""
    arguments = [brief(value) for value in call.args]
    named = sorted(call.kwargs.items(), key=lambda item: not (isinstance(item[1], str) and len(item[1]) > 40))
    arguments += [f"{name}={brief(value)}" for name, value in named]
    if limit is not None and len(arguments) > limit:
        arguments = [*arguments[:limit], f"… 共 {len(arguments)} 个参数"]
    return f"{call.call}({', '.join(arguments)})"


class RobotProxy:
    """游戏里的 self.robot：写法和机器人 SDK 一样，例如 await self.robot.lights.set_color("#00FF00")。

    调用不等机器人执行完，也没有返回值；开放哪些方法见 SDK_CALLS。
    """

    def __init__(self, send: Callable[[SdkCall], Awaitable[None]]):
        self._send = send

    def __getattr__(self, domain: str) -> "DomainProxy":
        if domain not in DOMAINS:
            raise AttributeError(f"游戏不能调用 robot.{domain}，可以用的有：{'、'.join(DOMAINS)}")
        return DomainProxy(domain, self._send)


class DomainProxy:
    def __init__(self, domain: str, send: Callable[[SdkCall], Awaitable[None]]):
        self._domain, self._send = domain, send

    def __getattr__(self, method: str) -> Callable[..., Awaitable[None]]:
        name = f"{self._domain}.{method}"
        if name not in SDK_CALLS:
            allowed = [key.split(".")[1] for key in SDK_CALLS if key.startswith(f"{self._domain}.")]
            raise AttributeError(f"游戏不能调用 robot.{name}，可以用的有：{'、'.join(allowed)}")

        async def call(*args, **kwargs) -> None:
            request = SdkCall(call=name, args=list(args), kwargs=kwargs)
            reason = problem(request)
            if reason:
                raise TypeError(reason)
            await self._send(request)

        return call
