"""
遥测层：把被诊断系统吐出的原始数据，变成 Agent 看得懂的少量上下文。

============================ 这一层为什么存在 ============================

需求假设 A1：单次故障的原始遥测（3–8 万行日志）**远超单个上下文容量**。

所以必须先降维。但**降维方式的选择，决定了整个项目的成败**：

  ❌ 用 LLM 摘要
     结果漂亮，但不可验证 —— 它可能悄悄丢掉"某条 ERROR 只出现过一次"
     这种低熵、高价值的信号。而且每次摘要结果不同，评测无法复现。

  ✅ 确定性结构化降维（本项目采用）
     解析 → 模板提取 → 按严重级别聚合 → 时间线。
     **可审计**：同一份输入永远得到同一份输出，且能证明"什么都没丢"。

============================ 关键设计：按【严重级别】保留，不按【频次】============================

朴素做法是"取出现次数最多的 N 条"。这会漏掉最要命的东西：

    一条 ERROR 只出现 1 次，而一条 INFO 出现 5 万次 ——
    按频次排序，那条 ERROR 会被挤出去。

所以本层的规则是：

    所有 ERROR 模板   → **无条件全部保留**（哪怕只出现 1 次）
    所有 WARNING 模板 → 保留
    INFO 模板         → 只保留前 N 条（按频次）

这条规则是"关键信号不丢"的**机制保证**，不是靠调参碰运气。
"""

from .models import LogRecord, ReducedView, TemplateStat, TimelineBucket
from .parse import parse_log_line, parse_prometheus, template_of
from .reduce import reduce_logs

__all__ = [
    "LogRecord",
    "ReducedView",
    "TemplateStat",
    "TimelineBucket",
    "parse_log_line",
    "parse_prometheus",
    "template_of",
    "reduce_logs",
]
