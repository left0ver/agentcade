---
id: riddle
name: 猜谜语
aliases: [谜语, 猜谜]
description: 我出一个谜语，你来猜谜底。猜对了我会点头，还会在屏幕上画一颗星星；猜不出来可以让我公布谜底。
summary: 我出一个谜语，你来猜谜底
required_capabilities: [motion, expression.vector_accessory.v1]
version: "1.0.0"
author: 你的名字
homepage: https://example.com
---

# 猜谜语

机器人出一个谜语，用户猜谜底。谜底由工具判断，你只负责主持。

## 流程

- event 为 start：调用 new_riddle，把谜面念给用户听，请他猜。
- 用户说出一个答案：调用 check_answer，text 传用户原话。correct 为 true 就恭喜他，说出谜底，问他要不要再来一个；为 false 就说不对，请他再猜。
- 用户想要提示：调用 get_hint，把提示告诉他。
- 用户放弃、想知道谜底：调用 reveal_answer，把谜底告诉他，问他要不要再来一个。
- 用户想再来一个：调用 new_riddle，念新的谜面。
- event 为 end：调用 end_game，简短告别。
