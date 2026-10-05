# 你画我猜

对桌面机器人说“我们来玩你画我猜”，屏幕会逐步画出一种动物。直接说动物名称即可回答。

等待、生成题目和绘图期间都可以自由交流，例如询问动物知识或追问刚才的话题。机器人播报时暂停识别，播报结束后恢复；播报期间说出的内容会被丢弃。全部语音按顺序完整播放。自由交流由 DeepSeek 处理，保留最近六轮聊天上下文；游戏指令和答案由 Jev 分类，普通聊天不会消耗回答次数。

每局五道题目，由所选绘图 provider 逐题生成，同一局的动物不会重复。每题有两次回答机会，答对获得一分。可以说“提示”“下一题”“结束游戏”。识别不明确时会请用户重新回答，不消耗回答次数。游戏中连续三次、每次二十秒没有回答，会结束当前游戏并等待下一次开始指令。

每题生成三个简短提示 `hints`，每条以“它”开头，写成适合语音播报的完整短句，长度为 4 至 16 个字符，只描述一个特征，从宽泛线索逐步过渡到明显特征。题目展示时不主动播报提示。用户主动请求提示或首次答错时，按共同的顺序提供下一条提示；首次猜测前也可以请求提示。提示用完后告知用户已经没有新提示。请求提示不消耗回答机会，进入下一题时重新从第一条开始。第二次答错时公布答案。

支持 DeepSeek API、本机 Claude Code 和本机 Codex。动物名称、认可别名、提示和全部笔画坐标均由模型动态生成，包括动物自己的眼睛。每题要求绘制完整动物全身，包含头部、躯干及该物种具有的四肢、翅膀、鳍或尾巴。模型决定视角、姿态和五官比例，所有身体部位都位于屏幕范围内，图案通过 SDK 的自定义矢量配件逐笔显示。

游戏使用 `emojis/` 中的三个颜文字表情：答对时显示 09，答错时显示 141，生成题目时显示 111。表情覆盖机器人原有眼睛。回答语音准备好后切换反馈表情并开始播放：答对时保持 09 直到语音结束；答错时显示 141，1.5 秒后恢复当前动物图案，语音继续播放，最后一次答错也采用相同规则。111 的进度条从开始调用模型时计时，按已用时间与 `DRAWING_ESTIMATE_SECONDS` 的比例线性增加，百分比向下取整，生成期间最高显示 99%；生成成功后显示 100% 并切换为动物图案。默认预计 60 秒，15、30、45 秒时分别显示 25%、50%、75%，超过预计时间保持 99%。这个进度条表示预计等待进度。结束游戏时恢复待机表情。

`guess_animal/expressions.py` 使用 `svgelements` 读取三个独立 SVG，用 `Shapely` 处理填充区域，再转换成 SDK 支持的白色矢量笔画。曲线使用折线近似，全部帧均遵守十二条笔画、一百九十二个坐标点的设备限制。`guess_animal/eyes.py` 的 `LOOKS` 保留九种基础眼型，供眼型预览程序使用。

DeepSeek provider 通过 OpenAI SDK 的 `AsyncOpenAI.chat.completions.create()` 调用官方兼容接口。本机 provider 分别调用 `claude --print` 和 `codex exec`，通过 JSON Schema 获取结构化输出。三个 provider 共用全身图案提示词、Pydantic 检查和去重历史。生成请求包含不同的构图要求、请求标识及近期动物名称。每幅图案必须符合坐标、笔画和总点数限制，重复名称、别名冲突、提示中包含答案及完全相同的笔画数据都会在发送设备前被拒绝。

应用启动时播放开始游戏的说明，收到开始指令后生成第一题。每道题目生成前，机器人会播报题号及预计等待秒数，播报完成后调用所选 provider。图案逐笔显示完成后，再说“第几题画好了！请看图，说出动物名称。”，播报完成后开始接收回答。答对、用完回答机会或跳过当前题目后，才生成下一题；最后一题结束后播报成绩。

通过 `DRAWING_ESTIMATE_SECONDS` 设置语音播报的预计等待时间，默认 60 秒；实际耗时取决于 provider、模型和服务情况。每次生成的 JSON 与去重记录保存在 `.artifacts/`；重新启动应用或切换 provider 后仍会读取近期动物与图案记录。

