# 你画我猜

对桌面机器人说“我们来玩你画我猜”，屏幕会逐步画出一种动物。直接说动物名称即可回答。

每局五道题目，由所选绘图 provider 逐题生成，同一局的动物不会重复。每题有两次回答机会，答对获得一分。可以说“提示”“下一题”“结束游戏”。识别不明确时会请用户重新回答，不消耗回答次数。游戏中连续三次、每次二十秒没有回答，会结束当前游戏并等待下一次开始指令。

支持 DeepSeek API、本机 Claude Code 和本机 Codex。动物名称、认可别名、提示和全部笔画坐标均由模型动态生成，包括动物自己的眼睛。每题要求绘制完整动物全身，包含头部、躯干及该物种具有的四肢、翅膀、鳍或尾巴。模型决定视角、姿态和五官比例，所有身体部位都位于屏幕范围内，图案通过 SDK 的自定义矢量配件逐笔显示。

游戏使用 `emojis/` 中的三个颜文字表情：答对时显示 09，答错时显示 141，生成题目时显示 111。表情覆盖机器人原有眼睛。回答语音准备好后切换反馈表情并开始播放：答对时保持 09 直到语音结束；答错时显示 141，1.5 秒后恢复当前动物图案，语音继续播放，最后一次答错也采用相同规则。111 的进度条从开始调用模型时计时，按已用时间与 `DRAWING_ESTIMATE_SECONDS` 的比例线性增加，百分比向下取整，生成期间最高显示 99%；生成成功后显示 100% 并切换为动物图案。默认预计 60 秒，15、30、45 秒时分别显示 25%、50%、75%，超过预计时间保持 99%。这个进度条表示预计等待进度。结束游戏时恢复待机表情。

`guess_animal/expressions.py` 使用 `svgelements` 读取三个独立 SVG，用 `Shapely` 处理填充区域，再转换成 SDK 支持的白色矢量笔画。曲线使用折线近似，全部帧均遵守十二条笔画、一百九十二个坐标点的设备限制。`guess_animal/eyes.py` 的 `LOOKS` 保留九种基础眼型，供眼型预览程序使用。

DeepSeek provider 通过 OpenAI SDK 的 `AsyncOpenAI.chat.completions.create()` 调用官方兼容接口。本机 provider 分别调用 `claude --print` 和 `codex exec`，通过 JSON Schema 获取结构化输出。三个 provider 共用全身图案提示词、Pydantic 检查和去重历史。生成请求包含不同的构图要求、请求标识及近期动物名称。每幅图案必须符合坐标、笔画和总点数限制，重复名称、别名冲突、提示中包含答案及完全相同的笔画数据都会在发送设备前被拒绝。

应用启动时播放开始游戏的说明，收到开始指令后生成第一题。每道题目生成前，机器人会播报题号及预计等待秒数，播报完成后调用所选 provider。图案逐笔显示完成后，再说“第几题画好了！请看图，说出动物名称。”，播报完成后开始接收回答。答对、用完回答机会或跳过当前题目后，才生成下一题；最后一题结束后播报成绩。

通过 `DRAWING_ESTIMATE_SECONDS` 设置语音播报的预计等待时间，默认 60 秒；实际耗时取决于 provider、模型和服务情况。每次生成的 JSON 与去重记录保存在 `.artifacts/`；重新启动应用或切换 provider 后仍会读取近期动物与图案记录。

## 运行

需要 Python 3.11 以上、uv，以及支持 `expression.runtime.v3`、`expression.vector_accessory.v1`、`audio.stream`、`microphone` 的机器人。

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
| `DEEPSEEK_API_KEY` | 选择 `deepseek` 时必填 |
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

等待游戏指令和回答时，机器人麦克风通过 SDK 输出 16 kHz、单声道、16-bit PCM，发送给 ASR。程序只处理句子结束后的识别结果，再由 Jev 判断游戏指令及答案是否正确。ASR 热词使用游戏控制指令，动物名称由 ASR 自由识别。

机器人反馈使用 24 kHz、单声道、16-bit PCM。程序接收完整语句音频后调用 `robot.audio.play_pcm()`，等待设备确认播放完成。录音和播放依次进行，生成、播放与绘图期间不接收回答；用户应在机器人说完“请看图，说出动物名称”后回答。

每次听取回答使用独立的 ASR 任务，只接受该任务的第一个最终句子。题目带有递增版本号，旧题目的结果不能改变新题目。Jev 只返回有限类别，回复文本由游戏程序生成。

网络错误、鉴权失败、无效生成结果、设备错误、无效音频和麦克风数据丢失都会直接终止应用并输出错误。无语音属于正常等待状态。语音服务和 DeepSeek 出题会产生 API 调用费用；本机 provider 消耗对应 CLI 账户的用量。

## 验证

```bash
uv run pytest -q
uv run ruff check app.py guess_animal scripts tests
uv run watcherobot app check .
uv run python -m scripts.check_services
uv run python -m scripts.check_generation
```

`check_services` 调用真实的 TTS、ASR 和 Jev，会产生模型调用费用。它验证合成音频的识别结果，以及游戏启动、别名、错误答案、多种候选、提示、结束等中文用例。使用 `--service speech` 或 `--service jev` 可单独检查。测试音频与报告写入忽略目录 `.artifacts/`。

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

设备检查展示 09、141 和 111，并发送 111 从 0 到 100 的全部进度帧，检查设备确认。随后运行两道题目的完整流程：播报生成提示、单题生成、逐笔显示、播报完成提示，将回答文本通过真实 Jev 接口判定并播报结果。第一题检查答错后显示 141、恢复动物图案，以及答对后显示 09；第二题检查两次答错和结束游戏的行为。每次答错都会记录表情显示至恢复图案的时间，并检查恢复发生在语音播放期间；计时检查包含 1.5 秒等待和设备通信容差。它同时检查播放完成事件、麦克风音频格式、数据量和音频丢失情况。结果写入 `.artifacts/device_check.json`。这个检查会实际改变机器人屏幕并播放语音，完成后恢复待机表情。验证范围为设备指令和数据传输；回答文本由检查程序传入 Jev，图案的视觉效果和 ASR 识别效果需要单独检查。

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
| `guess_animal/judge.py` | Jev 游戏意图及答案判定 |
| `guess_animal/robot_game.py` | 麦克风、播放、显示及游戏流程 |
| `guess_animal/config.py` | 配置读取与检查 |
| `tests/test_game.py` | 游戏规则和 SDK 图案格式测试 |
| `tests/test_generator.py` | 生成格式、坐标限制、提示与去重测试 |
| `tests/test_eyes.py` | 眼睛参数的 SDK 检查 |
| `tests/test_expressions.py` | 表情资源打包、矢量几何误差及全部进度帧的 SDK 检查 |

SDK 会收集应用根目录中的 Python 模块。`app.json` 声明运行依赖，`.watcherignore` 排除测试脚本与中间文件。
