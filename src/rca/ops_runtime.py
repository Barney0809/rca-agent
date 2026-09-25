"""ops 运行时的**唯一定义处**：授权根 / 隔离区 / 审计 / 授权库的位置，以及引擎构造。

为什么单独抽一个模块（harness-log #43 的教训：同一种知识写在两个地方，迟早走散）：

    现在有**两条**入口都能执行 ops 动作：
        · `scripts/ops.py`        —— 人用的 CLI
        · `scripts/ops_mcp_server.py` —— MCP server（工具面收口后的正式通道）
    两条入口必须指向**同一批路径、同一个策略引擎**，否则会出现
    "CLI 里删掉的东西在 MCP 那条路上看不见"这类鬼事。所以路径与构造只写在这里。
"""

from __future__ import annotations

from pathlib import Path

from rca.policy import PolicyEngine

ROOT = Path(__file__).resolve().parent.parent.parent

# 授权根：诊断产物都在 runs/ 下。**必须是绝对路径** ——
# 相对路径前缀会让 Grant.covers() 永远匹配不上（#9：授权静默失效）。
ARTIFACT_ROOT = ROOT / "runs"
QUARANTINE_ROOT = ARTIFACT_ROOT / "_quarantine"
AUDIT_PATH = ARTIFACT_ROOT / "_audit.ndjson"

# ⚠️ 授权库刻意放在 **runs/ 之外**（`.grants/` 在项目根下）：
#    放进授权根里的话，被约束的一方只要往那个文件追加一行就给自己开了门 ——
#    deny-first 会退化成形式主义。`PolicyEngine.__init__` 里有硬检查拦这件事。
GRANT_STORE = ROOT / ".grants" / "grants.ndjson"


def build_engine(*, actor: str = "human") -> PolicyEngine:
    """构造策略执行点。`actor` 只影响审计里记的名字，不影响权限。"""
    QUARANTINE_ROOT.mkdir(parents=True, exist_ok=True)
    return PolicyEngine(
        allowed_roots=[ARTIFACT_ROOT],
        quarantine_root=QUARANTINE_ROOT,
        audit_path=AUDIT_PATH,
        quarantine_ttl_s=72 * 3600,
        grant_store=GRANT_STORE,
    )
