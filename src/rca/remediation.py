"""自动修复闭环：**提案 → 人审批 → 执行 → 验证**（D21 / M5）。

============================ 它接的是哪一段 ============================

    M1 判据（结论有没有形式缺陷）
    M2 软提醒（一次自我修正）
    M3 MCP 代理（外部 Agent 的接入面）
    M4 事前闸门（真的能拦）
    M5 **本模块**：从"诊断出根因"走到"真的把系统改回来"，并且**每一步可核对**

============================ 三条铁律（都比"能自动修复"更重要）============================

1. **提案必须有据**：动作只能来自**变更记录**（把被注入的旋钮改回 `from` 值），
   不许凭空发明一个"修法"。一个自己猜修法的 Agent 比一个不修的 Agent 危险得多。
2. **只能提案可逆动作**（ADR-0006）：只允许 `set_knobs`（可逆、且**每次都过策略执行点**），
   **绝不**提案删除/隔离区动作。不可逆的能力不该长在这条链路上。
3. **验证不许在缺少观测时说"通过"**：没有事后观测就如实报 `unverified`
   —— "没测出来"和"测出来是好的"必须分开（#26 的空绿）。

⚠️ 本模块**不自己发任何 HTTP、也不直接碰运维工具箱**：
   执行一律通过**那扇被批准的 MCP 门**（`rca.mcp_client.call_ops_tool` →
   `src/rca/mcp_server.py` → 运维工具箱 → 策略执行点）。
   这样"无旁路保证"（FR-2.1）与"每次都过策略点"同时成立 ——
   只走工具箱只保证后者，**不保证前者**（写第一版时我把这两件事混成了一件，
   结构性守卫当场把我拦下来了：它在我的文档字符串里认出那个类名，判定为"第三条门"）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

REMEDIATION_AUDIT_PATH = Path("runs") / "_remediation.ndjson"

#: 允许提案的动作白名单 —— **只有可逆的**（ADR-0006：不存在硬删除）
REVERSIBLE_ACTIONS = ("set_knobs",)


def _mcp_ops_call(name: str, arguments: dict) -> dict:
    """默认执行通道：**经 MCP 客户端**调 ops 工具（被点名的那扇 Agent 门）。

    延迟 import：`mcp_client` 会拉起 stdio 子进程，测试里不该被无谓地拖进来。
    """
    from rca.mcp_client import call_ops_tool

    import asyncio

    return asyncio.run(call_ops_tool(name, arguments))


@dataclass(frozen=True)
class Proposal:
    """一条"把系统改回去"的提案。

    名字刻意叫 `current_value` / `target_value`（而不是 from/to）：
    变更记录里的 `from` 是**原来**的值（要改回的目标），`to` 是**现在**的值。
    反过来的话，读代码的人一定会搞错该把哪个值填进动作里。
    """

    service: str
    knob: str
    current_value: str          # 现在是什么（变更记录里的 to）
    target_value: str           # 要改回什么（变更记录里的 from）
    source: str
    action: str = "set_knobs"

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "service": self.service,
            "knob": self.knob,
            "current_value": self.current_value,
            "target_value": self.target_value,
            "source": self.source,
            "reversible": self.action in REVERSIBLE_ACTIONS,
        }

    def describe(self) -> str:
        return (f"{self.action}：{self.service}.{self.knob} 由 {self.current_value} "
                f"改回 {self.target_value}（依据：{self.source}）")


def propose(changes: list[dict]) -> list[Proposal]:
    """从**变更记录**反转出提案：把被改过的旋钮改回原值。

    ⚠️ 只认形状完整的记录（`target`/`key`/`from` 都在）。缺字段就跳过 ——
       宁可少提一条，也不要凭空补一个值出来。
    """
    out: list[Proposal] = []
    for rec in changes or []:
        service = str(rec.get("target") or "").strip()
        knob = str(rec.get("key") or "").strip()
        before = rec.get("from")
        after = rec.get("to")
        if not (service and knob) or before is None or after is None:
            continue
        out.append(Proposal(
            service=service,
            knob=knob,
            current_value=str(after),       # 现在是 after
            target_value=str(before),       # 目标是把 before 改回来
            source=f"{rec.get('ts', '?')} 由 {rec.get('by', '?')} 注入："
                   f"{service}.{knob} {before} → {after}",
        ))
    return out


def apply(
    proposal: Proposal,
    *,
    ops_call: Callable[[str, dict], dict] | None = None,
    approved_by: str = "",
) -> dict:
    """执行提案 —— **必须先有人审批**，且动作必须**经那扇被批准的 MCP 门**。

    没有 `approved_by` 就**直接拒绝、什么都不做**：这条链路上最容易出的错
    就是"审批"被写成了一个可选参数、然后慢慢被默认通过。
    """
    if not approved_by.strip():
        return {"applied": False, "reason": "缺少审批人 —— 这条链路不允许无人审批执行",
                "audit_written": 0}
    if proposal.action not in REVERSIBLE_ACTIONS:
        return {"applied": False,
                "reason": f"动作 {proposal.action!r} 不在可逆白名单里（{REVERSIBLE_ACTIONS}）",
                "audit_written": 0}

    call = ops_call or _mcp_ops_call
    arguments: dict[str, Any] = {
        "service": proposal.service,
        "knobs": {proposal.knob: proposal.target_value},
    }
    payload = call("set_knobs", arguments)
    payload = dict(payload) if isinstance(payload, dict) else {"raw": str(payload)}
    applied = bool(payload.get("allowed", payload.get("ok", False)))
    written = append_audit({
        "type": "remediation_applied",
        "approved_by": approved_by,
        "proposal": proposal.to_dict(),
        "result": payload,
        "applied": applied,
    })
    return {"applied": applied, "result": payload, "audit_written": written}


def verify(*, symptom: str, before: int | None, after: int | None) -> dict:
    """验证：症状有没有好转。**缺少观测就说 unverified**，不许说"通过"。"""
    if before is None or after is None:
        missing = "事前" if before is None else "事后"
        return {
            "status": "unverified",
            "symptom": symptom,
            "why": f"缺少{missing}观测 —— 需要再跑一次场景才能比较（不猜）",
        }
    if after < before:
        status = "improved"
    elif after == before:
        status = "unchanged"
    else:
        status = "worse"
    return {"status": status, "symptom": symptom, "before": before, "after": after,
            "delta": after - before}


def append_audit(record: dict, *, path: Path | None = None) -> int:
    """写一条闭环账本（`runs/_remediation.ndjson`）。返回写入条数（0/1）。"""
    target = path or REMEDIATION_AUDIT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), **record}
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        fh.flush()
    return 1


__all__ = [
    "Proposal",
    "REMEDIATION_AUDIT_PATH",
    "REVERSIBLE_ACTIONS",
    "append_audit",
    "apply",
    "propose",
    "verify",
]
