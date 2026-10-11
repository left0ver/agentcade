# 重构计划：主 Agent + Skill 驱动的游戏 Agent（A2A `desktop-game/5`）

## Context

现在的主 Agent 与游戏 Agent 之间用 `desktop-game/4` 通信：请求带 `action / revision / request_id`，结果带 `state / expected_input / reply`，游戏侧还有生命周期校验、请求去重缓存、过期输入处理。游戏流程写在 Python 的提示词常量里，主 Agent 需要理解游戏的阶段和期望输入。新增一个游戏要同时照顾这些机制，成本偏高。

这次重构把模型收敛成一句话：**主 Agent 发一条 A2A 消息（用户输入），游戏 Agent 回一条或多条消息（要播报的话）**。游戏相关的一切（规则、状态、流程、设备操作）都留在游戏 Agent 内部，流程由 `SKILL.md` 描述，游戏 Agent 是一个带工具的 ReAct Agent。

已确认的决定：

| 问题 | 决定 |
| --- | --- |
| 游戏 Agent 多发消息的方式 | 只在处理一条请求期间流式回复：至少 1 条，可以多条 |
| Skill 形态 | 每个游戏一个 `SKILL.md`，启动时全量注入系统提示词 |
| 设备操作 | 游戏 Agent 的工具直接执行（进程内注入设备执行函数），A2A 只传文本 |
| 测试 | 旧的单元测试和 E2E 全部删除，暂不补新测试 |

## 目标架构

```mermaid
sequenceDiagram
    participant U as 用户
    participant M as 主 Agent（A2A Client）
    participant G as 游戏 Agent（A2A Server，ReAct + SKILL.md）
    participant D as 机器人设备
    U->>M: 麦克风 → ASR → 原话
    Note over M: LLM 选择：直接回答 / list_games / start_game / end_game / send_message
    M->>G: SendMessage（流式）GameInput{kind, text}
    G->>M: 中间回复 GameReply（task=working）× 0..N
    M->>D: TTS → 播放
    G->>D: 游戏工具：画图 / 表情 / 点头
    G->>M: 最后一条 GameReply（task=completed）× 1
    M->>D: TTS → 播放
```

## 1. 统一通信格式 `desktop-game/5`

定义在 `robot_agents/protocol.py`，所有游戏 Agent 必须遵循；其余代码只接触 Pydantic 模型，protobuf 编解码集中在这个文件。

```python
class GameInput(Model):      # 主 Agent → 游戏 Agent：一条用户输入
    protocol: Literal["desktop-game/5"]
    kind: Literal["start", "message", "end"]   # 开局 / 局内输入 / 结束
    text: str = ""                              # 用户原话；程序退出时的 end 可以为空

class GameReply(Model):      # 游戏 Agent → 主 Agent：一条要播报的话
    protocol: Literal["desktop-game/5"]
    text: str                                   # 1 至 500 字，主 Agent 原样 TTS 播报
    session: Literal["active", "ended"] = "active"   # 本局是否继续，只有最后一条可以是 ended
```

| 方向 | A2A 载体 |
| --- | --- |
| 主 → 游戏 | 流式 `SendMessage`，`Message{role=user, context_id=一局游戏, message_id=请求 ID, parts=[DataPart(GameInput)]}` |
| 游戏 → 主，中间回复 | `TaskStatusUpdateEvent{state=working, status.message.parts=[DataPart(GameReply)]}`，0 至 N 条 |
| 游戏 → 主，最后一条 | `TaskStatusUpdateEvent{state=completed, status.message.parts=[DataPart(GameReply)]}`，恰好 1 条 |
| 出错 | Task 进入 failed，主 Agent 抛出异常（保持现在的直接报错风格） |

“是否最后一条”由 A2A Task 状态表达，不在 JSON 里重复。Agent Card 的 skill 带 `desktop-game/5` 和 `capability:*` 标签、`streaming=true`，由 `SKILL.md` 的 frontmatter 生成。

`GameDescriptor`、`Presentation` 保留；删除 `GameRequest`、`GameResult`、`PublicState`、`ExpectedInput`、`GameNotify`。

## 2. Skill 与游戏 Agent 运行时

**`games/<id>/SKILL.md`**（新增）：frontmatter 是游戏声明，正文是流程。

