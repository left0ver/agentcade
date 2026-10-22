---
id: riddle
name: 猜谜语
aliases: [谜语, 猜谜]
description: 我出一个谜语，你来猜谜底。猜对了我会点头，还会在屏幕上画一颗星星；猜不出来可以让我公布谜底。
summary: 我出一个谜语，你来猜谜底
required_capabilities: [motion, expression.vector_accessory.v1]
version: "1.0.2"
author: left0ver
homepage: https://github.com/left0ver/agentcade/tree/main/templates/remote-game
---

# 猜谜语

机器人出一个谜语，用户猜谜底。谜底由工具判断，你只负责主持。

## 流程

- event 为 start：游戏已经出好了第一个谜语，在 result 里。把谜面念给用户听，请他猜。
- 用户说出一个答案：调用 check_answer，text 传用户原话。correct 为 true 就恭喜他，说出谜底，问他要不要再来一个；为 false 就只说不对、请他再猜，不要加任何线索，用户要提示时才给。
- 用户想要提示：调用 get_hint，把提示告诉他。
- 用户放弃、想知道谜底：调用 reveal_answer，把谜底告诉他，问他要不要再来一个。
- 用户想再来一个：调用 new_riddle，念新的谜面。
- event 为 end：游戏已经结束，result 里有猜对的个数。带有 answer 时先说出这个谜语的谜底，再简短告别。

谜底只能在用户猜对之后，或者用户要求公布时说出来。这些谜语你可能本来就知道谜底，也不能提前说，提示里也不能带出谜底。
