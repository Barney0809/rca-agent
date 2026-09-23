"""
策略执行点（Policy Enforcement Point）。

============================ 这个包存在的唯一理由 ============================

**让"不可逆的操作"在这个系统里根本不存在。**

2026-09-22 的事故里，一个被派遣的代理执行了 `Remove-Item -Recurse -Force`，
删掉了工作目录之外的一个真实目录，数据永久丢失。

事后复盘得到的结论不是"要更小心"，而是：

    **不能靠模型的自觉，要靠机制。**

本包就是那个机制。它由四件事组成，缺一不可：

    pathguard   路径规范化 + 白名单前缀  —— 事故里缺的就是这一步
    quarantine  "删除"= 移入隔离区，不是抹掉 —— 让不可逆变成可逆
    audit       每次判定都记账，拒绝也记 —— 没有静默失败
    engine      唯一的决策入口，无旁路

============================ 一条可以直接验证的性质 ============================

**`src/rca/policy/` 全目录不含任何硬删除调用。**

这不是"我们小心不调用"，而是"想调用也找不到"。
对应的回归用例会扫描本目录的源码来证明这一点：

    tests/test_policy.py::test_policy_module_contains_no_hard_delete_calls

如果哪天有人在隔离区到期清理里加了 `shutil.rmtree`，那条用例会立刻变红。
"""

from .audit import AuditLog
from .engine import TOOL_MATRIX, UNKNOWN_TOOL_VERB, Grant, PolicyEngine, verb_for_tool
from .pathguard import PathCheck, check_path, is_within, normalize
from .quarantine import (
    QuarantineEntry,
    QuarantineError,
    list_entries,
    quarantine,
    restore,
    summarize,
    sweep_report,
)
from .verbs import DEFAULT_ALLOWED_VERBS, GRANT_REQUIRED_VERBS, Decision, Verb

__all__ = [
    "AuditLog",
    "Decision",
    "DEFAULT_ALLOWED_VERBS",
    "GRANT_REQUIRED_VERBS",
    "Grant",
    "PathCheck",
    "PolicyEngine",
    "QuarantineEntry",
    "QuarantineError",
    "TOOL_MATRIX",
    "UNKNOWN_TOOL_VERB",
    "Verb",
    "check_path",
    "is_within",
    "list_entries",
    "normalize",
    "quarantine",
    "restore",
    "summarize",
    "sweep_report",
    "verb_for_tool",
]
