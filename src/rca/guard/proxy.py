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

import json
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
class Gate:
    """**事前闸门**（D21 / M4）：在"转发之前"判定 —— 这是"真的能拦"的落点。

    M1–M3 的护栏只**判定**：`block` 是一句结论，动作照样发生。
    M4 把它变成动作面的一道闸门：判为拦的调用**不转发给上游**，
    并把结构化拒绝交给调用方（Agent 可以据此改道，而不是崩溃）。

    三条闸门规则（全部**确定性**、不看标准答案）：

      1. **打转**：同一「工具 + 参数」达到 `repeat_threshold` 次 ⇒ 拦。
         （工具层原本只是**提醒**模型；这里升级为拦 —— 提醒被无视过一次。）
      2. **次数预算**：累计工具调用超过 `max_tool_calls` ⇒ 拦（0 = 不限制）。
      3. **写类工具走策略执行点**：`write_tools` 里点名 + 提供 `policy_check` 时，
         由 `src/rca/policy/` 的 deny-first 决策说话 —— 护栏**不自己发明**一套授权，
         这是"与策略点合流"，不是再造一个。

    ⚠️ 关键不变量：**判为拦之后，上游一定不能被调用**。
       "报了拦但照样转发"是这一类实现最容易出的错（而且看起来完全正常），
       所以它有一条专门的用例 + 一个故意重演它的变异体。
    """

    max_tool_calls: int = 0
    repeat_threshold: int = 3
    write_tools: tuple[str, ...] = ()
    #: 写类工具的授权判定：`(name, args) -> (allowed, reason)`。
    #: 生产上接 `src/rca/policy/` 的策略执行点；测试里可以注入假的。
    policy_check: Callable[[str, dict], tuple[bool, str]] | None = None


def _signature(name: str, args: dict) -> str:
    """工具 + 参数的稳定签名（排序后序列化，避免键序造成"看起来不同"）。"""
    return f"{name}|{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"


def pre_action_findings(trace: Trace, name: str, args: dict, gate: Gate) -> list[Finding]:
    """转发**之前**的判定（返回非空 = 拦下这次调用）。"""
    out: list[Finding] = []

    if gate.max_tool_calls and len(trace.calls) >= gate.max_tool_calls:
        out.append(Finding(
            rule="tool_budget_exceeded",
            severity="block",
            subject=f"{len(trace.calls)}/{gate.max_tool_calls}",
            detail="工具调用次数已超过本次预算 —— 继续调用不会带来新信息",
        ))

    if gate.repeat_threshold:
        same = sum(1 for c in trace.calls
                   if _signature(c.name, _args_of(c)) == _signature(name, args))
        if same >= gate.repeat_threshold:
            out.append(Finding(
                rule="repeated_tool_call",
                severity="block",
                subject=name,
                detail=(f"同一「工具 + 参数」已经调用过 {same} 次"
                        f"（阈值 {gate.repeat_threshold}）—— 这是打转，不是深挖"),
            ))

    if name in gate.write_tools and gate.policy_check is not None:
        allowed, reason = gate.policy_check(name, args)
        if not allowed:
            out.append(Finding(
                rule="policy_denied",
                severity="block",
                subject=name,
                detail=f"策略执行点拒绝了这个写类动作：{reason}",
            ))

    return out


def _args_of(call: ToolCall) -> dict:
    """把记录下来的 `args`（可能已经是字符串）还原成 dict，用于重放签名。"""
    if isinstance(call.args, dict):
        return call.args
    try:
        loaded = json.loads(call.args or "{}")
        return loaded if isinstance(loaded, dict) else {}
    except (TypeError, ValueError):
        return {}


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
    #: 事前闸门（M4）。默认 `Gate()` = 只拦打转（阈值 3），不限制次数预算、不管写类。
    gate: Gate = field(default_factory=Gate)

    trace: Trace = field(init=False)
    submissions: list[Submission] = field(default_factory=list, init=False)
    #: 被拦下的调用次数（M4 的账，报告里要能看见"护栏真的拦了几次"）
    blocked: int = field(default=0, init=False)
    #: 最近一次被拦的原因（诊断用）
    last_blocked: list[Finding] = field(default_factory=list, init=False)
    #: 单调递增的事件序号（**每个动作一个号**：一次工具调用+它的返回共用一个号，
    #: 每次交卷一个号）。见 `_next_step` 的说明。
    _step: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.trace = Trace(label=self.label)

    def _next_step(self) -> int:
        """下一个事件序号。

        ⚠️ 必须**单调递增**，不能用"列表长度"推。自审时验证过：用长度推的话，
           "交卷之后再调工具"会算出已经用过的号（实测 `[1,1,2,2,2,4]` ——
           第二次调用撞上了第一次交卷的 2），于是**证据指针会指到错的事件上**。
           真实 Agent 完全可能先交卷、再复查证据、再交一次。
        """
        self._step += 1
        return self._step

    # ---------------------------------------------------------- 工具面
    def names(self) -> list[str]:
        if self.tool_names is not None:
            return list(self.tool_names())
        return []

    def call(self, name: str, args: dict | None = None) -> dict:
        """先过**事前闸门**，通过才转发；并把"问了什么 / 回了什么"记进事件流。

        ⚠️ 两条不能动摇的性质：
          1. **判为拦就不转发** —— 上游工具箱**一次都不能被调用**（有用例专门钉住）；
          2. **拦住也要留痕**：记下"它要调"和"被拦"，所以判定规则（M1）仍然看得到
             这条路径上发生过什么 —— 拦掉的动作不该从证据里消失。
        """
        args = dict(args or {})
        # ⚠️ 顺序不能反：**先判定、再记录**。先记录的话，这次调用自己就会被算进
        #    "已经调过几次"里 ⇒ 阈值 1 时第一次调用就被拦（实测被用例抓住）。
        findings = pre_action_findings(self.trace, name, args, self.gate)
        step = self._next_step()
        # 参数存成**规范 JSON**（不是 repr）：重复调用的判定要能原样解析回来
        self.trace.add(ToolCall(step=step, name=name,
                                args=json.dumps(args, sort_keys=True, ensure_ascii=False),
                                role="external"))

        if findings:
            detail = "；".join(f"{f.rule}：{f.detail}" for f in findings)
            text = f"[护栏拦截] 这次调用没有被执行。{detail}"
            self.trace.add(ToolResult(step=step, name=name, text=text, ok=False, role="external"))
            self.blocked += 1
            self.last_blocked = findings
            return {
                "ok": False,
                "blocked": True,
                "text": text,
                "findings": [
                    {"rule": f.rule, "severity": f.severity, "subject": f.subject,
                     "detail": f.detail, "evidence": list(f.evidence)}
                    for f in findings
                ],
            }

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
        step = self._next_step()
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
            "n_blocked": self.blocked,                 # ★ M4：真的拦了几次
            "submissions": [s.to_dict() for s in self.submissions],
            "final_verdict": self.final_verdict.verdict,
            "n_findings": len(self.final_verdict.findings),
        }


__all__ = ["GuardedToolFace", "Submission", "Finding"]