## 运行

需要 Python 3.11 以上、uv，以及支持 `expression.runtime.v3`、`expression.vector_accessory.v1`、`microphone`、`rtc.audio.full_duplex.v1` 的机器人。电脑和机器人需要能够通过局域网建立 WebRTC 连接。

```bash
uv sync
uv run watcherobot robot status
uv run watcherobot app run .
```

设备首次连接时执行 `uv run watcherobot robot setup`。应用必须通过 SDK Runtime 启动。停止应用可以按运行终端的 Ctrl+C，或者执行 `uv run watcherobot app stop`。

## 配置

在项目根目录的 `.env` 中提供配置，环境变量优先。配置名称见 `.env.example`。本机的 API key 已经写入 `.env`；Git 与 SDK 发布文件收集规则均排除该文件。

| 配置 | 默认值或说明 |
| --- | --- |
| `DASHSCOPE_API_KEY` | ASR 与 TTS 共用的 API key |
| `TYPESAFE_API_KEY` | Jev API key |
| `DRAWING_PROVIDER` | `deepseek`、`claude_code` 或 `codex`，默认 `deepseek` |
| `DRAWING_TIMEOUT_SECONDS` | 单次生成超时秒数，默认 `180` |
| `DRAWING_ESTIMATE_SECONDS` | 生成前播报的预计秒数，默认 `60` |
| `CLAUDE_CODE_COMMAND` | Claude Code 命令或绝对路径，默认 `claude` |
| `CLAUDE_CODE_MODEL` | 留空使用 Claude Code 配置的模型 |
| `CODEX_COMMAND` | Codex 命令或绝对路径，默认 `codex` |
| `CODEX_MODEL` | 留空使用 Codex 配置的模型 |
| `DEEPSEEK_API_KEY` | 自由交流必填，同时供 DeepSeek 绘图使用 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | `deepseek-flash` |
| `DASHSCOPE_BASE_URL` | 用户提供的北京业务空间地址 |
| `QWEN_ASR_MODEL` | `qwen-audio-3.1-asr-flash-streaming` |
| `QWEN_TTS_MODEL` | `qwen-audio-3.1-tts-flash` |
| `QWEN_TTS_VOICE` | `yuxiaoyun_v3.1` |
| `JEV_MODEL` | `jev-latest` |
| `JEV_CONFIDENCE_THRESHOLD` | `0.65`，低于阈值时不接受判定 |

ASR 与 TTS 使用 Qwen-Audio 的 WebSocket 协议。程序从 `DASHSCOPE_BASE_URL` 提取主机名称，并使用 `/api-ws/v1/inference` 路径。模型与音色必须匹配，同系列的参数配置需要符合对应模型文档。

## 本机绘图工具

安装并登录要使用的 CLI，在运行机器人的同一系统账户下确认命令可用：

```bash
claude --version
claude auth status
codex --version
codex login status
```

使用 Claude Code 时，在 `.env` 中设置：

```dotenv
DRAWING_PROVIDER=claude_code
CLAUDE_CODE_COMMAND=claude
CLAUDE_CODE_MODEL=
```

使用 Codex 时，在 `.env` 中设置：

```dotenv
DRAWING_PROVIDER=codex
CODEX_COMMAND=codex
CODEX_MODEL=
```

保存配置后重新执行 `uv run watcherobot app run .`。如果 Runtime 的 `PATH` 找不到 CLI，请把对应 `COMMAND` 配置成可执行文件的绝对路径。本机 provider 使用 CLI 现有登录状态及模型配置；模型变量可覆盖该次绘图使用的模型。ASR、TTS 和 Jev 仍使用各自的 API key。

CLI 每题独立调用，输入包含全部生成要求，输出为动物笔画 JSON。调用工作目录、临时文件和日志位于 `.artifacts/provider_runs/<provider>/`。退出码非零、缺失输出、无效图案和超时都会直接终止本次生成；不会自动切换 provider。取消或超时会终止调用进程。

