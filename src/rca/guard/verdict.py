"""判定聚合 + 审计留痕（D21 / M1）。

============================ 三档判定 ============================

    allow   没有发现形式缺陷
    warn    有可疑之处，但**不拦**（交给软层提醒 / 交给人看）
    block   硬缺陷（如"结论点名了一个轨迹里根本不存在的标识符"、或"没有结论事件"）

⚠️ M1 **只判定、不拦截** —— `block` 目前是一句结论，不是一次阻断。
   真正阻断工具调用是 M4 的事（要和 `src/rca/policy/` 的策略点合流）。
   现在就把话说清楚，免得文档里写着"拦截"而代码里没有。

============================ 为什么用自己的审计账本 ============================

`runs/_guard.ndjson`，**不复用** `runs/_audit.ndjson`：
后者的读方（`policy/audit.py` 的 `denials()`）把每条记录当成**动作判定**，
把"判断发现"混进去会让"哪些动作被拒了"这个问题的答案变脏。
一份账本只放一类事实 —— 这是单一职责，不是重复建设。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .rules import Finding

GUARD_AUDIT_PATH = Path("runs") / "_guard.ndjson"

#: block 比 warn 严重；allow 最小
_SEVERITY_ORDER = {"warn": 1, "block": 2}


@dataclass(frozen=True)
class Verdict:
    verdict: str                       # allow / warn / block
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        return self.verdict == "allow"

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.rule] = out.get(f.rule, 0) + 1
        return out

    def lines(self) -> list[str]:
        return [f.line() for f in self.findings]


def judge(findings: list[Finding] | tuple[Finding, ...]) -> Verdict:
    """把发现聚合成一档判定。空列表 ⇒ allow（但"没有结论事件"会**显式**报 block）。"""
    findings = tuple(findings)
    if not findings:
        return Verdict("allow", ())
    worst = max(_SEVERITY_ORDER.get(f.severity, 0) for f in findings)
    return Verdict("block" if worst >= 2 else "warn", findings)


def append_audit(verdict: Verdict, *, label: str, path: Path | None = None) -> int:
    """把一次判定写进护栏账本，返回写入条数。

    ⚠️ `allow` **不写**（否则账本会被"什么都没发生"淹没）；
       有发现才写 —— 账本是给"回头看发生了什么"用的。
    """
    target = path or GUARD_AUDIT_PATH
    if verdict.ok:
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().isoformat(timespec="milliseconds")
    written = 0
    with target.open("a", encoding="utf-8") as fh:
        for f in verdict.findings:
            fh.write(json.dumps({
                "ts": ts,
                "type": "guard_finding",
                "actor": "guard",
                "label": label,
                "rule": f.rule,
                "severity": f.severity,
                "subject": f.subject,
                "detail": f.detail,
                "evidence": list(f.evidence),
            }, ensure_ascii=False) + "\n")
            written += 1
        fh.flush()
    return written
