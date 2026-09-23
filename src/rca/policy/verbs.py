"""
动词分级：给每个操作定一个"破坏力等级"。

============================ 为什么需要分级 ============================

把所有操作一视同仁，只有两个选择：全放开或全禁止。

    全放开 → 一次越界就不可挽回（这正是 2026-09-22 那次事故）
    全禁止 → Agent 什么也做不了，没有实用价值

分级给出第三条路：**不同等级不同待遇**。

    READ         自由
    WRITE        自由（有 git 兜底 / 可覆盖重写）
    DELETE       **默认拒绝**，且**永不真删**（走隔离区）
    MOVE_OUTSIDE **默认拒绝**（把文件移出授权范围同样是不可逆的）
    FORCE        **默认拒绝**（强制类操作，如 -Force / 忽略错误继续）

============================ 关键：DELETE 不是"更严格的 WRITE" ============================

它是**性质不同**的操作：

    WRITE 的目标是"让文件变成某个样子"—— 写错了还能再写回来
    DELETE 的目标是"让文件消失"—— 删错了就没有"再删回来"

所以本项目的设计是：**不存在硬删除这个动作**（对应需求 FR-2.6）。
`delete` 在实现层被翻译成"移入隔离区 + 设 TTL"。

> **把不可逆变成可逆，比"守住不可逆操作"更根本。**
> 2026-09-22 那次无法挽回，根因不是守门没拦住，
> 而是**物理层面根本没有留下退路**：`-Force` 绕过了回收站，
> 磁盘又没有卷影副本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Verb(IntEnum):
    """操作的破坏力等级。数值越大越危险。

    用 IntEnum 而不是普通 Enum，是为了能直接比较大小（`verb >= Verb.DELETE`）。
    对应 Java：一个带序号的枚举，外加可比较——Java 的 enum 天然有序。
    """

    READ = 0
    WRITE = 1
    DELETE = 2
    MOVE_OUTSIDE = 3
    FORCE = 4

    @property
    def label(self) -> str:
        return {
            Verb.READ: "读取",
            Verb.WRITE: "写入",
            Verb.DELETE: "删除",
            Verb.MOVE_OUTSIDE: "移出授权范围",
            Verb.FORCE: "强制操作",
        }[self]


# 默认允许的等级。**这个集合要尽可能小** —— deny-first 的含义就是
# "默认拒绝，白名单放行"，而不是"默认放行，黑名单拦截"。
#
# 反例（事故中的实际行为）：默认放行 + 没有黑名单 = 什么都拦不住。
DEFAULT_ALLOWED_VERBS: frozenset[Verb] = frozenset({Verb.READ, Verb.WRITE})

# 需要显式授权（grant）才能执行的等级
GRANT_REQUIRED_VERBS: frozenset[Verb] = frozenset(
    {Verb.DELETE, Verb.MOVE_OUTSIDE, Verb.FORCE}
)


@dataclass
class Decision:
    """一次策略判定的结果。

    ⚠️ `suggestion` 是**必填语义**（对应需求 FR-2.8）：
       拒绝必须给出"那你可以怎么做"，而不是干巴巴地说"不行"。
       一条只说"不"的守卫，会逼着人去绕过它。
    """

    allowed: bool
    verb: Verb
    reason: str
    suggestion: str = ""
    resolved_path: str | None = None
    quarantine_id: str | None = None
    grant_id: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def event_type(self) -> str:
        """映射成事件流协议里的事件类型（见 docs/03-事件流协议.md）。

        审计记录直接复用协议里定义的事件类型，不另起一套 ——
        否则"审计"和"事件流"会各长一份，字段迟早对不上。
        """
        if self.allowed:
            if self.quarantine_id:
                return "policy.quarantined"
            return "policy.allowed"
        return "policy.denied"

    def to_event_data(self) -> dict:
        data: dict = {
            "op": self.extra.get("op", ""),
            "path": self.resolved_path,
            "verb_level": self.verb.name.lower(),
            "reason": self.reason,
        }
        if not self.allowed:
            data["suggestion"] = self.suggestion
        if self.quarantine_id:
            data["quarantine_id"] = self.quarantine_id
            data["ttl_s"] = self.extra.get("ttl_s")
        if self.grant_id:
            data["grant_id"] = self.grant_id
        return data