调用参数依据 [Claude Code 非交互文档](https://code.claude.com/docs/en/headless) 和 [Codex 非交互文档](https://developers.openai.com/codex/noninteractive/)。

## 语音处理

等待用户、生成题目和绘图期间，通过 SDK 麦克风接收 16 kHz、单声道、16-bit PCM，发送到持续运行的 Qwen ASR。程序处理每句话的最终识别结果，使用句子编号去重。Jev 区分游戏控制、动物答案和普通交流；DeepSeek 回答聊天内容，游戏程序生成答题反馈。ASR 热词使用游戏控制指令，动物名称由 ASR 自由识别。

机器人反馈使用 Qwen 分块返回的 24 kHz、单声道、16-bit PCM，TTS 音量为 10。`QwenSpeech.synthesize_stream()` 持续输出音频，`RtcAudioPlayer.play()` 接收异步音频流。PyAV 将音频转换为设备使用的 16 kHz 单声道，以 32 kbps 编码为每包 20 毫秒的 Opus，再通过 `aiortc` 连续发送。每次播报建立 RTC 连接，首段音频准备完成后开始播放，播报结束后释放连接。

播放队列最多保存 0.5 秒的 PCM，队列满时暂停读取上游。结束时补充 0.3 秒静音，并检查设备解码进度；连接失败、设备播放错误与完成等待超时都会报告异常。退出应用会关闭 RTC、ASR 和 TTS，释放设备音频资源。日志记录首段 PCM 到达、开始发送和播放完成的时间。反馈表情在首段音频开始发送前展示，答错表情从此时开始计时 1.5 秒。

播报开始前关闭 SDK 麦克风并清除待发送的音频，向 ASR 发送静音维持连接；涉及这段静音区间的识别结果也会被忽略。RTC 使用 `sendonly` 发送播放音频。播报完成后关闭 RTC 并重新开启 SDK 麦克风，等待开启完成后继续游戏流程。已经接收的输入按顺序处理，聊天回复、生成提示和游戏反馈统一进入播报队列，彼此不会打断。

题目生成在独立任务中进行，因此等待生成时仍可聊天或结束游戏。每句话关联其音频接收时的题目版本，旧题目的答案不会改变新题目。结束游戏会取消尚未完成的生成，并清除尚未播报的过期游戏提示；正在播报的语音完整播放。等待回答的二十秒计时会在用户讲话和语音反馈后重新开始。

网络错误、鉴权失败、无效生成结果、设备错误、无效音频和接收队列溢出都会直接终止应用并输出错误。无语音属于正常等待状态。持续 ASR、TTS、DeepSeek 绘图和自由交流会产生 API 调用费用；本机绘图 provider 消耗对应 CLI 账户的用量。自由交流没有联网搜索功能，实时信息不会经过外部查询。

## 验证

```bash
uv run pytest -q
uv run ruff check app.py guess_animal scripts tests
uv run watcherobot app check .
uv run python -m scripts.check_services
uv run python -m scripts.check_generation
uv run python -m scripts.check_conversation
```

`check_services` 调用真实的 TTS、ASR 和 Jev，会产生模型调用费用。它验证合成音频的识别结果，以及游戏启动、别名、错误答案、多种候选、提示、结束等中文用例。使用 `--service speech` 或 `--service jev` 可单独检查。测试音频与报告写入忽略目录 `.artifacts/`。

连接机器人并停止游戏应用后，可以检查真实流式播放：

```bash
uv run watcherobot app run scripts/stream_check
```

检查程序播放长句和短句，记录首段 PCM、首帧发送及设备首次报告有效音频输出的时间，并检查开始发送早于全部输入结束。随后验证中途取消、再次播放与麦克风恢复。报告写入 `.artifacts/stream_check.json`。设备音频统计定期上报，首次报告时间包含上报间隔，不能等同于人耳听见声音的准确时间。

`check_conversation` 使用真实 Jev 和 DeepSeek 验证普通问答、连续追问与游戏分类，结果写入 `.artifacts/conversation_check.json`。macOS 上可将机器人放在电脑扬声器附近，执行完整语音交流检查：

```bash
uv run watcherobot app run scripts/conversation_device_check
```

这个检查通过电脑扬声器播放真实合成的开始指令、聊天提问和结束指令，由机器人麦克风与真实 ASR 接收。它检查播报期间暂停识别、准备题目期间可以聊天、聊天不影响计分，以及播报按顺序完成。报告和测试音频写入 `.artifacts/conversation_device_check/`。

`check_generation` 根据 `DRAWING_PROVIDER` 连续三次真实生成，每次只生成一道题目，记录耗时并验证动物去重、图案去重和 SDK 矢量格式。使用 `--count 1` 可检查一道题目。报告按 provider 保存在 `.artifacts/generation_check/`。Jev 检查中的动物答案也由所选 provider 真实生成。单元测试中的 JSON 样本来自真实生成结果，保存在 `tests/fixtures/`。

可以直接在终端分别检查三个 provider：

```bash
DRAWING_PROVIDER=deepseek uv run python -m scripts.check_generation --count 1
DRAWING_PROVIDER=claude_code uv run python -m scripts.check_generation --count 1
DRAWING_PROVIDER=codex uv run python -m scripts.check_generation --count 1
```

连接机器人并停止游戏应用后，可以在真机上对比全部眼型，不调用任何模型服务。屏幕先显示眼型编号，再显示该眼型，约八秒后自动切到下一种；转动滚轮可前后切换，点击屏幕切到下一种，手动切换后停止自动轮播。终端同步输出当前眼型的编号与说明。按 Ctrl+C 或十分钟后结束，并恢复待机表情：

```bash
uv run watcherobot app run scripts/eye_preview
```

同样在停止游戏应用后，可以执行设备检查：

```bash
uv run watcherobot app run scripts/device_check
```

设备检查展示 09、141 和 111，并发送 111 从 0 到 100 的全部进度帧，检查设备确认。随后运行两道题目的完整流程：播报生成提示、单题生成、逐笔显示、播报完成提示，将回答文本通过真实 Jev 接口判定并播报结果。第一题检查答错后显示 141、恢复动物图案，以及答对后显示 09；第二题检查两次答错和结束游戏的行为。每次答错都会记录表情显示至恢复图案的时间，并检查恢复发生在语音播放期间；计时检查包含 1.5 秒等待和设备通信容差。它同时检查流式播放完成、麦克风音频格式、数据量和音频丢失情况。结果写入 `.artifacts/device_check.json`。这个检查会实际改变机器人屏幕并播放语音，完成后恢复待机表情。验证范围为设备指令和数据传输；回答文本由检查程序传入 Jev，图案的视觉效果和 ASR 识别效果需要单独检查。

提示检查在每道题首次答错后检查第一条提示，并检查下一题的提示顺序重新开始。所有答案均通过真实 Jev 请求和机器人语音播放，报告记录生成的提示、播放文本及当前音色。

## 文件

| 文件 | 用途 |
| --- | --- |
| `app.py` | SDK Application 入口 |
| `guess_animal/animals.py` | 动物数据结构、矢量编码与屏幕限制 |
| `guess_animal/generator.py` | 全身图案提示词、输出检查与生成历史 |
| `guess_animal/expressions.py` | 09、111、141 的 SVG 转换和生成进度 |
| `guess_animal/providers.py` | DeepSeek、Claude Code、Codex 调用及进程清理 |
| `guess_animal/game.py` | 出题、回答次数、计分和题目切换 |
| `guess_animal/eyes.py` | 待机、隐藏眼睛与基础眼型预览参数 |
| `guess_animal/speech.py` | Qwen ASR 与 TTS |
| `guess_animal/audio_stream.py` | PCM 流的 RTC 播放、播放统计与取消清理 |
| `guess_animal/microphone.py` | SDK 麦克风接收、播报时关闭收音与恢复 |
| `guess_animal/conversation.py` | DeepSeek 自由交流与聊天上下文 |
| `guess_animal/judge.py` | Jev 游戏意图及答案判定 |
| `guess_animal/robot_game.py` | 麦克风、播放、显示及游戏流程 |
| `guess_animal/config.py` | 配置读取与检查 |
| `tests/test_game.py` | 游戏规则和 SDK 图案格式测试 |
| `tests/test_generator.py` | 生成格式、坐标限制、提示与去重测试 |
| `tests/test_eyes.py` | 眼睛参数的 SDK 检查 |
| `tests/test_expressions.py` | 表情资源打包、矢量几何误差及全部进度帧的 SDK 检查 |

SDK 会收集应用根目录中的 Python 模块。`app.json` 声明运行依赖，`.watcherignore` 排除测试脚本与中间文件。
