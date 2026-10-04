# 你画我猜

对桌面机器人说“我们来玩你画我猜”，屏幕会逐步画出一种动物。直接说动物名称即可回答。

每局五道题目，由 DeepSeek 逐题生成，同一局的动物不会重复。每题有两次回答机会，答对获得一分。可以说“提示”“下一题”“结束游戏”。识别不明确时会请用户重新回答，不消耗回答次数。游戏中连续三次、每次二十秒没有回答，会结束当前游戏并等待下一次开始指令。

动物名称、认可别名、提示和笔画坐标均由 `deepseek-flash` 动态生成。图案使用 SDK 的自定义矢量配件，与机器人原有眼睛共同组成动物面部；每种图案逐笔显示。

程序通过 OpenAI SDK 的 `AsyncOpenAI.chat.completions.create()` 调用 DeepSeek 官方兼容接口，使用 JSON Output 和 Pydantic 检查输出。生成请求包含不同的构图要求、请求标识及近期动物名称。每幅图案必须符合坐标、笔画和总点数限制，重复名称、别名冲突、提示中包含答案及完全相同的笔画数据都会在发送设备前被拒绝。

应用启动时播放开始游戏的说明，收到开始指令后生成第一题。每道题目生成前，机器人会说“正在生成第几题，大约需要三十秒。准备好了我会告诉你，请稍等。”，播报完成后调用 DeepSeek。图案逐笔显示完成后，再说“第几题画好了！请看图，说出动物名称。”，播报完成后开始接收回答。答对、用完回答机会或跳过当前题目后，才生成下一题；最后一题结束后播报成绩。

三次真实单题生成分别耗时约 18、20、27 秒，语音提示中的三十秒还考虑了绘图和完成提示的时间。实际时间会随服务情况变化。每次生成的 JSON 与去重记录保存在 `.artifacts/`；重新启动应用仍会读取近期动物与图案记录。

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
| `DEEPSEEK_API_KEY` | DeepSeek API key |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | `deepseek-flash` |
| `DASHSCOPE_BASE_URL` | 用户提供的北京业务空间地址 |
| `QWEN_ASR_MODEL` | `qwen-audio-3.1-asr-flash-streaming` |
| `QWEN_TTS_MODEL` | `qwen-audio-3.1-tts-flash` |
| `QWEN_TTS_VOICE` | `yuxiaoyun_v3.1` |
| `JEV_MODEL` | `jev-latest` |
| `JEV_CONFIDENCE_THRESHOLD` | `0.65`，低于阈值时不接受判定 |

ASR 与 TTS 使用 Qwen-Audio 的 WebSocket 协议。程序从 `DASHSCOPE_BASE_URL` 提取主机名称，并使用 `/api-ws/v1/inference` 路径。模型与音色必须匹配，同系列的参数配置需要符合对应模型文档。

## 语音处理

等待游戏指令和回答时，机器人麦克风通过 SDK 输出 16 kHz、单声道、16-bit PCM，发送给 ASR。程序只处理句子结束后的识别结果，再由 Jev 判断游戏指令及答案是否正确。ASR 热词使用游戏控制指令，动物名称由 ASR 自由识别。

机器人反馈使用 24 kHz、单声道、16-bit PCM。程序接收完整语句音频后调用 `robot.audio.play_pcm()`，等待设备确认播放完成。录音和播放依次进行，生成、播放与绘图期间不接收回答；用户应在机器人说完“请看图，说出动物名称”后回答。

每次听取回答使用独立的 ASR 任务，只接受该任务的第一个最终句子。题目带有递增版本号，旧题目的结果不能改变新题目。Jev 只返回有限类别，回复文本由游戏程序生成。

网络错误、鉴权失败、无效生成结果、设备错误、无效音频和麦克风数据丢失都会直接终止应用并输出错误。无语音属于正常等待状态。语音服务和 DeepSeek 出题都会产生 API 调用费用。

## 验证

```bash
uv run pytest -q
uv run ruff check app.py guess_animal scripts tests
uv run watcherobot app check .
uv run python -m scripts.check_services
uv run python -m scripts.check_generation
```

`check_services` 调用真实的 TTS、ASR 和 Jev，会产生模型调用费用。它验证合成音频的识别结果，以及游戏启动、别名、错误答案、多种候选、提示、结束等中文用例。使用 `--service speech` 或 `--service jev` 可单独检查。测试音频与报告写入忽略目录 `.artifacts/`。

`check_generation` 连续三次调用 DeepSeek，每次只生成一道题目，记录耗时并验证动物去重、图案去重和 SDK 矢量格式。Jev 检查中的动物答案也由 DeepSeek 真实生成。单元测试中的 JSON 样本来自真实生成结果，保存在 `tests/fixtures/`。

连接机器人并停止游戏应用后，可以执行设备检查：

```bash
uv run watcherobot app run scripts/device_check
```

设备检查运行两道题目的完整流程：播报生成提示、单题生成、逐笔显示、播报完成提示，将生成的动物名称通过真实 Jev 接口判定并播报结果，然后生成下一题。它检查逐题状态、播放完成事件和最后一题之后的结束行为，再检查麦克风音频格式、数据量和音频丢失情况。结果写入 `.artifacts/device_check.json`。这个检查会实际改变机器人屏幕并播放语音，完成后恢复待机表情。它验证设备指令和数据传输，不评价图案的视觉辨识效果；回答文本由检查程序传入 Jev。

## 文件

| 文件 | 用途 |
| --- | --- |
| `app.py` | SDK Application 入口 |
| `guess_animal/animals.py` | 动物数据结构、矢量编码与屏幕限制 |
| `guess_animal/generator.py` | DeepSeek 出题、输出检查与生成历史 |
| `guess_animal/game.py` | 出题、回答次数、计分和题目切换 |
| `guess_animal/speech.py` | Qwen ASR 与 TTS |
| `guess_animal/judge.py` | Jev 游戏意图及答案判定 |
| `guess_animal/robot_game.py` | 麦克风、播放、显示及游戏流程 |
| `guess_animal/config.py` | 配置读取与检查 |
| `tests/test_game.py` | 游戏规则和 SDK 图案格式测试 |
| `tests/test_generator.py` | 生成格式、坐标限制、提示与去重测试 |

SDK 会收集应用根目录中的 Python 模块。`app.json` 声明运行依赖，`.watcherignore` 排除测试脚本与中间文件。
