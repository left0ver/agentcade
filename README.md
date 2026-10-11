# 桌面机器人 Agent

主 Agent 负责语音聊天和游戏选择，通过 A2A 与独立游戏 Agent 通信。目前提供“你画我猜”和“猜数字”，游戏目录由 `games.json` 配置。

说“有哪些游戏”查看目录，说“开始猜数字”或“玩第二个”选择游戏。游戏期间可以聊天，也可以直接继续回答；说“继续游戏”查询当前进度。等待用户输入期间保持游戏状态。

同时只能运行一个游戏。说“结束游戏”退出，结束播报和设备操作完成后才能开始其他游戏。直接要求换游戏时，机器人会提示先退出当前游戏。

## 你画我猜

对机器人说“我们来玩你画我猜”开始。

- 每局 5 题，每题 2 次机会，答对得 1 分，同一局动物不重复
- 最近 72 小时出现过的动物、同义名称和相同图案不会重复，满 72 小时后可以再次出题；重启程序后仍然保留有效历史
- 答对或答错时，机器人会用点头、摇头或表情反馈，动作完成后回到居中姿态
- 说“提示”要线索，每题 3 条，不占机会；第一次答错也会给一条
- 答题结束后说“下一题”继续；答题期间说“下一题”跳过，说“结束游戏”退出
- 画图要等一会儿，这段时间可以随便聊天，聊天不算答题；和游戏有关的话等画完再说，想退出可以直接说“结束游戏”

机器人说话时不收音，播报完成后恢复收音。

## 猜数字

机器人随机选择 1 到 20 的整数，玩家有五次回答机会。回答后会告知数字过大或过小；说“提示”可以获得奇偶信息。答对或机会用完后结束，也可以随时说“结束游戏”。

## 运行

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)，以及一台和电脑在同一局域网的机器人，固件支持 `expression.runtime.v3`、`expression.vector_accessory.v1`、`microphone`、`audio.stream`、`motion`。

```bash
cp robot_agents/.env.example robot_agents/.env
cp games/guess_animal/.env.example games/guess_animal/.env
cp games/guess_number/.env.example games/guess_number/.env
# 分别填写三个文件中的配置
uv sync
uv run watcherobot robot setup    # 仅首次连接设备
uv run watcherobot app run .
```

按 Ctrl+C 或执行 `uv run watcherobot app stop` 停止。

## 配置

每个 Agent 从自己目录中的 `.env` 读取配置，对应目录提供 `.env.example`。文件必须存在；读取时不修改进程环境，不读取根目录 `.env`，也不使用同名进程环境变量覆盖。配置值按字面读取，不展开 `${...}`。

| 配置文件 | 用途 |
| --- | --- |
| `robot_agents/.env` | 主 Agent 的文字 LLM、工具调用、ASR、TTS、音色和游戏目录路径 |
| `games/guess_animal/.env` | 你画我猜的游戏 Agent 与绘图模型、Jev、置信度和预计绘图时间 |
| `games/guess_number/.env` | 猜数字的游戏 Agent 模型 |

| 变量 | 用途 |
| --- | --- |
| `DASHSCOPE_API_KEY` | 主 Agent 的 Qwen ASR/TTS 接口凭据 |
| `QWEN_ASR_MODEL` | 默认 `qwen-audio-3.1-asr-flash-streaming`，持续识别用户语音 |
| `QWEN_TTS_MODEL` | 默认 `qwen-audio-3.1-tts-flash`，合成聊天和游戏播报 |
| `QWEN_TTS_VOICE` | TTS 音色，默认 `yuxiaoyun_v3.1` |
| `SPEECH_TIMEOUT_SECONDS` | 单次语音合成最长等待秒数，默认 90 |
| `ASR_SILENCE_MS` | ASR 判定说话结束的静音时间，默认 800 毫秒 |
| `ASR_INPUT_GAIN` | 麦克风 PCM 输入增益，默认 4，范围 1 至 8；通过 FFmpeg volume 处理，保持采样率和时间信息 |
| `TYPESAFE_API_KEY` | 你画我猜的 JEV 答案判定凭据 |
| `JEV_MODEL` / `JEV_CONFIDENCE_THRESHOLD` | 你画我猜的 JEV 模型与判定置信度，默认 `jev-latest` / `0.65` |
| `LLM_API_KEY` | 当前配置文件所属 Agent 的 OpenAI 兼容接口凭据 |
| `GAME_CATALOG` | 主程序的游戏目录路径，默认项目中的 `games.json`；相对路径以主程序 `.env` 所在目录为基准 |
| `OBSERVABILITY_PORT` | 可观测性页面的本机端口，默认 8790；不能使用 SDK 守护进程占用的 8765 和 8767 |

