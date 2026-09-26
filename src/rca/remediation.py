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
4. **写回去的值必须与世界的类型一致**（#62）：世界的 `/_inject` **不做任何类型转换**，
   所以把 `"1"`（字符串）写进一个整数旋钮，服务会在**运行时**崩
   （`max(1, "1")` → TypeError → 500），而"读回确认"如果按**字符串**比较，
   还会报告 `applied=True` —— 类型被改坏了，检查却说"成功"。
   ⇒ 写入前先读当前值学它的类型，读回时**连类型一起比**。

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


def coerce_like(current: object, value: object) -> object:
    """把 `value` 转成与 `current` **同一个类型**的值。

    ⚠️ 为什么必须做（#62，实测把世界弄崩过）：
       变更记录里的值是**字符串**（注入器写的是 `str(from_val)`），而世界里的旋钮是
       `int` / `float`。世界的 `/_inject` 只做 `setattr`，**不做任何类型转换** ⇒
       把 `"1"` 写进 `downstream_retries` 之后，`max(1, "1")` 抛 TypeError，
       **之后每一个请求都 500**（症状是 order 侧 502）。
       更糟的是：读回检查当时按 `str(observed) == str(target)` 比较，
       所以它**报告了 `applied=True`** —— 类型被改坏了，检查却说成功。

    类型以**世界当前的值**为准（不是我们猜一个 schema）：读不到当前值就**拒绝执行**，
    宁愿不动，也不猜。
    """
    if isinstance(current, bool):                    # bool 是 int 的子类，必须先判
        text = str(value).strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        raise ValueError(f"{value!r} 不是布尔值")
    if isinstance(current, int):
        return int(str(value).strip())
    if isinstance(current, float):
        return float(str(value).strip())
    if isinstance(current, str):
        return str(value)
    raise ValueError(f"不知道该把值写成什么类型（世界当前是 {type(current).__name__}）")


