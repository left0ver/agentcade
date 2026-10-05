# 你画我猜

桌面机器人上的语音小游戏：机器人在屏幕上一笔一笔画出一种动物，你说出它的名字。动物、提示和笔画都由大模型现场生成，每局都不一样。

## 玩法

对机器人说“我们来玩你画我猜”开始。

- 每局 5 题，每题 2 次机会，答对得 1 分，同一局动物不重复
- 最近 72 小时出现过的动物、同义名称和相同图案不会重复，满 72 小时后可以再次出题；重启程序后仍然保留有效历史
- 答对时点头，答错时摇头，动作与反馈语音同时进行，完成后回到居中姿态
- 说“提示”要线索，每题 3 条，不占机会；第一次答错也会给一条
- 说“下一题”跳过，说“结束游戏”退出
- 连续 3 次、每次 20 秒没人说话，本局自动结束
- 画图要等一会儿，这段时间可以随便聊天，聊天不算答题

机器人说话和执行答题反馈动作时不收音，语音与动作都完成后恢复收音。

## 运行

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)，以及一台和电脑在同一局域网的机器人，固件支持 `expression.runtime.v3`、`expression.vector_accessory.v1`、`microphone`、`rtc.audio.full_duplex.v1`、`motion`。

```bash
cp .env.example .env              # 填入 API key
uv sync
uv run watcherobot robot setup    # 仅首次连接设备
uv run watcherobot app run .
```

按 Ctrl+C 或执行 `uv run watcherobot app stop` 停止。

## 配置

配置写在 `.env` 里，同名环境变量优先。各项含义见 `.env.example` 的注释：

| 变量 | 用途 |
| --- | --- |
| `DASHSCOPE_API_KEY` | 语音识别和语音合成（阿里云百炼 Qwen-Audio） |
| `TYPESAFE_API_KEY` | Jev，判断玩家说的是指令、答案还是闲聊 |
| `LLM_API_KEY` | OpenAI 兼容接口，闲聊和画图共用 |

`DASHSCOPE_BASE_URL` 是百炼业务空间的地址，换 key 时记得一起改。

## 闲聊和画图模型

闲聊和画图共用 `LLM_PROVIDER` 和 `LLM_MODEL_NAME`，使用相同的接口地址、凭据和请求参数：

| 值 | 说明 |
| --- | --- |
| `openai`（默认） | 通过 OpenAI SDK 调用兼容 Chat Completions 的接口 |
| `claude_code` | 本机 `claude --print`，用 CLI 已登录的账号 |
| `codex` | 本机 `codex exec`，同上 |

OpenAI 兼容接口只需填写同一组变量，例如：

```dotenv
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=填写你的密钥
LLM_MODEL_NAME=deepseek-flash
LLM_EXTRA_BODY_JSON={"thinking":{"type":"enabled"},"reasoning_effort":"low"}
```

