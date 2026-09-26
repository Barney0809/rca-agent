"""
运行时可变参数（Knobs）—— 故障注入的着力点。

============================ 为什么需要它 ============================

故障注入有两种做法：

  a) 改环境变量 → 重启容器        ❌ 太慢；而且重启会打断现场，
                                      "故障发生前的状态"就没了
  b) 运行时改内存里的参数         ✅ 立即生效，现场连续

我们用 (b)。每个服务暴露一个 `/_inject` 端点，接收一个补丁对象。

⚠️⚠️ 极重要的一条纪律 ⚠️⚠️

    `/_inject` **绝不能往服务日志里写任何东西**。

因为 Agent 的证据来源就是服务日志。如果注入动作自己留了痕，
Agent 只要 grep "注入 / inject" 就能直接拿到答案 —— 谜题就废了。

注入的痕迹**只允许出现在「变更事件日志」里**，而且只有**真实的配置变更**
才记（见 docs/04-故障目录.md §4）。其他故障（外部依赖劣化、数据变慢、
内存泄漏）在真实世界里也不是"变更"，所以不留变更记录。

于是变更 Agent 会看到恰好两条变更，其中一条是红鲱鱼 ——
**"有变更"不等于"是它"**，这正是信息隔离要制造的局面。

=============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter


@dataclass
class Knobs:
    """运行时可调参数。初始值来自环境变量（Settings），注入时被改写。

    每个字段都对应一种或多种故障（见 docs/04-故障目录.md）。
    """

    # --- 连接池容量（F2）---
    pool_limit: int = 8

    # --- 获取连接的等待上限（F2 的配套参数）---
    # 为什么它也需要可注入：池很小时，吞吐 = 池容量 / 单次处理时长。
    # 等待上限若高达 2000ms，整个系统会被拖到每分钟几个请求，
    # 场景根本跑不完。把它调小（如 400ms）能让"快速失败"成为主路径，
    # 从而在合理时间内产生足够的失败样本 —— 现实里短超时也是常见配置。
    pool_acquire_timeout_ms: int = 2000

    # --- 本环节数据访问的额外延迟（F3）---
    slow_op_ms: int = 0

    # --- 每次请求泄漏的 MB 数（F6，对照组）---
    leak_mb_per_req: float = 0.0

    # --- 外部风控（F1 慢 / F5 错）---
    risk_latency_ms: int = 30
    risk_error_rate: float = 0.0

    # --- 调用下游的重试次数（F4）---
    downstream_retries: int = 1

    def apply(self, patch: dict) -> dict:
        """应用一个补丁，返回真正发生变化的字段。

        返回值的形状：{"字段名": [旧值, 新值]}
        把它原样返回给调用方（注入器），注入器再据此写变更事件。
        """
        changed: dict[str, list] = {}
        for key, new_value in patch.items():
            if not hasattr(self, key):
                continue                      # 未知字段直接忽略，不报错
            old_value = getattr(self, key)
            if old_value != new_value:
                setattr(self, key, new_value)
                changed[key] = [old_value, new_value]
        return changed

    def snapshot(self) -> dict:
        """当前全部参数（用于诊断与对账）。"""
        return {
            "pool_limit": self.pool_limit,
            "pool_acquire_timeout_ms": self.pool_acquire_timeout_ms,
            "slow_op_ms": self.slow_op_ms,
            "leak_mb_per_req": self.leak_mb_per_req,
            "risk_latency_ms": self.risk_latency_ms,
            "risk_error_rate": self.risk_error_rate,
            "downstream_retries": self.downstream_retries,
        }


#: `/_inject` 的**带外**留痕：只保留最近 N 条（有界，防止长跑吃内存）。
#:
#: ⚠️ 为什么要有它（ADR-0009 / harness-log #65）：一次集成用例红在
#:    "循环里的 5xx"，而**谁把世界改成这样的查不到** —— `/_inject` 刻意不留任何痕迹。
#:    于是最该有日志的那件事，恰好是唯一没日志的。这一层是**排障用的**，
#:    不是审计账本（真要账本看 `runs/_remediation.ndjson` 与 `src/rca/policy/audit.py`）。
#:
#: ⚠️ **绝不写进服务日志**：Agent 的三路只读工具之一就是日志 ——
#:    写进去等于把"谁改了 retries"直接告诉 Agent，F4 场景（配置漂移靠变更记录发现）当场失效。
#:    所以它只走**这个 Agent 看不见的只读端点**（见 ADR-0009 决定 1）。
INJECT_HISTORY_LIMIT = 64
_INJECT_HISTORY: list[dict] = []


def record_injection(service: str, changed: dict, by: str = "") -> None:
    """把一次真实改动记进带外历史（有界环形缓冲）。

    ⚠️ 这里**不许抛异常**：它是在 `/_inject` 的处理路径里被调用的 ——
       留痕失败把"注入"这条路径搞崩（500），调用方会以为"注入失败"，
       而真相是"日志记不下来"。所以对值只做**保守转换**（认不出就原样存）。
    """
    if not changed:
        return
    import time  # noqa: PLC0415

    def _plain(v: object) -> object:
        return list(v) if isinstance(v, (list, tuple)) else v      # apply() 给的是 [旧, 新]

    _INJECT_HISTORY.append({
        "ts": round(time.time(), 3),
        "service": service,
        "changed": {k: _plain(v) for k, v in changed.items()},
        "by": by,                      # 调用方**自报**（本机都是 127.0.0.1，世界猜不出来源）
    })
    del _INJECT_HISTORY[:-INJECT_HISTORY_LIMIT]


def inject_history() -> list[dict]:
    """最近若干次注入（新的在后）。只读。"""
    return list(_INJECT_HISTORY)


def make_inject_router(knobs: Knobs, on_change=None, service: str = "") -> APIRouter:
    """构造 `/_inject` 端点。

    参数：
      knobs      —— 要改的参数集合
      on_change  —— 可选回调（changed: dict）→ None。用于"改了池大小要通知池"这类联动。
      service    —— 服务名，只用于**带外留痕**（ADR-0009），不进日志。

    ⚠️ 这里【刻意不写任何日志】。理由见本文件头部。
    """
    router = APIRouter()

    @router.post("/_inject", include_in_schema=False)
    async def inject(patch: dict):
        # `by` 是**调用方自报**的来源（不进 Knobs，只是一个记号）：注入器传 fault_id、
        # 集成夹具传用例名 —— 否则事后无从知道"是谁改的"（#65 就卡在这里）。
        by = str(patch.pop("by", "")) if isinstance(patch, dict) else ""
        changed = knobs.apply(patch)
        if changed and on_change is not None:
            on_change(changed)
        record_injection(service, changed, by)
        # 注意：没有 log.xxx —— 这是刻意的
        return {"changed": changed, "current": knobs.snapshot()}

    @router.get("/_knobs", include_in_schema=False)
    async def read_knobs():
        """只读当前参数。仅供本机排查用，Agent 不读它。"""
        return knobs.snapshot()

    @router.get("/_inject_history", include_in_schema=False)
    async def read_inject_history():
        """只读**带外**注入历史（ADR-0009）。Agent 的工具面里没有它 —— 这是有意的。"""
        return {"limit": INJECT_HISTORY_LIMIT, "records": inject_history()}

    return router
