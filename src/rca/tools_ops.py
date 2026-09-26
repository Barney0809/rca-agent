"""ops 工具：**唯一能改变世界的工具家族** —— 每一次调用都必须经过策略执行点。

================================================================================
为什么会有这个文件（D15，对应 FR-2.1「无旁路保证」）
================================================================================

在这之前，`src/rca/policy/` 是一个**建好了却没人调用**的包：只有 `tests/test_policy.py`
import 它。于是项目首页那句

    任何不可逆操作都必须经过独立于模型判断的确定性边界

在代码里**没有落点** —— 诊断 Agent 的工具面只有三个**只读**工具，
"边界"挡在一条没有人的路上（harness-log #41 记的就是这类"说的比做的多"）。

这个文件把那条路铺出来：

    · 三个 ops 工具：改旋钮（可逆）、删除产物（不可逆）、从隔离区还原
    · 每次调用都走 `PolicyEngine`：路径规范化 → 动词分级 → 默认拒绝 → 审计
    · **而且只发给 `operator` 角色**：日志/指标/变更三个诊断专员依旧只有只读工具
      ⇒ 在诊断路径上，不可逆操作不是"被拦住"，而是**根本给不到**

================================================================================
两条刻意的设计（都是"防自己"）
================================================================================

1. **不提供 grant 工具。**
   `PolicyEngine.grant()` 的注释写着：授权只能由人/测试发，**Agent 不能给自己开门**。
   这里严格遵守 —— 本模块没有任何方法能创建授权，`grant_id` 只能从外面传进来。

2. **拒绝是一个正常返回值，不是异常。**
   被拒的调用返回 `OpsResult(allowed=False, reason=..., suggestion=...)`，
   **并且已经写进审计**。调用方（Agent）看得见自己为什么被拒 ——
   静默失败比报错危险（#9 就是被"静默失效的授权"咬的）。

================================================================================
仍未接线、如实标注
================================================================================

这些工具是**进程内调用**，没有经过 MCP 收口。所以准确的说法是：

    「无旁路保证」在 **operator 这条路径上成立**（所有动作都过 PolicyEngine）；
    整个 Agent 的"工具面完全收口"**仍未完成** —— 见 ADR-0001、harness-log #45。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx

from rca.policy import Decision, PolicyEngine, Verb

# 被诊断世界的三个服务（与 scripts/inject_fault.py 保持一致）
WORLD_SERVICES: dict[str, str] = {
    "order": "http://127.0.0.1:8080",
    "inventory": "http://127.0.0.1:8081",
    "payment": "http://127.0.0.1:8082",
}


@dataclass
class OpsResult:
    """ops 工具的统一返回。**被拒绝也是正常结果**（而不是异常）。"""

    tool: str
    allowed: bool
    reason: str
    suggestion: str = ""
    quarantine_id: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "allowed": self.allowed,
            "reason": self.reason,
            "suggestion": self.suggestion,
            "quarantine_id": self.quarantine_id,
            **({"detail": self.detail} if self.detail else {}),
        }

    @classmethod
    def from_decision(cls, tool: str, d: Decision, **detail) -> OpsResult:
        return cls(
            tool=tool,
            allowed=d.allowed,
            reason=d.reason,
            suggestion=d.suggestion,
            quarantine_id=d.quarantine_id or "",
            detail=detail,
        )


class OpsToolBox:
    """把「改变世界的动作」接到策略执行点上。

    参数：
        engine    `PolicyEngine`（唯一决策入口）
        world     服务名 → base url；测试里可以指向假服务
        actor     审计里记的调用者（角色名）
        trace_id  审计里记的诊断链路 id
        client    可注入的 httpx.Client —— 测试用它避免真连世界
    """

    def __init__(
        self,
        engine: PolicyEngine,
        *,
        world: dict[str, str] | None = None,
        actor: str = "agent",
        trace_id: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self.engine = engine
        self.world = dict(world or WORLD_SERVICES)
        self.actor = actor
        self.trace_id = trace_id
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------ 内部
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=10.0)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def _deny(self, tool: str, d: Decision) -> OpsResult:
        return OpsResult.from_decision(tool, d)

    # ------------------------------------------------- 1) 可逆：改世界的旋钮
    def set_knobs(
        self,
        service: str,
        knobs: dict,
        *,
        grant_id: str | None = None,
    ) -> OpsResult:
        """改被诊断世界的一个/几个旋钮（可逆：再改回去即可）。

        动词等级 **WRITE** ⇒ 按 `DEFAULT_ALLOWED_VERBS` 默认放行，但**要审计**。
        为什么不像删除那样要求授权：改旋钮本身可逆，本项目把"必须授权"留给
        **不可逆**那一档（`GRANT_REQUIRED_VERBS`）。真正危险的是下面那个。
        """
        d = self.engine.authorize(tool="set_knobs", verb=Verb.WRITE,
                                  actor=self.actor, trace_id=self.trace_id)
        if not d.allowed:
            return self._deny("set_knobs", d)
        if service not in self.world:
            return OpsResult(
                tool="set_knobs", allowed=False,
                reason=f"未知服务：{service}（可选：{sorted(self.world)}）",
                suggestion="用 order / inventory / payment 之一",
            )
        try:
            # 自报来源（ADR-0009）：这条是 **Agent / 人的那扇门**，
            # 留空的话世界的历史里就是"匿名改动" —— 而出问题时最想知道谁改的正是这种。
            payload = {**dict(knobs), "by": f"ops:{self.actor or 'unknown'}"}
            r = self._http().post(f"{self.world[service]}/_inject",
                                  json=payload, timeout=10.0)
            r.raise_for_status()
            after = r.json()
        except Exception as exc:  # noqa: BLE001 —— 连不上世界也是一种"没做成"
            return OpsResult(
                tool="set_knobs", allowed=True,
                reason=f"已获准执行，但调用世界失败：{exc}",
                suggestion="确认被诊断世界在跑（docker compose ps）",
                detail={"service": service, "requested": knobs},
            )
        return OpsResult(
            tool="set_knobs", allowed=True,
            reason=f"已按策略放行并写入 {service}",
            detail={"service": service, "requested": knobs, "world_reply": after},
        )

    # --------------------------------------------- 2) 不可逆：删除产物（默认拒绝）
    def delete_artifact(
        self,
        path: str,
        *,
        grant_id: str | None = None,
        note: str = "",
    ) -> OpsResult:
        """**不可逆操作**：删除一个诊断产物。

        ⇒ 默认拒绝（`Verb.DELETE` 在 `GRANT_REQUIRED_VERBS` 里）。
        ⇒ 即使拿到有效授权，也**不会真的抹掉** —— 它被移入隔离区，可还原。
        ⇒ 路径越界时，**先**报路径问题（哪怕同时也没有授权）。
        """
        d = self.engine.quarantine_delete(
            tool="delete_artifact", path=path, grant_id=grant_id,
            actor=self.actor, trace_id=self.trace_id, note=note,
        )
        return OpsResult.from_decision("delete_artifact", d, requested_path=path)

    # ------------------------------------------------------------------ 3) 撤销
    def restore_artifact(self, quarantine_id: str, *, to: str | None = None) -> OpsResult:
        """从隔离区还原。**这个方向不需要授权** —— 它是"把不可逆变回可逆"的那一半。"""
        try:
            d = self.engine.restore(quarantine_id=quarantine_id, to=to,
                                    actor=self.actor, trace_id=self.trace_id)
        except Exception as exc:  # noqa: BLE001
            return OpsResult(
                tool="restore_artifact", allowed=False,
                reason=f"还原失败：{exc}",
                suggestion="用 list_quarantine() 看隔离区里有什么",
                quarantine_id=quarantine_id,
            )
        # ⚠️ engine.restore() 返回的是 **Decision**（不是 Path）—— 第一版把它当路径
        #    拼进 reason，结果打印出一整坨 dataclass repr。这里直接用它的理由与路径。
        return OpsResult(
            tool="restore_artifact",
            allowed=bool(d.allowed),
            reason=d.reason,
            suggestion=d.suggestion,
            quarantine_id=quarantine_id,
            detail={"restored_to": d.resolved_path} if d.resolved_path else {},
        )

    # ------------------------------------------------------------------ 只读辅助
    def list_quarantine(self) -> list:
        """隔离区里有什么（只读，无需授权）。"""
        return self.engine.list_quarantine()


# --------------------------------------------------------------------------- #
# 工具面登记：给 Agent 用的函数定义（openai tools 格式）
#
# ⚠️ 与 `src/rca/tools.py` 里的三个只读工具**分开登记**，而且**默认不发**：
#    诊断角色（logs / metrics / change）的工具面里**没有**这些名字。
#    给谁发、发给谁，由 `src/rca/agents/roles.py` 决定 ——
#    那里有一条用例守着"诊断角色拿不到任何写工具"。
# --------------------------------------------------------------------------- #
OPS_TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "set_knobs",
            "description": (
                "改变被诊断世界的旋钮（例如把某个服务的延迟调高），用于做受控实验。"
                "该动作会经过策略执行点并写入审计日志。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string",
                                "enum": sorted(WORLD_SERVICES),
                                "description": "要改哪个服务"},
                    "knobs": {"type": "object",
                              "description": "旋钮名 → 新值，例如 {\"risk_latency_ms\": 800}"},
                },
                "required": ["service", "knobs"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_artifact",
            "description": (
                "删除一个诊断产物。**这是不可逆操作：默认会被拒绝**，"
                "必须有覆盖它的显式授权；即使获准，也只是移入隔离区（可还原）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的产物路径"},
                    "grant_id": {"type": "string", "description": "覆盖本次操作的授权 id"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restore_artifact",
            "description": "把之前移入隔离区的产物还原回来（不需要授权）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "quarantine_id": {"type": "string"},
                    "to": {"type": "string", "description": "还原到哪（默认回原位）"},
                },
                "required": ["quarantine_id"],
            },
        },
    },
]


def ops_tool_names() -> set[str]:
    """本模块暴露的工具名（给角色隔离的用例用）。"""
    return {spec["function"]["name"] for spec in OPS_TOOL_SPECS}


def artifact_root_default() -> Path:
    """ops 工具默认允许的路径根（诊断产物都在 runs/ 下）。"""
    return Path(__file__).resolve().parent.parent.parent / "runs"