更换服务时，将 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL_NAME` 改成目标服务的配置。接口需要支持 JSON 对象输出。`LLM_EXTRA_BODY_JSON` 默认是 `{}`，用于填写所选服务支持的可选参数，如思考模式、输出长度或服务等级。它不能覆盖模型、消息和响应格式。`LLM_TIMEOUT_SECONDS` 控制单次调用超时，默认 180 秒。

使用本机 CLI 时，在启动游戏的电脑上安装对应 CLI，并在终端完成登录。
将 `.env` 中同名的六项变量替换为下面的一组，语音和 Jev 配置继续保留。

**Codex**：执行 `codex login` 登录，使用 `codex login status` 检查登录状态。

```dotenv
LLM_PROVIDER=codex
LLM_API_KEY=
LLM_BASE_URL=
LLM_MODEL_NAME=
LLM_EXTRA_BODY_JSON={}
LLM_COMMAND=codex
```

**Claude Code**：执行 `claude auth login` 登录，使用 `claude auth status` 检查登录状态。

```dotenv
LLM_PROVIDER=claude_code
LLM_API_KEY=
LLM_BASE_URL=
LLM_MODEL_NAME=
LLM_EXTRA_BODY_JSON={}
LLM_COMMAND=claude
```

`LLM_MODEL_NAME` 留空时，闲聊和画图均使用 CLI 默认模型；填写时通过 `--model` 指定。切换到 CLI 时，将接口模式下的模型名称清空，`LLM_EXTRA_BODY_JSON` 设为 `{}`。CLI 使用自己的登录凭据。

`LLM_COMMAND` 只填写可执行文件名称或绝对路径，不包含 `exec`、`--print` 等参数，程序会自动添加。留空时按 provider 使用 `codex` 或 `claude`。如果启动游戏时找不到命令，macOS/Linux 可用 `command -v codex` 或 `command -v claude` 查询绝对路径，Windows PowerShell 可用 `(Get-Command codex).Source` 或 `(Get-Command claude).Source`，将结果填入 `LLM_COMMAND`。

修改 `.env` 后重新启动游戏。`LLM_TIMEOUT_SECONDS` 同样控制 CLI 调用超时，可根据所用模型的耗时调整。

生成失败或超时会直接报错，不会自动换 provider。每次生成的 JSON 和去重记录存在 `.artifacts/`，重启后仍然有效。

## 测试

```bash
uv run pytest -q
uv run ruff check app.py guess_animal scripts tests
uv run watcherobot app check .
```

默认执行 `tests/unit` 中的单元测试，使用真实题目和音频样本，覆盖游戏规则、提示顺序、图案校验、表情、音频处理及进程清理，不连接设备和模型服务。

E2E 会调用真实服务并产生费用。运行全部 provider 的比较测试时，使用 `LLM_PROVIDER=openai` 配置好接口，Claude Code 和 Codex 需要已经登录；测试使用两个 CLI 各自的默认模型，同时验证画图和闲聊。真机测试需要连接机器人并停止游戏，将机器人放在 Mac 电脑扬声器附近，确保它能清楚收录电脑播放的声音。

| 命令 | 检查内容 |
| --- | --- |
| `uv run pytest tests/e2e -m e2e -q` | 全部模型服务与真机 E2E |
| `uv run pytest tests/e2e/test_services.py -m e2e -q` | 三个 provider 的生成、历史保存和聊天上下文、Jev 分类、流式 TTS 与连续 ASR |
| `uv run pytest tests/e2e/test_robot.py -m e2e -q` | 真机，仅 macOS：完整五题语音游戏、提示、表情、计分、跳题、交流、结束及音频资源释放 |
| `uv run pytest tests/e2e/test_robot.py -m e2e -k expression -q` | 真机：播放语音时逐一验证进度条 0–100 的全部状态 |
| `uv run pytest tests/e2e/test_robot.py -m e2e -k answer_feedback -q` | 真机：答对点头、答错摇头、语音和表情同时反馈、动作取消与收音恢复 |
| `uv run pytest tests/e2e/test_services.py -m e2e -k codex -q` | 单独检查 Codex 绘图 |
| `uv run watcherobot app run scripts/eye_preview` | 真机：逐个预览眼型，不调模型 |

真机 E2E 通过电脑扬声器发声、机器人麦克风收音，调用生产游戏流程。它检查主动提示不消耗回答次数、答错按顺序提示、提示用完不重复、答错表情显示后恢复动物图案、答对计分、两次答错公布答案、跳题、连续聊天、播报期间暂停收音、播报后恢复，以及五题结束和生成期间退出。随后检查取消播放、再次播放和麦克风恢复。语音服务 E2E 还会持续发送 35 秒静音，再验证同一 ASR 会话识别两条独立语句。测试不使用 mock。

每次 E2E 的音频、应用日志和报告保存到 `.artifacts/tests/<测试名称>/<运行编号>/`。真机测试核对本次运行编号、应用退出状态和全部场景结果。可以追加 `--junitxml=.artifacts/tests/e2e.xml` 输出 JUnit 报告。设备未连接、模型失败、超时或断言失败都会使测试失败。图案检查覆盖格式、几何与设备确认，人眼辨识效果需要人工查看。

## 实现

语音处理路径：麦克风 → Qwen ASR → Jev 判断意图 → 游戏逻辑或 LLM 闲聊 → Qwen TTS → WebRTC 播放。

- **图案**：模型输出动物名、别名、3 条提示和笔画坐标，经 Pydantic 校验后用 SDK 的矢量配件逐笔显示。设备上限是 12 条笔画、192 个点。重名、提示里带答案、笔画和以前完全相同的结果都会被拒绝。
- **表情**：`emojis/` 里的三个 SVG 转成白色矢量笔画，分别用于生成中、答对、答错。生成中的进度条按 `DRAWING_ESTIMATE_SECONDS` 估算，完成前最多到 99%。
- **语音**：TTS 返回的 24 kHz PCM 转成 16 kHz Opus，边合成边播。播报时关闭麦克风，所有播报排队，互不打断。
- **出错**：网络、鉴权、设备或生成结果有问题时，直接退出并打印错误。

```
app.py                 SDK 入口
guess_animal/
  robot_game.py        主流程：收音、播报、显示
  game.py              出题、计分、提示
  generator.py         提示词、输出校验、去重历史
  providers.py         OpenAI 兼容接口 / Claude Code / Codex 调用
  animals.py           动物数据结构和矢量编码
  expressions.py       表情 SVG 转换和进度条
  eyes.py              待机眼睛和眼型预览参数
  judge.py             Jev 意图和答案判定
  conversation.py      LLM 闲聊和上下文
  speech.py            Qwen ASR / TTS
  audio_stream.py      PCM 流的 RTC 播放
  microphone.py        麦克风收音和播报时静音
  config.py            配置读取和检查
emojis/                表情 SVG
scripts/               眼型预览工具
tests/                 单元测试、真实服务和真机 E2E
```

## License

MIT
