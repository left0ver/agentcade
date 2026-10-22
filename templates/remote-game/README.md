---
title: 猜谜语
emoji: 🧩
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# 猜谜语：Agentcade 远程游戏模板

一个可以单独部署的 Agentcade 游戏。部署好之后，任何人在小懒的设置页面里粘贴它的地址，确认权限，就能对机器人说“我想玩猜谜语”。

怎么写游戏、怎么测试、有哪些要求，见[游戏开发指南](https://github.com/left0ver/agentcade/blob/main/docs/game-development.md)。

开头那几行是 Hugging Face Space 的配置，部署到别的地方时可以删掉。

## 文件

| 文件 | 内容 |
| --- | --- |
| `riddle/SKILL.md` | 游戏声明和流程。`id` 和目录名一致；`summary` 是开机问候里念的简短介绍（30 字以内，句末不带标点）；`required_capabilities` 声明要让机器人做什么；`version`、`author`、`homepage` 会显示在安装确认页上 |
| `riddle/agent.py` | 入口类 `GameAgent`：工具、状态，用 `self.act(...)` 发设备指令 |
| `riddle/settings.yaml`、`riddle/config.py` | 游戏读取的配置项 |
| `Dockerfile` | 安装依赖，运行 `agentcade-game serve` |

改成你自己的游戏：把 `riddle` 目录改成你的游戏 ID（小写字母、数字和下划线），同时改 `SKILL.md` 里的 `id`、`author`、`homepage`，`agent.py` 里的导入，以及 `Dockerfile` 里的两处 `riddle`。

## 本地调试

```bash
pip install -r requirements.txt
cp riddle/.env.example riddle/.env       # 填写大模型的 API Key

agentcade-game chat riddle               # 文字模拟器：不用机器人，打字和游戏对话
agentcade-game check riddle              # 合规检查：名片和完整的一局
agentcade-game serve riddle --port 7860  # 运行服务，小懒的设置页面里填 http://127.0.0.1:7860
```

## 部署到 Hugging Face Space

从 2026 年 7 月起，免费账号不能再建 Docker 类型的 Space，要订阅 PRO。

1. 在 Hugging Face 新建一个 Space，类型选 Docker。
2. 把这个目录推到 Space 的仓库。
3. 在 Space 的 Settings → Variables and secrets 里添加 Secret `LLM_API_KEY`，需要的话再加 `LLM_BASE_URL`、`LLM_MODEL_NAME`。
4. 构建完成后，地址是 `https://<用户名>-<Space 名>.hf.space`。名片里的对外地址会自动用这个地址，不用设置 `PUBLIC_URL`。
5. 运行 `agentcade-game check https://<用户名>-<Space 名>.hf.space`，通过后把地址发给用户，或者提交到游戏目录。

免费的 Space 长时间没人访问会休眠，第一次连接要等它启动。

## 部署到自己的服务器

最简单的是直接用 IP 和端口，在防火墙或安全组里放行 7860 端口：

```bash
docker build -t riddle .
docker run -d --restart unless-stopped -p 7860:7860 \
  -e PUBLIC_URL=http://<服务器 IP>:7860 \
  -e LLM_API_KEY=... riddle
```

用户在设置页面填 `http://<服务器 IP>:7860`。http 不加密，用户玩游戏时说的话会明文传输，安装时设置页面会提醒。

有域名的话建议配上 HTTPS：端口只绑在本机，`PUBLIC_URL` 改成 https 地址。

```bash
docker run -d --restart unless-stopped -p 127.0.0.1:7860:7860 \
  -e PUBLIC_URL=https://games.example.com/riddle \
  -e LLM_API_KEY=... riddle
```

前面用 Caddy 或 Nginx 提供 HTTPS，并且不要缓冲 SSE 流式响应，请求超时设到 5 分钟以上。Caddy 示例：

```
games.example.com {
    handle_path /riddle/* {
        reverse_proxy 127.0.0.1:7860 {
            flush_interval -1
        }
    }
}
```

`PUBLIC_URL` 要和用户填写的地址完全一致，名片里的消息地址就是 `PUBLIC_URL` 加 `/a2a`。

### 国内服务器

Dockerfile 默认用国内的镜像源：基础镜像从 DaoCloud 拉，Python 依赖从清华镜像装，agentcade 的源码经 gh-proxy.com 从 GitHub 下载，直接 `docker build -t riddle .` 就行。

这些镜像站时常变化，哪个不能用了就换一个，例如：

```bash
docker build -t riddle \
  --build-arg PYTHON_IMAGE=<别的 Docker 镜像源>/library/python:3.11-slim \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
  --build-arg GITHUB_PROXY=https://gh-proxy.org/ .
```

海外部署时换回官方源：`--build-arg PYTHON_IMAGE=python:3.11-slim --build-arg PIP_INDEX_URL=https://pypi.org/simple --build-arg GITHUB_PROXY=`。GitHub 镜像站是第三方服务，源码经过它转发，介意的话自己部署一个 gh-proxy。

## 提交到游戏目录

想让别人在小懒设置页面的“发现更多游戏”里直接看到它：`check` 全部通过后会打印一条目录条目，把它加进 [agentcade 仓库](https://github.com/left0ver/agentcade) 的 `catalog/games.json`，提一个 PR。提交要求和审核标准见仓库里的 `catalog/README.md`。

游戏以后改了版本或者要的能力，用户那里会显示“当前版本未经审核”，要再提一次 PR 更新条目。

## 要求

- 地址可以是 https 域名，也可以是 `http://IP:端口`。公网地址用 http 时传输不加密，用户安装时会看到提醒。
- 一个服务可以同时给很多人玩，每一局按 `context_id` 分开。超过 30 分钟没有输入的局会自动关闭，同时进行的局数默认最多 32 局。
- 游戏用的大模型和其他服务都由你自己付费，用户不需要提供密钥。
- 用户在玩这个游戏时说的话会发到你的服务器上，请妥善对待。