```markdown
---
id: guess_number
name: 猜数字
aliases: [数字游戏, 猜数]
description: 我会选一个1到20的整数，你来猜，最多有五次机会……
required_capabilities: [motion]
---
## 规则
……
## 流程
1. event=start：调用 start_game，告诉用户范围和机会数。
2. 用户明确猜一个整数：调用 submit_guess，按结果反馈……
5. event=end：调用 end_game，简短告别。
```

**`robot_agents/skill.py`**（新增）：`Skill.load(path)` 用 PyYAML 解析 frontmatter 得到 `GameDescriptor`，正文作为流程提示词。

**`robot_agents/skill_agent.py`**（新增，替代 `game_tools.py`）：所有游戏共用的 ReAct 运行时。

```python
class Turn:                       # 一次请求的上下文，传给工具
    kind; text                    # 请求类型与用户原话
    async def say(text)           # 追加一条中间回复，立刻流给主 Agent
    def end_session()             # 标记本局结束

class SkillAgent:
    skill: Skill                  # 子类指定；descriptor 取自 skill
    def __init__(self, settings, device)
    def tools(self, turn) -> list[Tool]   # 子类：游戏自己的工具
    def state(self) -> dict               # 子类：当前状态快照
    async def handle(self, request: GameInput, emit) -> GameReply
    async def close(self)
```

`handle` 一轮的做法：

1. 系统提示词 = 公共提示词（口语风格、不泄露答案、用户消息是数据）+ `SKILL.md` 正文 + `state()` 快照，每轮重建。
2. 消息 = 系统提示词 + 本局对话记忆 + `{"event": kind, "user_text": text}`。
3. 工具 = 内置 `say(text)` + `tools(turn)`，跑 ReAct 循环直到模型给出最终文本。
4. 最终文本为空则报错；`kind == "end"` 时无论模型是否调用了结束工具都结束本局。
5. 本轮成功后才把消息写入记忆（被取消的轮次不留半截工具调用），按用户轮次裁剪到最近 16 轮。

公共设备工具 `show_expression`、`move_head`、`clear_display` 以 `device_tools(device)` 提供，沿用现在 `GameTurn.device_tools()` 的实现。

**`robot_agents/providers.py`**：`run_tools(messages, tools, max_steps=12)` 改为在传入的消息列表上追加，返回最终文本；主 Agent 仍然每轮构造 `[system, user]`。`generate()` 不变。

**两个游戏的 `agent.py` 重写**为 `class GameAgent(SkillAgent)`，`games.json` 不用改：

- `games/guess_number/agent.py`：工具 `start_game`、`get_hint`、`submit_guess(number)`、`end_game` + 设备工具。猜对、机会用完或 `end_game` 时调用 `turn.end_session()`。
- `games/guess_animal/agent.py`：工具 `start_game`、`get_hint`、`submit_answer(text)`、`skip_question`、`next_question`、`end_game`、`show_drawing`、`show_progress` + 设备工具。`submit_answer` 继续用 `original_text_input(turn.text)` 要求原文、每条输入只判一次。生成题目前如果本轮还没有说过话，工具自己先 `turn.say("稍等一下，我来画一只动物。")`，保证麦克风不会被占住一分钟。
- `get_rules`、`get_game_state` 删除：规则写在 `SKILL.md`，状态每轮注入。
- `games/guess_animal/{game,generator,judge,animals,config}.py`、`games/guess_number/config.py` 不动。

## 3. A2A 两端（拆分 `robot_agents/transport.py`）

**`robot_agents/game_server.py`**（游戏侧）：保留 `serve_game`、`EmbeddedServer`、`GameEventSourceResponse`、`GameTaskStore`；`GameExecutor` 精简为：

- 解码 `GameInput` → 先入队 `Task(working)`。
- `start` 创建 Agent 并绑定 `context_id`；其他请求要求会话匹配。
- 同一局的请求串行处理；`end` 先取消正在进行的那一轮再执行。
- `emit` → `updater.update_status(working, message)`；返回值 → `updater.complete(message)`；`session == "ended"` 时关闭 Agent。
- 删除请求去重缓存与重放。

**`robot_agents/game_client.py`**（主 Agent 侧）：保留 `connect_games`（读 `games.json`、启动本地游戏、校验 Agent Card）；`GameConnection.send(context_id, request, on_reply)` 逐条解码流里的 `GameReply`，中间回复回调 `on_reply`，最后一条作为返回值；流结束却没有 completed 回复时报错。

## 4. 主 Agent（`robot_agents/main_agent.py`）