`DASHSCOPE_BASE_URL` 是百炼业务空间的地址，换 key 时记得一起改。

## 文字模型

主 Agent 和游戏 Agent 各自在自己的 `.env` 中设置 `LLM_MODEL_NAME`、`LLM_BASE_URL`、`LLM_API_KEY` 和请求参数。相同键名在不同文件中互不影响，运行时不会读取其他 Agent 的配置。全部 LLM 调用通过 OpenAI SDK 访问兼容 Chat Completions 的接口，使用原生 function calling 执行注册工具。主 Agent 的普通聊天回复直接交给 TTS。

OpenAI 兼容接口在对应 Agent 的配置文件中填写，例如：

```dotenv
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=填写你的密钥
LLM_MODEL_NAME=deepseek-flash
LLM_EXTRA_BODY_JSON={"thinking":{"type":"enabled"},"reasoning_effort":"low"}
```

更换服务时，将 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL_NAME` 改成目标服务的配置。这三项必须填写，接口需要支持 JSON 对象输出和 function calling。`LLM_EXTRA_BODY_JSON` 默认是 `{}`，用于填写所选服务支持的可选参数，如思考模式、输出长度或服务等级。它不能覆盖模型、消息、响应格式、流式模式或工具参数。`LLM_TIMEOUT_SECONDS` 控制单次调用超时，默认 180 秒。

修改配置后重新启动应用。本地游戏的 A2A 等待时间为该游戏模型超时的三倍加 30 秒。远程游戏在 `games.json` 中单独配置 `timeout_seconds`，默认 210 秒。

题目生成失败或超时会自动重试，最多三次，仍然失败时机器人会请用户再试一次。每次生成的 JSON 和去重记录存在 `.artifacts/`，重启后仍然有效。

你画我猜的生成提示词要求模型优先选择常见动物，并使用常用、容易读写的名称和别名，避免名称中含有不常见的生僻字。

## 可观测性

应用运行期间打开 `http://127.0.0.1:8790` 查看实时轨迹，端口由 `OBSERVABILITY_PORT` 设置，服务只监听本机。端口上已有其他服务时应用启动失败并提示修改端口。页面按轮次列出记录，点击任意一行在右侧查看完整输入和输出：

| 记录 | 内容 |
| --- | --- |
| 用户 | ASR 的最终识别文字，每条开启一轮 |
| 主 Agent · 回复 | 主 Agent 直接回复用户的聊天文字 |
| 主 Agent · 工具、游戏 Agent · 工具 | 各自发起的工具调用，包含完整参数、返回或错误、耗时 |
| A2A →、A2A ← | 主 Agent 发给游戏的 `GameInput`，以及游戏返回的每一条 `GameReply`：中间回复和最后一条回复 |
| 设备执行 | 实际播报、绘图、表情、动作与完成状态，关联发起操作的游戏工具 |

设备记录带有图案时，检查器显示对应笔画。A2A 回复展示面向用户的文字和本局是否结束，可与主 Agent 的播报原文核对。检查器底部列出关联的上级和下级记录，点击即可跳转。一次 A2A 请求及其游戏工具、设备执行均归入发起请求的那一轮。程序退出时自动发送的 `end` 显示在“轮次之间”。远程游戏的设备 trace 由游戏部署方记录。

应用事件在内存中保留最近 2000 条。页面最多显示最近 3000 条记录，断线后保留已收到的记录并自动重连。事件包含对话内容，不包含密钥。

每一轮的标题显示从用户说完到最后一条记录的耗时，时间线一列画出各条记录在这一轮中的起止位置：工具调用和 A2A 请求是时间段，其余记录是时间点。页面顶部可以搜索记录、收起或展开全部轮次；收起后每轮显示用户原话和记录条数。上下方向键切换选中的记录。

## 检查

