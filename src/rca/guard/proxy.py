"""护栏代理：把**工具面**与**结论面**收在一处（D21 / M3）。

============================ 它在平台里的位置 ============================

   外部 Agent  →  本代理（记录 + 判定）  →  真实工具
                     │
                     └─ submit_conclusion(结论文本) → 跑规则 → 把判定回给 Agent

ADR-0007 决定 3 选的就是这个形态，理由：**既能拦、又不要求对方改代码**。
本模块是它的**框架无关核心**；MCP 线路层（把它挂到 `MCPServer` 上）是薄薄一层适配，
复用仓库里已有的 `src/rca/mcp_server.py` / `mcp_client.py` 模式。

============================ 为什么结论必须单独回传 ============================

MCP 流量只覆盖**行动面**（工具调用与返回）—— 它**看不到** Agent 最后那句结论。
而"这个数字没有依据""这个指标名是编的"这类判断，恰恰要用结论去对照证据
（M1 实测过：归档轨迹里结论文本不在 trace 里，得从 results.json 单独取）。

⇒ 接口形状**现在**就定下来（ADR-0007 的原话），否则后面所有规则都会卡在这里：

    submit_conclusion(text: str) -> {
        "verdict": "allow" | "warn" | "block",
        "findings": [{"rule", "severity", "subject", "detail", "evidence"}],
        "submission_no": int,        # 第几次交卷（允许改完再交）
    }

返回给 Agent 是**刻意的**：护栏的价值在于让它在交卷前知道"你这条证据在哪"。
（M1 的硬规则只判、不拦；真正阻断工具调用是 M4。）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .events import Claim, ToolCall, ToolResult, Trace
from .rules import Finding, run_rules
from .verdict import Verdict, judge


@dataclass
class Submission:
    """一次交卷的记录（同一个 Agent 可以改完再交，但每次都被记下来）。"""

    no: int
    text: str
    verdict: Verdict

    def to_dict(self) -> dict:
        return {
            "submission_no": self.no,
            "verdict": self.verdict.verdict,
            "findings": [
                {"rule": f.rule, "severity": f.severity, "subject": f.subject,
                 "detail": f.detail, "evidence": list(f.evidence)}
                for f in self.verdict.findings
            ],
        }


@dataclass
class GuardedToolFace:
    """外部 Agent 能看到的那一面：工具 + 一个 `submit_conclusion`。

    `toolbox` 只要求"有 `call(name, args)` 方法"（自家 `ToolBox` 就是），
    因此代理**不认识任何框架类型** —— 这正是平台化需要的形状。
    """

    toolbox: Any
    label: str = ""
    #: 工具名清单（交给对方去暴露；代理不关心它们怎么被描述）
    tool_names: Callable[[], list[str]] | None = None

    trace: Trace = field(init=False)
    submissions: list[Submission] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.trace = Trace(label=self.label)

    # ---------------------------------------------------------- 工具面
    def names(self) -> list[str]:
        if self.tool_names is not None:
            return list(self.tool_names())
        return []

    def call(self, name: str, args: dict | None = None) -> dict:
        """转发一次工具调用，并把"问了什么 / 回了什么"记进事件流。

        ⚠️ **上游报错也要记进事件流**（`ok=False` + 错误原文）：
           护栏判"这条主张有没有证据"靠的就是这串事件；
           把失败悄悄吞成空字符串，会让"工具坏了"看起来像"工具说什么都没有" ——
           那是 #26 的同族错误（空绿）。
        """
        args = dict(args or {})
        step = len(self.trace.calls) + 1
        self.trace.add(ToolCall(step=step, name=name, args=repr(args), role="external"))
        try:
            result = self.toolbox.call(name, args)
            text = getattr(result, "text", None)
            if text is None:
                text = str(result)
            ok = bool(getattr(result, "ok", True))
        except Exception as exc:                      # noqa: BLE001 —— 见上：要记录，不要吞掉
            text, ok = f"{type(exc).__name__}: {exc}", False
        self.trace.add(ToolResult(step=step, name=name, text=text, ok=ok, role="external"))
        return {"ok": ok, "text": text}

    # ---------------------------------------------------------- 结论面
    def submit_conclusion(self, text: str) -> dict:
        """交卷：记下结论、跑规则、把判定回给 Agent。

        ⚠️ **只判这一次交卷**（工具事件 + 最新这条结论），理由是被实测逼出来的：
           第一版若把"所有 `final` 结论"都拿去判，那么被否掉的第一稿会**一直留在轨迹里**，
           Agent 改完再交也永远洗不清（`test_agent_may_fix_and_resubmit_*` 当场变红）。
           旧稿仍然完整留档在 `submissions` 里 —— 看得见，但不参与本次判定。
        """
        text = str(text or "")
        step = len(self.trace.calls) + len(self.submissions) + 1
        self.trace.add(Claim(step=step, text=text, kind="final", phase="submit"))

        judged = Trace(
            label=self.trace.label,
            events=[e for e in self.trace.events
                    if not (isinstance(e, Claim) and e.kind == "final")],
        )
        judged.add(self.trace.final_claims[-1])

        verdict = judge(run_rules(judged))
        self.submissions.append(Submission(no=len(self.submissions) + 1, text=text, verdict=verdict))
        return self.submissions[-1].to_dict()

    # ---------------------------------------------------------- 报告
    @property
    def final_verdict(self) -> Verdict:
        """最后一次交卷的判定（没有交过卷时给出 `missing_conclusion` 那条）。"""
        if self.submissions:
            return self.submissions[-1].verdict
        return judge(run_rules(self.trace))

    @property
    def n_tool_calls(self) -> int:
        return len(self.trace.calls)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "n_tool_calls": self.n_tool_calls,
            "submissions": [s.to_dict() for s in self.submissions],
            "final_verdict": self.final_verdict.verdict,
            "n_findings": len(self.final_verdict.findings),
        }


__all__ = ["GuardedToolFace", "Submission", "Finding"]
