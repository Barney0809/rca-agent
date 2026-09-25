"""护栏（运行时监督）—— D21 / M1。

**定位：这不是第二个裁判。**
裁判判"结论对不对"（评分路径，可以看标准答案）；
护栏判"过程有没有形式缺陷"（运行路径，**绝不许**看答案 —— 见 `docs/adr/0007`）。

入口：

    from rca.guard import Trace, ToolCall, ToolResult, Claim, run_rules, judge

    trace = Trace(label="F4 r3")
    trace.add(ToolCall(...)); trace.add(ToolResult(...))
    trace.add(Claim(step=99, text="结论原文", kind="final"))
    verdict = judge(run_rules(trace))
    verdict.verdict      # allow / warn / block
"""

from __future__ import annotations

from .events import Claim, Event, ToolCall, ToolResult, Trace, identifiers_in, numbers_in
from .rules import (
    DIAGNOSTIC_RULES,
    RULES,
    Finding,
    inject_form_defect,
    run_diagnostics,
    run_rules,
)
from .verdict import Verdict, append_audit, judge

__all__ = [
    "DIAGNOSTIC_RULES",
    "RULES",
    "Claim",
    "Event",
    "Finding",
    "ToolCall",
    "ToolResult",
    "Trace",
    "Verdict",
    "append_audit",
    "identifiers_in",
    "inject_form_defect",
    "judge",
    "numbers_in",
    "run_diagnostics",
    "run_rules",
]
