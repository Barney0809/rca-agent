"""
策略执行点（Policy Enforcement Point）—— Agent 侧唯一的"行动出口"。

============================ 它在架构里的位置 ============================

    Agent（后续 D6/D7 写的那些）
        │  经由 MCP 调用工具
        ▼
    ★ 策略执行点（独立进程）  ← 本模块
        ├─ 工具→动词矩阵        这个工具属于哪一级破坏力
        ├─ 路径守卫             规范化 + 白名单前缀
        ├─ 授权（grant + TTL）   不可逆操作需要显式授权
        ├─ 隔离区               删除 = 挪走，不是抹掉
        └─ 审计                 每次判定都记账，拒绝也记

**"唯一"是重点**（FR-2.1）：Agent 侧不提供任何绕过它的文件系统工具。
只要存在一条旁路，"策略"就退化成"建议"。

============================ deny-first 的含义 ============================

    默认**拒绝**，白名单放行。

而不是"默认放行 + 黑名单拦截"。区别在于：

    黑名单：你只能拦住你已经想到的坏事 —— 没想到的全放过去了
    白名单：你只放行你明确认可的事 —— 没想到的全被拒绝

事故里那条路径不在任何白名单里，所以它会**在第一步就被拒绝**，
根本走不到"删除"那一步。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .audit import AuditLog
from .pathguard import check_path
from .quarantine import (
    QuarantineError,
    list_entries,
    load_entry,
    quarantine,
    restore as restore_entry,
    summarize,
)
from .verbs import DEFAULT_ALLOWED_VERBS, GRANT_REQUIRED_VERBS, Decision, Verb

# ================================================================
# 策略矩阵：工具 → 破坏力等级
# ================================================================
#
# 这是"某工具能做多危险的事"的唯一声明处。新增工具必须在这里登记 ——
# **未登记的工具一律按最高等级处理**（而不是"未知就放行"）。
#
# 最后一行那种"默认最严"的写法很关键：如果漏登记一个工具，
# 结果是"它什么也做不了"（安全），而不是"它什么都能做"（事故）。
TOOL_MATRIX: dict[str, Verb] = {
    # --- 只读：读遥测，不碰文件系统 ---
    "query_logs": Verb.READ,
    "query_metrics": Verb.READ,
    "get_changes": Verb.READ,
    # --- 只读：读诊断产物（受路径约束）---
    "read_artifact": Verb.READ,
    # --- 写入：写诊断报告 / 执行诊断脚本（受路径约束）---
    "write_artifact": Verb.WRITE,
    "run_diagnostic": Verb.WRITE,
    # --- 不可逆：必须显式授权 ---
    "delete_artifact": Verb.DELETE,
    "move_artifact_outside": Verb.MOVE_OUTSIDE,
    "force_overwrite": Verb.FORCE,
}

# 未登记工具的处理等级：最高。
UNKNOWN_TOOL_VERB = Verb.FORCE


def verb_for_tool(tool: str) -> Verb:
    return TOOL_MATRIX.get(tool, UNKNOWN_TOOL_VERB)


# ================================================================
# 授权（grant）
# ================================================================

@dataclass
class Grant:
    """一次显式授权：允许某个动词、在某段路径前缀上、在限定时间内生效。

    为什么要 TTL（FR-2.5）：
        永久授权等于把"一次性例外"变成"永久后门"。
        事故的教训是"一次疏忽就够了" —— 那么授权也必须是一次性的。
    """

    grant_id: str
    verb: Verb
    path_prefix: str
    expires_at: float
    issued_by: str

    def is_valid(self, now: float) -> bool:
        return now < self.expires_at

    def covers(self, verb: Verb, resolved_path: Path | None) -> bool:
        if self.verb < verb:
            return False
        if resolved_path is None:
            return True
        import os
        prefix = os.path.normcase(self.path_prefix).rstrip("\\/")
        target = os.path.normcase(str(resolved_path))
        return target == prefix or target.startswith(prefix + os.sep)

    @property
    def ttl_remaining_s(self) -> float:
        return max(0.0, self.expires_at - time.time())


# ================================================================
# 引擎
# ================================================================

class PolicyEngine:
    """所有工具调用的唯一决策入口。

    参数：
        allowed_roots        授权根目录列表（白名单）。**这些目录之外一律拒绝。**
        quarantine_root      隔离区位置（通常也在某个授权根之内）
        quarantine_ttl_s     TTL，仅用于标记"可以清理了"，不触发任何删除
        audit_path           审计日志（NDJSON）路径
        clock                时间源，便于测试注入（对应 Java 的 Clock 抽象）
    """

    def __init__(
        self,
        *,
        allowed_roots: Sequence[Path],
        quarantine_root: Path,
        audit_path: Path,
        quarantine_ttl_s: int = 72 * 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # ⚠️ 授权根本身也要规范化 —— 否则"带 .. 的授权根"会让前缀比对失效
        self.allowed_roots = tuple(p.resolve(strict=False) for p in allowed_roots)
        self.quarantine_root = quarantine_root.resolve(strict=False)
        self.quarantine_ttl_s = quarantine_ttl_s
        self.audit = AuditLog(audit_path)
        self._clock = clock
        self._grants: dict[str, Grant] = {}

    # ---------------------------------------------------------- 授权
    def grant(
        self,
        *,
        verb: Verb,
        path_prefix: str | Path,
        ttl_s: int = 300,
        issued_by: str = "human",
    ) -> Grant:
        """显式授权。**只有人能调用** —— Agent 不能给自己发授权。

        这是权限模型里最容易做错的一点：如果 Agent 能调用 grant，
        那 deny-first 就名存实亡（它自己给自己开门）。
        在 D4 的实现里，grant 只在测试与人工程序中被调用，
        不作为任何工具暴露给 Agent。
        """
        g = Grant(
            grant_id=f"g-{secrets.token_hex(4)}",
            verb=verb,
            # ⚠️ 必须规范化成**绝对路径**再存。
            #
            # 踩过的坑：第一版直接存 str(path_prefix)。调用方若传相对路径，
            # 前缀就是相对的，而 covers() 拿到的是 check_path 解析出的
            # **绝对路径** —— 两者永远匹配不上，于是授权**静默失效**
            # （不报错、不留痕，只是"没生效"）。
            # 这正是本项目一直在防的那类缺陷：静默失败比报错危险。
            path_prefix=str(Path(path_prefix).resolve(strict=False)),
            expires_at=self._clock() + ttl_s,
            issued_by=issued_by,
        )
        self._grants[g.grant_id] = g
        return g

    def _find_grant(self, grant_id: str | None) -> Grant | None:
        if not grant_id:
            return None
        g = self._grants.get(grant_id)
        if g is None:
            return None
        if not g.is_valid(self._clock()):
            return None
        return g

    # ---------------------------------------------------------- 纯判定
    def decide(
        self,
        *,
        tool: str,
        verb: Verb | None = None,
        path: str | None = None,
        grant_id: str | None = None,
    ) -> Decision:
        """**无副作用、不写审计**的判定。公共方法负责写审计。

        判定顺序（顺序不能换，理由见下）：

            1. 取动词等级（矩阵；未登记工具按最高级）
            2. **先校验路径** —— 因为路径越界是最常见也最危险的情况，
               应当优先报出来（哪怕同时也没有授权，路径问题更需要被看见）
            3. 再校验授权 —— 不可逆操作必须有覆盖它的有效授权
        """
        v = verb if verb is not None else verb_for_tool(tool)

        # ---- 第 2 步：路径校验 ----
        resolved: Path | None = None
        if path is not None:
            pc = check_path(path, self.allowed_roots)
            if not pc.ok:
                return Decision(
                    allowed=False,
                    verb=v,
                    reason=pc.reason,
                    suggestion=self._suggest_for_path(pc.original, pc.reason),
                    resolved_path=str(pc.resolved) if pc.resolved else None,
                    extra={"op": tool, "original_path": pc.original},
                )
            resolved = pc.resolved

        # ---- 第 3 步：授权校验 ----
        if v in GRANT_REQUIRED_VERBS:
            g = self._find_grant(grant_id)
            if g is None:
                return Decision(
                    allowed=False,
                    verb=v,
                    reason=(
                        f"「{v.label}」属于不可逆操作，默认拒绝"
                        + ("（授权无效或已过期）" if grant_id else "（未提供授权）")
                    ),
                    suggestion=self._suggest_for_grant(v, resolved),
                    resolved_path=str(resolved) if resolved else None,
                    grant_id=grant_id,
                    extra={"op": tool},
                )
            if not g.covers(v, resolved):
                return Decision(
                    allowed=False,
                    verb=v,
                    reason=(
                        f"授权 {g.grant_id} 未覆盖本次操作"
                        f"（授权范围：{g.verb.label} @ {g.path_prefix}）"
                    ),
                    suggestion=self._suggest_for_grant(v, resolved),
                    resolved_path=str(resolved) if resolved else None,
                    grant_id=grant_id,
                    extra={"op": tool},
                )
            return Decision(
                allowed=True,
                verb=v,
                reason=f"已获授权 {g.grant_id}（剩余 {g.ttl_remaining_s:.0f}s）",
                resolved_path=str(resolved) if resolved else None,
                grant_id=g.grant_id,
                extra={"op": tool},
            )

        # ---- 允许的动词（READ / WRITE）----
        if v in DEFAULT_ALLOWED_VERBS:
            return Decision(
                allowed=True,
                verb=v,
                reason=f"「{v.label}」在默认允许范围内"
                + ("，且路径已通过白名单校验" if path is not None else ""),
                resolved_path=str(resolved) if resolved else None,
                extra={"op": tool},
            )

        # ---- 理论上到不了这里（未登记工具按 FORCE，已在上面的 grant 分支处理）----
        return Decision(
            allowed=False,
            verb=v,
            reason=f"「{v.label}」未被策略放行",
            suggestion="请显式授权，或改用只读方式完成目标",
            extra={"op": tool},
        )

    # ---------------------------------------------------------- 带审计的公共方法
    def authorize(
        self,
        *,
        tool: str,
        path: str | None = None,
        verb: Verb | None = None,
        grant_id: str | None = None,
        actor: str = "agent",
        trace_id: str = "",
    ) -> Decision:
        """给读/写类操作用：判定 + 写审计。"""
        d = self.decide(tool=tool, verb=verb, path=path, grant_id=grant_id)
        self.audit.write(d, tool=tool, actor=actor, trace_id=trace_id)
        return d

    def quarantine_delete(
        self,
        *,
        tool: str,
        path: str,
        grant_id: str | None = None,
        actor: str = "agent",
        trace_id: str = "",
        note: str = "",
    ) -> Decision:
        """**"删除"的唯一实现 —— 它把目标移入隔离区，从不抹掉。**

        这就是需求 FR-2.6「不存在硬删除」的落点。
        调用方拿到的 Decision 里带 `quarantine_id`，随时可以还原。
        """
        d = self.decide(tool=tool, verb=Verb.DELETE, path=path, grant_id=grant_id)
        if not d.allowed:
            self.audit.write(d, tool=tool, actor=actor, trace_id=trace_id)
            return d

        try:
            entry = quarantine(
                Path(d.resolved_path or path),
                self.quarantine_root,
                self.quarantine_ttl_s,
                note=note,
            )
        except QuarantineError as exc:
            denied = Decision(
                allowed=False,
                verb=Verb.DELETE,
                reason=f"隔离失败：{exc}",
                suggestion="检查目标是否存在、隔离区是否可写",
                resolved_path=d.resolved_path,
                extra={"op": tool},
            )
            self.audit.write(denied, tool=tool, actor=actor, trace_id=trace_id)
            return denied

        d.quarantine_id = entry.quarantine_id
        d.extra["ttl_s"] = entry.ttl_s
        d.reason = f"已移入隔离区（不是删除，可还原）：{entry.quarantine_id}"
        self.audit.write(d, tool=tool, actor=actor, trace_id=trace_id)
        return d

    def restore(
        self,
        *,
        quarantine_id: str,
        to: str | None = None,
        actor: str = "agent",
        trace_id: str = "",
    ) -> Decision:
        """从隔离区还原。

        还原**不需要授权**：它是"减少破坏"的方向，不是"增加破坏"的方向。
        给它加授权只会让人在紧急恢复时多一道障碍。
        """
        # ⚠️ 先校验 id 的类型，而不是直接往下传。
        #    踩过的坑：调用方在上一步被拒时拿到 quarantine_id=None，
        #    这里把 None 交给正则，抛的是 TypeError —— 一个内部崩溃，
        #    而不是"可解释的拒绝"。任何对外接口都要挡住这种输入。
        if not quarantine_id or not isinstance(quarantine_id, str):
            d = Decision(
                allowed=False,
                verb=Verb.MOVE_OUTSIDE,
                reason="还原需要一个有效的隔离项 id，收到的是空值",
                suggestion=(
                    "先确认上一步的「删除」真的被放行了 —— "
                    "若它被拒，Decision.quarantine_id 就是空的，没有东西可还原。"
                    "可用 list_quarantine() 列出当前所有隔离项。"
                ),
                extra={"op": "restore"},
            )
            self.audit.write(d, tool="restore", actor=actor, trace_id=trace_id)
            return d

        try:
            load_entry(self.quarantine_root, quarantine_id)
        except QuarantineError as exc:
            d = Decision(
                allowed=False,
                verb=Verb.MOVE_OUTSIDE,
                reason=f"还原失败：{exc}",
                suggestion="用 list_entries() 列出有效的隔离项 id",
                extra={"op": "restore"},
            )
            self.audit.write(d, tool="restore", actor=actor, trace_id=trace_id)
            return d

        # 还原目标若在授权范围内，还要过路径校验（防止"还原到范围外"变成一条旁路）
        if to is not None:
            pc = check_path(to, self.allowed_roots)
            if not pc.ok:
                d = Decision(
                    allowed=False,
                    verb=Verb.MOVE_OUTSIDE,
                    reason=f"还原目标不在授权范围内：{pc.reason}",
                    suggestion="指定一个授权范围内的还原位置",
                    resolved_path=str(pc.resolved) if pc.resolved else None,
                    extra={"op": "restore"},
                )
                self.audit.write(d, tool="restore", actor=actor, trace_id=trace_id)
                return d

        try:
            dest = restore_entry(
                self.quarantine_root, quarantine_id, Path(to) if to else None
            )
        except QuarantineError as exc:
            d = Decision(
                allowed=False,
                verb=Verb.MOVE_OUTSIDE,
                reason=f"还原失败：{exc}",
                suggestion="确认还原目标不存在（本实现拒绝覆盖）",
                extra={"op": "restore"},
            )
            self.audit.write(d, tool="restore", actor=actor, trace_id=trace_id)
            return d

        d = Decision(
            allowed=True,
            verb=Verb.MOVE_OUTSIDE,
            reason=f"已从隔离区还原到 {dest}",
            resolved_path=str(dest),
            quarantine_id=quarantine_id,
            extra={"op": "restore"},
        )
        d.extra["event_override"] = "policy.restored"
        self.audit.write(d, tool="restore", actor=actor, trace_id=trace_id)
        return d

    # ---------------------------------------------------------- 勘查
    def summary(self) -> dict:
        return {
            "allowed_roots": [str(p) for p in self.allowed_roots],
            "quarantine_root": str(self.quarantine_root),
            "active_grants": len([g for g in self._grants.values() if g.is_valid(self._clock())]),
            "quarantine": summarize(self.quarantine_root),
            "audit_records": len(self.audit.read_all()),
            "audit_denials": len(self.audit.denials()),
        }

    def list_quarantine(self):
        return list_entries(self.quarantine_root)

    # ---------------------------------------------------------- 拒绝话术（FR-2.8）
    @staticmethod
    def _suggest_for_path(original: str, reason: str) -> str:
        return (
            f"「{original}」不在允许操作的范围内。"
            f"如果确实需要处理它，请由人显式授权（grant），"
            f"或把它复制到授权目录内再操作 —— 不要试图绕过策略执行点。"
        )

    @staticmethod
    def _suggest_for_grant(verb: Verb, resolved: Path | None) -> str:
        target = str(resolved) if resolved else "目标"
        return (
            f"「{verb.label}」是不可逆操作。若要执行，需要由人显式授权"
            f"（grant(verb={verb.name}, path_prefix=..., ttl_s=...)），"
            f"授权会带 TTL 自动失效。"
            f"若只是想让它不再出现在视野里，可用隔离而非删除："
            f"隔离区（{target} 会被移到那里并保留 {verb.label} 前的全部内容）。"
        )
