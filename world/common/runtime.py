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


def make_inject_router(knobs: Knobs, on_change=None) -> APIRouter:
    """构造 `/_inject` 端点。

    参数：
      knobs      —— 要改的参数集合
      on_change  —— 可选回调（changed: dict）→ None。用于"改了池大小要通知池"这类联动。

    ⚠️ 这里【刻意不写任何日志】。理由见本文件头部。
    """
    router = APIRouter()

    @router.post("/_inject", include_in_schema=False)
    async def inject(patch: dict):
        changed = knobs.apply(patch)
        if changed and on_change is not None:
            on_change(changed)
        # 注意：没有 log.xxx —— 这是刻意的
        return {"changed": changed, "current": knobs.snapshot()}

    @router.get("/_knobs", include_in_schema=False)
    async def read_knobs():
        """只读当前参数。仅供本机排查用，Agent 不读它。"""
        return knobs.snapshot()

    return router
