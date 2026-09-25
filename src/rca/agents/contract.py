"""输出契约的补救：模型没吐 JSON 时，**催一次**再放弃。

============================ 为什么需要它 ============================

2026-09-25 把任务改成"列出**所有**异常及其根因"之后，
baseline 的 JSON 解析成功率从 100% 掉到 **67%**（21 次里 2 次没给出干净 JSON）。

其中一次的原文是一整段英文散文（"I now have enough evidence. Let me finalize
the analysis. **Issue A — ...**"）—— 结论其实是对的，但**没有遵守输出契约**。

============================ 为什么这不是小事 ============================

解析失败时，评分只能拿**未经约束的原始文本**去判 —— 而那里面混着模型的
**推理过程**，不是它的**结论**。"在推理里提到过某个词"和"结论里主张某件事"
是两件事（这正是 #16/#21 两次假阳性的根源）。

⇒ **解析失败会让测量口径悄悄变松**，而不是仅仅"丢了一次数据"。

============================ 做法 ============================

不重写 prompt、不加大预算，只加**一次**补救：

    模型给出最终回复 → 抠不出 JSON → 追加一条"你上一条不是合法 JSON，
    请只输出 JSON 对象" → 再给一次机会（**最多一次**，之后按原样接受）

放在这里而不是各 Agent 里，是为了**三个调用点共用同一份逻辑**：
baseline、专职 Agent、交叉质证 —— 复制三份迟早会走散
（本项目已经因为"两份判定不一致"栽过一次，见 eval/scenarios.py 的注释）。
"""

from __future__ import annotations

from .baseline import extract_json

# 催促语。刻意**只说格式**，不说内容 —— 免得影响它下什么结论。
JSON_REPAIR_NUDGE = (
    "你上一条回复不是合法的 JSON（可能是写了说明文字或代码块标记）。\n"
    "请**只输出那个 JSON 对象本身**，不要任何解释、不要代码块、不要多余文字。"
)


def needs_json_repair(text: str) -> bool:
    """这段回复是不是没有给出可解析的 JSON 结论。"""
    return extract_json(text) is None


def repair_messages(messages: list[dict], raw_text: str) -> list[dict]:
    """构造"催促一次"的消息序列（在原对话后面追加两轮）。

    返回**新的列表**，不原地改传入的 messages ——
    调用方要能自由决定用不用它。
    """
    return [
        *messages,
        {"role": "assistant", "content": raw_text},
        {"role": "user", "content": JSON_REPAIR_NUDGE},
    ]
