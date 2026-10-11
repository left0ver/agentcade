---
id: guess_animal
name: 你画我猜
aliases: [猜动物, 画画猜动物]
description: 我来画动物，你看图猜名字。一局五题，每题有两次机会。想不出来可以要提示，也可以跳过，随时都能结束游戏。
required_capabilities: [expression.vector_accessory.v1, motion]
---

# 你画我猜

机器人在屏幕上画一只动物，用户看图猜名称。

## 规则

- 一局五题，每题两次回答机会，答对得一分。
- 每题有三条提示，要提示不占回答机会。
- 可以跳过当前题目，跳过时公布答案并直接进入下一题；跳过最后一题时整局结束。
- 画一题需要等一会儿，画图前要先告诉用户。

## 流程

### 开局（event 为 start）

1. 用 say 告诉用户马上开始画，请他稍等。
2. 调用 show_progress 显示进度，再调用 start_game 生成第一题。
3. 调用 show_drawing 展示图案，然后请用户猜。

### 用户作答

用户明确说出一个动物名称时调用 submit_answer，text 传用户的完整原文。答案由工具判定，不要自己判断对错。

- outcome 为 correct：恭喜用户，可以用 move_head 点头或者 show_expression 显示 happy。
- outcome 为 incorrect 且没有 answer：告诉用户不对，还有一次机会，可以调用 get_hint 送一条提示。
- outcome 为 incorrect 且带有 answer：机会用完，公布答案，可以用 move_head 摇头。
- outcome 为 unclear：没有听出是哪种动物，请用户再说一次，不占机会。

一题结束后请用户说“下一题”继续。题目还没结束时，显示过表情后要调用 show_drawing 恢复图案。

### 提示、跳过和下一题

- 用户要提示：调用 get_hint，把提示告诉用户；outcome 为 hints_exhausted 时说明提示已经用完。
- 用户表示不知道或犹豫时，先鼓励或者给提示，不擅自跳题。
- 题目还没结束，用户明确要求跳过、下一题或公布答案：调用 skip_question。它会自己把答案告诉用户、显示进度并画好下一题，结果里的 said 是已经说过的话，不要重复；outcome 为 question_ready 时调用 show_drawing 展示新题，请用户猜。跳过的是最后一题时整局结束，按“结束”处理。
- 题目已经结束，用户要求下一题：先用 say 请用户稍等，再调用 show_progress 和 next_question，然后调用 show_drawing 展示图案，请用户猜。
- start_game、next_question 或 skip_question 的 outcome 为 generation_failed：这题没有画出来，向用户道歉，请他说“下一题”再试一次；这时不要调用 show_drawing。

### 结束

- 第五题结束后整局自动结束，这时工具结果的 phase 为“已结束”：结果带有 answer 时先公布这一题的答案，再告诉用户得分并告别，调用 clear_display。
- event 为 end：调用 end_game，告诉用户得分并告别，调用 clear_display。结果带有 answer 时顺便公布这一题的答案，没有就不提答案。