```bash
uv run ruff check app.py robot_agents games observability scripts
uv run watcherobot app check .
```

真机端到端测试：

```bash
uv run python scripts/e2e_robot.py
```

脚本启动应用，用电脑扬声器替用户说话，机器人收音并回应，再根据可观测性事件核对每一步：查询游戏、猜数字的开局、猜数、提示和结束、游戏中的闲聊，以及你画我猜的开局（先回复稍等、显示进度、画出题目）、提示、作答和结束。运行前连接机器人并放在电脑扬声器附近，保持环境安静；全程大约五分钟，会调用真实的语音和模型服务。合成的用户语音、应用日志、轨迹和报告保存在 `.artifacts/e2e/<时间>/`。

除此之外没有其他自动化测试。`uv run watcherobot app run scripts/eye_preview` 可以在真机上逐个预览眼型，不调用模型。

## 实现

```mermaid
flowchart TD
    Mic[机器人麦克风 16 kHz PCM] --> ASR[Qwen ASR\n服务端判断说话结束]
    ASR -->|用户原话| Main[主 Agent：聊天与工具调用\n内部包含 A2A Client]
    Catalog[games.json 和 Agent Card] --> Main
    Main -->|A2A JSON-RPC| Animal[你画我猜 Agent]
    Main -->|A2A JSON-RPC| Number[猜数字 Agent]
    Main -->|A2A JSON-RPC| Extension[新增游戏 Agent]
    Animal --> Drawing[绘图 LLM 与 Jev]
    Animal --> Result[一条或多条面向用户的回复]
    Number --> Result
    Extension --> Result
    Main --> Queue[聊天与游戏回复输出队列]
    Queue --> Output[公共设备执行模块]
    Animal --> Output
    Number --> Output
    Extension --> Output
    Result --> Main
    Output -->|聊天与游戏播报文本| Speech[Qwen TTS]
    Speech --> Audio[SDK PCM 音频播放]
    Output --> Device[屏幕、表情、头部动作]
```

主 Agent 使用持续的 ASR WebSocket 会话识别麦克风语音，由 ASR 服务判定说话结束。主 Agent 根据原话、历史和当前游戏，直接回答，或者调用 `list_games`、`start_game`、`end_game`、`send_message`。每轮模型调用都会将当前已注册游戏的名称、ID、别名和介绍按目录顺序写入系统提示词，新增游戏通过注册即可使用。普通聊天由主 Agent 直接回复，游戏介绍和播报使用简短的日常口语。

主 Agent 通过 A2A 给游戏 Agent 发一条消息，游戏 Agent 把它当作用户输入，回复一条或多条要播报的话，主 Agent 原样交给 TTS。第一条回复播完后恢复收音，其余回复到达时继续播报。游戏还在处理上一条输入时，新的游戏输入不会转发，主 Agent 会请用户稍等。消息格式见 [通信设计](docs/main-agent-game-a2a-design.md)。

游戏 Agent 是由 `SKILL.md` 驱动的 ReAct Agent。`SKILL.md` 写明规则和整体流程，模型按流程连续调用游戏自己的工具：规则工具返回题目、提示、答案判定和计分等事实，设备工具直接执行绘图、表情和动作。耗时操作之前，模型用 `say` 先说一句话。聊天期间游戏状态保持不变，等待用户输入期间没有计时提醒、机会扣除或自动结束。

本地游戏通过官方 `a2a-sdk` 启动仅监听 `127.0.0.1` 的 HTTP 服务，并通过 Agent Card 发现。`context_id` 标识一局游戏，同一局的请求逐条处理；`end` 会取消正在处理的请求。主 Agent 收到本局结束的回复后清除当前会话。