def apply(
    proposal: Proposal,
    *,
    ops_call: Callable[[str, dict], dict] | None = None,
    approved_by: str = "",
    read_knob: Callable[[str, str], object | None] | None = None,
) -> dict:
    """执行提案 —— **必须先有人审批**，**必须读回确认真的变了**。

    ⚠️ 三条都是踩出来的：
      · 没有 `approved_by` 就什么都不做（"审批"不能是个默认通过的参数）；
      · **不能只看返回码**判断成功：世界对不认识的字段会返回 200 + `changed: {}`
        ⇒ 动作什么都没改，而"applied=True"会是一句假话（实测过）。
      · **值要带着正确的类型写回去，读回要连类型一起比**（#62）：世界的 `/_inject`
        不做类型转换，写进去一个字符串会让服务在**运行时**崩；而按字符串比较的读回
        检查会把这个错误报成"成功"。
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

    # ★ 先读**当前值**：它的类型就是写回去时该用的类型（不存在"猜 schema"这回事）
    current = reader(proposal.service, knob)
    try:
        target = coerce_like(current, proposal.target_value)
    except (TypeError, ValueError) as exc:
        return {"applied": False, "observed": current,
                "reason": (f"不敢写：读到的当前值是 {current!r}（{type(current).__name__}），"
                           f"要写的 {proposal.target_value!r} 转不过去（{exc}）—— "
                           f"类型对不上的写入会让服务在运行时崩，所以宁可不写"),
                "audit_written": 0}

    arguments: dict[str, Any] = {
        "service": proposal.service,
        "knobs": {knob: target},
    }
    payload = call("set_knobs", arguments)
    payload = dict(payload) if isinstance(payload, dict) else {"raw": str(payload)}

    # ★ 读回：动作生效的唯一凭据（返回码不算）。**类型也要比** —— 否则"把整数写成字符串"
    #   这种改坏会被判成成功（#62 实测：值看着一样，服务却开始 500）。
    observed = reader(proposal.service, knob)
    effect_ok = (observed is not None
                 and type(observed) is type(target)
                 and observed == target)
    applied = bool(payload.get("allowed", payload.get("ok", False))) and effect_ok
    reason = ""
    if not effect_ok:
        if observed is not None and str(observed) == str(target):
            reason = (f"类型不对：读回 {knob}={observed!r}（{type(observed).__name__}），"
                      f"期望 {target!r}（{type(target).__name__}）—— 值看着一样，但类型被改坏了，"
                      f"服务会在运行时崩（这类'成功'是假的）")
        else:
            reason = (f"静默空操作：世界没有发生任何变化（读回 {knob}={observed!r}，"
                      f"期望 {target!r}）—— 常见原因是名字用错"
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


def verify(*, symptom: str, before: int | None, after: int | None,
           lower_is_better: bool = True) -> dict:
    """验证：症状有没有好转。**缺少观测就说 unverified**，不许说"通过"。

    ⚠️ `lower_is_better` 这个方向参数是**踩出来的**：默认"越小越好"适用于
       WARNING 条数、错误调用数这类**坏事的计数**；但"**成功订单数**"是越大越好 ——
       我把它接进单向判据后，脚本把 `10 → 0`（灾难）判成了 `improved` 并宣布"通过" ✗✗。
       ⇒ 判据必须显式知道方向，否则它会用漂亮的措辞报告灾难。
    """
    if before is None or after is None:
        missing = "事前" if before is None else "事后"
        return {
            "status": "unverified",
            "symptom": symptom,
            "why": f"缺少{missing}观测 —— 需要再跑一次场景才能比较（不猜）",
        }
    if after == before:
        status = "unchanged"
    elif (after < before) if lower_is_better else (after > before):
        status = "improved"
    else:
        status = "worse"
    return {"status": status, "symptom": symptom, "before": before, "after": after,
            "delta": after - before, "lower_is_better": lower_is_better}


def precheck_metric_signal(*, symptom: str, sent_before: int, ok_before: int | None,
                           sent_after: int, ok_after: int | None) -> dict:
    """前置检查：**这个指标在两种状态下都必须有信号**，否则不许下结论。

    ⚠️ 为什么要单独立一道门（这是 #61 修完之后紧接着撞上的第二种错法）：

      · #61 那种是**判据方向错了** —— 它会用漂亮的措辞把灾难说成修复；
      · 这一种是**判据没错、但没有信号** —— 「修好了」这句话**根本没有依据**，
        因为故障态本来就没有症状（这个指标对这个故障不敏感，或者故障压根没生效）。
        它更隐蔽：数字全对、格式全对、结论很漂亮，只是**什么也没证明**。

    规则三条（少一条就会得出不成立的结论）：

      1. 两态都要有**样本**（`sent > 0`）—— 没发出订单时谈「改善/恶化」是空话；
      2. 两态都要有**观测**（成功数是个数字，不是 `None`）——
         「没测到」不等于「测到 0」（#26 的空绿，在测量侧的形态）；
      3. **故障态必须真的有症状**（`ok_before < sent_before`）——
         若故障态全部成功，说明这个指标上**看不到这个故障**，
         此时无论恢复态是多少，「修好了」都是没有依据的 ⇒ 停在「不许下结论」。

    ⚠️ 口径（写清楚，别让读的人自己猜）：成功数落在 **0（地板）** 时**仍然有信号** ——
       方向可读（比故障态更差），但**幅度不可读**（这一次是 0，下一次还是 0，分不出来）。
       所以它不算「没信号」；只是结论里**不许谈幅度**。
    """
    out = {"symptom": symptom, "before": ok_before, "after": ok_after,
           "sent_before": sent_before, "sent_after": sent_after}
    if sent_before <= 0 or sent_after <= 0:
        return {**out, "ok": False, "status": "no-sample",
                "why": (f"样本不足：发出订单数 {sent_before} → {sent_after} "
                        f"（有一侧一笔都没发出去）⇒ 这个指标没有可比较的观测")}
    if ok_before is None or ok_after is None:
        missing = "故障态" if ok_before is None else "恢复态"
        return {**out, "ok": False, "status": "no-observation",
                "why": f"{missing}没有观测到成功数 —— 「没测到」不等于「测到 0」"}
    if ok_before >= sent_before:
        return {**out, "ok": False, "status": "no-signal-in-faulted-state",
                "why": (f"前置检查未通过：故障态**全部成功**（{ok_before}/{sent_before}）"
                        f"⇒ 这个指标上看不到故障（不敏感，或故障没生效）。"
                        f"此时「修好了」是没有依据的结论 —— 不许下结论")}
    return {**out, "ok": True, "status": "signal-in-both-states",
            "why": (f"两态都有信号 ✓（故障态 {ok_before}/{sent_before} 真失败过；"
                    f"恢复态 {ok_after}/{sent_after} 有观测）")}


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
    "coerce_like",
    "precheck_metric_signal",
    "propose",
    "verify",
]