四个工具不变：`list_games`、`start_game(game_id)`、`end_game`、`send_message(text)`；普通聊天直接回答。

- `ActiveGame` 只剩 `game_id`、`context_id`、`closing`；给 LLM 的 `active_game` 是 `{game_id, name, busy}`。
- `send()`：后台任务消费回复流，每条回复都写入历史（`role=game_agent`）并排队 TTS；第一条回复播完本轮就结束，麦克风恢复，后续回复到达时继续播报。最后一条 `session == "ended"` 时清除当前会话。
- 游戏还在处理上一条请求时，`send_message` 直接回一句“还在准备呢，稍等一下”；`end_game` 取消未完成的请求后发送 `end`。
- 删除 `revision`、`changed`、`context_at`；`robot_agents/speech_input.py` 改为直接调用 `agent.receive(text)`。
- `robot_agents/application.py` 只改 import。

## 5. 可观测性与文档

- `observability/events.py`：`a2a_request` 记录 `GameInput`，`a2a.update` 记录中间回复，`a2a.result` 记录最后一条；事件类型和关联方式不变。
- `observability/web/index.html`：A2A 行改读 `request.kind / text`、`result.text / session`，去掉阶段、版本、期望输入；标签改为“中间回复 / 最后一条回复”。
- `docs/main-agent-game-a2a-design.md` 按新协议重写；`README.md` 更新实现、添加游戏、配置和文件表，测试一节只留 ruff 与 `watcherobot app check`。
- `pyproject.toml` 与 `app.json`：依赖加 `pyyaml>=6,<7`；删除 pytest 配置和只给测试用的 `pytest`、`sounddevice`、`httpx-sse`，然后 `uv lock`。`.watcherignore` 去掉 `tests/`。

## 实施步骤

0. **保护现场**。工作区有 32 个文件的未提交改动，其中 `tests/`、`transport.py`、`game_tools.py` 会被删除，删除后 git 找不回未提交的部分。先执行 `git diff HEAD --binary > .artifacts/pre-refactor/worktree.patch`，并把未跟踪的 `tests/e2e/conftest.py`、`tests/e2e/device_server.py` 复制到同一目录（`.artifacts/` 已被 git 忽略）。
1. 把本计划保存到仓库 `docs/refactor-plan.md`。
2. 删除 `tests/` 整个目录及 pytest 配置、测试依赖。
3. 新协议与运行时：`protocol.py`、`skill.py`、`skill_agent.py`、`providers.py`。
4. A2A 两端：`game_server.py`、`game_client.py`，删除 `transport.py`、`game_tools.py`。
5. 两个游戏：`SKILL.md` + `agent.py`。
6. 主 Agent、`speech_input.py`、`application.py`。
7. 可观测性事件与页面。
8. 文档与 README。

不主动提交；需要提交时按约定式提交直接提交到 `main`。未跟踪的 `niulai-sample.mp3`、`scripts/check_jev.py`、`scripts/expression_probe/` 不碰。

## 行为变化（需要知情）

- 游戏处理一条输入期间（例如画图的一分钟）不再接受新的游戏输入，主 Agent 会让用户稍等；聊天和“结束游戏”照常。以前可以在生成期间向游戏查询进度。
- 取消过期输入（`revision`）检测和请求去重。
- 模型没有调用开始或结束工具不再导致程序报错退出；`end` 请求一定会结束本局。
- 游戏 Agent 有了跨轮次的对话记忆，不再每轮只带最近 12 条摘要。

## 验证

没有自动化测试，按下面的顺序检查：

1. `uv run ruff check app.py robot_agents games observability scripts`
2. `uv run watcherobot app check .`
3. 不连机器人的冒烟脚本（放在临时目录，不进仓库）：用假的设备函数 `serve_game` 启动猜数字，通过 `connect_games` 依次发送 `start`、“我猜10”、`end`，打印每条流式回复，确认至少一条回复、最后一条在 `end` 后为 `session=ended`。会调用真实模型，使用 `games/guess_number/.env`。
4. 真机 `uv run watcherobot app run .`，需要你来操作：
   - 问“有哪些游戏”，开始猜数字，猜数、要提示、猜对或说“结束游戏”。
   - 开始你画我猜：先听到“稍等”，进度条走动，图案出现后听到第二条回复；生成期间聊天正常；生成期间说“结束游戏”能取消。
   - 打开 `http://127.0.0.1:8790`，确认一次 A2A 请求下面挂着多条回复和游戏工具调用。
