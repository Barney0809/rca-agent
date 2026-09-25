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

#: 旋钮别名的规范表。**与 `scripts/inject_fault.py` 里那张表必须一致** ——
#: 那边也有一份（注入器在 scripts/ 下，从 src/ import 它会把依赖方向倒过来）。
#: 两处一致由一条用例钉住（`test_env_name_translation_matches_the_injector`），
#: 所以不会静默漂移。
KNOB_ALIASES = {
    "pool_size": "pool_limit",          # W_ORDER_POOL_SIZE → pool_limit
}

#: 世界服务的 knob 观测地址（**只读**，用于"动作到底生效没有"）
WORLD_KNOB_URLS = {
    "order": "http://127.0.0.1:8080",
    "inventory": "http://127.0.0.1:8081",
    "payment": "http://127.0.0.1:8082",
}


def knob_name_from_env(env_key: str, service: str = "") -> str:
    """把变更记录里的 **env 变量名**翻译成世界认的 **旋钮名**。

    ⚠️ 实测踩过的坑（2026-09-26，第一次 live 跑 M5）：变更记录的 `key` 是
       `W_INVENTORY_DOWNSTREAM_RETRIES`，而世界的 `/_inject` 只认 `downstream_retries`
       —— 名字不对时它会**返回 200 且 `changed: {}`**（什么都没改），
       于是"执行成功"是一句假话，而且**没有任何报错**。
    """
    name = str(env_key or "").strip().lower()
    if name.startswith("w_"):
        name = name[2:]
    for token in (str(service or "").lower(), "order", "inventory", "payment"):
        if token and name.startswith(token + "_"):
            name = name[len(token) + 1:]
            break
    return KNOB_ALIASES.get(name, name)


def read_world_knob(service: str, knob: str) -> object | None:
    """读回某个服务当前的旋钮值（**观测**，不是动作 —— 走只读的 `/_knobs`）。

    读不到就返回 None（"读不到"与"值不对"必须分开，见 `apply`）。
    """
    import httpx

    base = WORLD_KNOB_URLS.get(service)
    if base is None:
        return None
    try:
        return httpx.get(f"{base}/_knobs", timeout=5.0).json().get(knob)
    except Exception:                                                 # noqa: BLE001
        return None


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
    knob: str                   # **世界认的旋钮名**（已从 env 名翻译过来）
    current_value: str          # 现在是什么（变更记录里的 to）
    target_value: str           # 要改回什么（变更记录里的 from）
    source: str
    env_key: str = ""           # 变更记录里的原始 env 名（便于追溯，不发给世界）
    action: str = "set_knobs"

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "service": self.service,
            "knob": self.knob,
            "env_key": self.env_key,
            "current_value": self.current_value,
            "target_value": self.target_value,
            "source": self.source,
            "reversible": self.action in REVERSIBLE_ACTIONS,
        }

    def describe(self) -> str:
        aka = f"（env 名 {self.env_key}）" if self.env_key and self.env_key != self.knob else ""
        return (f"{self.action}：{self.service}.{self.knob}{aka} 由 {self.current_value} "
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
            knob=knob_name_from_env(knob, service),   # ★ 翻译：世界只认旋钮名
            env_key=knob,
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
    read_knob: Callable[[str, str], object | None] | None = None,
) -> dict:
    """执行提案 —— **必须先有人审批**，**必须读回确认真的变了**。

    ⚠️ 两条都是踩出来的：
      · 没有 `approved_by` 就什么都不做（"审批"不能是个默认通过的参数）；
      · **不能只看返回码**判断成功：世界对不认识的字段会返回 200 + `changed: {}`
        ⇒ 动作什么都没改，而"applied=True"会是一句假话（实测过）。
    """
    if not approved_by.strip():
        return {"applied": False, "reason": "缺少审批人 —— 这条链路不允许无人审批执行",
                "audit_written": 0}
    if proposal.action not in REVERSIBLE_ACTIONS:
        return {"applied": False,
                "reason": f"动作 {proposal.action!r} 不在可逆白名单里（{REVERSIBLE_ACTIONS}）",
                "audit_written": 0}

    call = ops_call or _mcp_ops_call
    reader = read_knob or read_world_knob
    knob = str(proposal.knob)
    arguments: dict[str, Any] = {
        "service": proposal.service,
        "knobs": {knob: proposal.target_value},
    }
    payload = call("set_knobs", arguments)
    payload = dict(payload) if isinstance(payload, dict) else {"raw": str(payload)}

    # ★ 读回：动作生效的唯一凭据（返回码不算）
    observed = reader(proposal.service, knob)
    effect_ok = observed is not None and str(observed) == str(proposal.target_value)
    applied = bool(payload.get("allowed", payload.get("ok", False))) and effect_ok
    reason = ""
    if not effect_ok:
        reason = (f"静默空操作：世界没有发生任何变化（读回 {knob}={observed!r}，"
                  f"期望 {proposal.target_value!r}）—— 常见原因是名字用错"
                  f"（变更记录里是 env 名，世界认的是旋钮名: {knob_name_from_env(knob, proposal.service)}）")

    written = append_audit({
        "type": "remediation_applied",
        "approved_by": approved_by,
        "proposal": proposal.to_dict(),
        "arguments": arguments,
        "observed_after": observed,
        "result": payload,
        "applied": applied,
        "reason": reason,
    })
    return {"applied": applied, "reason": reason, "observed": observed,
            "result": payload, "audit_written": written}


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