- **图案**：模型输出动物名、别名、3 条提示和笔画坐标，经 Pydantic 校验后用 SDK 的矢量配件逐笔显示。设备上限是 12 条笔画、192 个点。重名、提示里带答案、笔画和以前完全相同的结果都会被拒绝。
- **表情**：`robot_agents/assets/emojis/` 里的三个 SVG 转成白色矢量笔画，分别用于生成中、答对、答错。生成中的进度条分成十格，按 `DRAWING_ESTIMATE_SECONDS` 估算，每经过预计时间的十分之一增加一格，等待结果时最多显示九格。生成完成后显示满格，再绘制动物。每次进度变化使用 `expression_runtime.start()` 提交完整图案和显示参数，一次生成最多提交 11 个进度画面。`ProgressDisplay` 管理计时和任务标识，显示命令串行执行，退出后旧进度请求失效。
- **语音**：接收 TTS 返回的 24 kHz、单声道、16-bit PCM，完整响应收齐后通过 SDK `audio.play_pcm()` 播放。所有播报排队，互不打断；取消时停止设备播放。当前采用半双工交互，输入处理和播报流程分别登记麦克风暂停请求，全部释放后才恢复收音。暂停期间向 ASR 发送静音，涉及暂停音频的识别结果会被忽略。
- **设备连接**：`DeviceContext` 等待 `sys.sdk.ready.get` 确认与 `evt.sdk.ready` 能力信息后完成启动。麦克风收到第一段 PCM 后才标记为可以收音；连续 10 秒没有 PCM、设备指令超时或会话被关闭时，重新打开麦克风继续收音，日志包含会话 ID、最后序号、接收字节数和解码状态。音频发送任务串行执行，取消会结束该任务内部的流控等待。
- **出错**：启动阶段的问题直接退出并打印错误，包括配置缺失、机器人未连接或缺少能力、端口被占用、游戏加载失败，以及第一次连接识别服务失败。运行起来之后，单次故障只影响当时的那一步，程序继续运行：

  | 故障 | 处理 |
  | --- | --- |
  | 麦克风停流、设备指令超时、会话被关闭 | 重新打开麦克风，间隔 1 至 5 秒，直到恢复 |
  | 识别服务断开或超时 | 重新连接，间隔 1 至 10 秒，直到恢复 |
  | 语音合成失败 | 重试两次，仍然失败则跳过这一条播报 |
  | 播放、屏幕或动作失败 | 跳过这一条输出；游戏工具把失败告诉模型，由它重试或向用户说明 |
  | 主 Agent 的模型出错 | 请用户再说一遍 |
  | 游戏请求失败 | 提示用户；开局和结束失败时清除这一局，局内输入失败时保留 |
  | 游戏 Agent 的模型出错 | 回复一句兜底的话；开局失败时结束这一局 |
  | 模型接口连接失败、限流或服务端错误 | 自动重试两次 |
  | 答案判定（JEV）连接失败、超时、限流或服务端错误 | 自动重试两次，连同等待最多 30 秒 |
  | 工具参数不合法或执行失败 | 错误交还给模型，由它重试或向用户说明 |

  故障写入应用日志。运行中仍然会退出的情况：应用与机器人守护进程的通道断开，以及麦克风音频格式不符。

| 文件 | 职责 |
| --- | --- |
| `app.py` | SDK 入口 |
| `robot_agents/device.py` | 设备启动确认、音频发送顺序与设备状态记录 |
| `robot_agents/application.py` | 收音、主 Agent 与输出队列的运行周期 |
| `robot_agents/speech_input.py` | 持续 ASR 输入、语音增益和识别结果路由 |
| `robot_agents/tools.py` | 工具注册、JSON Schema、参数校验和执行记录 |
| `robot_agents/protocol.py` | 主 Agent 与游戏 Agent 的统一通信格式、游戏声明和设备输出 schema |
| `robot_agents/main_agent.py` | 主 Agent 决策、聊天历史、单游戏限制和游戏回复的播报 |
| `robot_agents/game_client.py` | 主 Agent 一侧：游戏注册、Agent Card 校验和 A2A 回复流 |
| `robot_agents/game_server.py` | 游戏一侧：本地 A2A 服务、会话与请求的顺序和取消 |
| `robot_agents/task_store.py` | 已完成 A2A Task 的保留数量 |
| `robot_agents/skill.py` | `SKILL.md` 的读取与校验 |
| `robot_agents/skill_agent.py` | 所有游戏共用的 ReAct 运行时、`say` 和公共设备工具 |
| `robot_agents/output.py` | 统一播报、屏幕、表情、动作与麦克风管理 |
| `games/<game_id>/SKILL.md` | 游戏声明、规则和流程 |
| `games/<game_id>/agent.py` | 游戏入口 `GameAgent`：状态和工具 |
| `games/<game_id>/config.py` | 游戏独立配置读取与检查 |
| `games.json` | 游戏目录 |
| `games/guess_animal/game.py` | 出题、计分、提示 |
| `games/guess_animal/generator.py` | 提示词、输出校验、去重历史 |
| `games/guess_animal/animals.py` | 动物数据结构 |
| `games/guess_animal/judge.py` | Jev 动物答案判定 |
| `robot_agents/providers.py` | OpenAI 兼容接口的结构化输出和工具调用 |
| `robot_agents/vector.py` | 公共笔画结构和矢量编码 |
| `robot_agents/expressions.py` | 表情 SVG 转换和进度条 |
| `robot_agents/eyes.py` | 待机眼睛和眼型预览参数 |
| `robot_agents/speech.py` | ASR/TTS WebSocket 协议、语音合成和识别时间关联 |
| `robot_agents/audio_stream.py` | SDK PCM 播放、完成确认和取消处理 |
| `robot_agents/microphone.py` | 麦克风收音和播报时静音 |
| `robot_agents/config.py` | 配置读取和检查 |
| `robot_agents/assets/emojis/` | 公共表情 SVG |
| `observability/events.py` | 事件总线、轮次与上下级关联、工具调用和 A2A 通信的记录入口 |
| `observability/server.py` | 本机 HTTP 服务与 SSE 事件流，支持按事件 ID 续传 |
| `observability/web/index.html` | 轨迹页面：按轮次分组的记录表与检查器 |
| `scripts/` | 真机端到端测试和眼型预览工具 |

## 添加游戏

每个游戏位于 `games/<game_id>/`，目录包含 `__init__.py`、`SKILL.md`、`.env.example` 和入口文件 `agent.py`。游戏专用模块放在对应游戏目录内；公共语音、模型和显示能力位于 `robot_agents/`。

`SKILL.md` 的 frontmatter 声明游戏，正文写规则和流程，启动时整体写入游戏 Agent 的系统提示词：

```markdown
---
id: guess_number
name: 猜数字
aliases: [数字游戏, 猜数]
description: 我会选一个1到20的整数，你来猜，最多有五次机会。
required_capabilities: [motion]
---

## 规则

……

## 流程

### 开局（event 为 start）

调用 start_game，然后告诉用户数字范围和机会数，请他开始猜。
```

`agent.py` 的入口类是 `GameAgent`，继承 `robot_agents.skill_agent.SkillAgent`：

- `skill = Skill.load(Path(__file__).with_name("SKILL.md"))` 读取游戏声明和流程。
- `load_settings()` 读取本游戏目录的 `.env`，返回包含 `llm: LLMSettings` 的游戏配置；其他游戏专用配置由游戏自行定义。
- `__init__(settings, device)` 调用父类构造函数，创建游戏状态。`device` 是异步设备执行函数，`await device(Presentation(...))` 执行图案、表情和动作。
- `tools(turn)` 返回本游戏的工具。`turn.text` 是用户原话，`await turn.say(text)` 立刻回复一句话，`turn.end_session()` 标记本局结束。需要表情和动作时加入 `device_tools(self.device)`。
- `state()` 返回当前状态快照，每轮写入系统提示词，不能包含未揭晓的答案。
- `close()` 释放游戏资源，需要时覆盖并调用父类实现。

工具返回可以转成 JSON 的事实，例如判定结果、提示和剩余次数，由模型组织成回复。无法执行的操作返回带 `outcome` 的结果，模型据此向用户说明。

把模块加入 `games.json`，重新启动应用：

```json
{
  "kind": "local",
  "id": "guess_number",
  "factory": "games.guess_number.agent:GameAgent"
}
```

`games/guess_number/` 是完整示例。增加使用现有语音、屏幕和动作能力的游戏，只需要新增游戏目录和注册配置，主 Agent 的提示词、选择逻辑和输出处理自动使用游戏声明。

独立部署的游戏可以配置 `{"kind":"remote","id":"my_game","url":"https://example.com"}`。服务提供 A2A Agent Card、流式 JSON-RPC 接口，以及标记 `desktop-game/5` 的 skill，并按 [通信设计](docs/main-agent-game-a2a-design.md) 收发消息。部署方通过 `serve_game(factory, settings, device)` 注入真实设备执行函数。设备能力通过 `capability:motion` 等 tag 声明，认证由部署环境提供。

## License

MIT
