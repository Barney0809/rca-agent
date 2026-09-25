"""ops CLI：**唯一能真正执行"改变世界"的入口**（人用的，不是 Agent 用的）。

================================================================================
为什么是"人的 CLI"而不是"给 Agent 一个工具"
================================================================================

策略执行点的注释写着：

    授权只能由人/测试发 —— **Agent 不能给自己开门**。

所以这个 CLI 里**有** `grant` 子命令（它是**人**的界面），
而暴露给 Agent 的 ops 工具面（`src/rca/tools_ops.py` 的 `OPS_TOOL_SPECS`）里**没有**。
两者是同一个机制的两半，缺一不可：

    · 没有 grant  → 不可逆操作永远做不了（人就真的什么都干不成）
    · grant 给 Agent → deny-first 名存实亡（它自己给自己开门）

================================================================================
用法（每一步都会写审计）
================================================================================

    :: 看隔离区里有什么（只读，无需授权）
    .\\.venv\\Scripts\\python.exe scripts\\ops.py quarantine

    :: 不可逆操作：默认拒绝
    .\\.venv\\Scripts\\python.exe scripts\\ops.py delete runs\\_eval\\some\\result.json

    :: 人要放行时：先签一次性授权（TTL 默认 300 秒），再带上 grant id
    .\\.venv\\Scripts\\python.exe scripts\\ops.py grant --verb delete --path runs\\_eval
    .\\.venv\\Scripts\\python.exe scripts\\ops.py delete runs\\_eval\\some\\result.json --grant-id g-xxxx

    :: 反悔：从隔离区还原（不需要授权 —— 它是"变回可逆"的那一半）
    .\\.venv\\Scripts\\python.exe scripts\\ops.py restore g-<隔离区 id>

    :: 改世界的旋钮（可逆动作，默认放行但要记账）
    .\\.venv\\Scripts\\python.exe scripts\\ops.py set-knobs order risk_latency_ms=800

⚠️ 审计日志默认在 `runs/_audit.ndjson`；**拒绝也会写进去**。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# P4 守则：打印非 ASCII 的脚本必须给 stdout 兜底，否则在 Windows 控制台
# （代码页 936）会抛 UnicodeEncodeError，把整段结论一起丢掉。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from rca.policy import PolicyEngine, Verb  # noqa: E402
from rca.tools_ops import OpsToolBox  # noqa: E402

# 授权根：诊断产物都在 runs/ 下。**注意这里写的是绝对路径** ——
# 相对路径前缀会让 covers() 永远匹配不上（#9：授权静默失效）。
ARTIFACT_ROOT = ROOT / "runs"
QUARANTINE_ROOT = ARTIFACT_ROOT / "_quarantine"
AUDIT_PATH = ARTIFACT_ROOT / "_audit.ndjson"

# ⚠️ 授权库刻意放在 **runs/ 之外**（`.grants/` 在项目根下）。
#    理由见 `PolicyEngine.__init__` 里那条硬检查：授权库落在授权根之内的话，
#    Agent 只要往那个文件追加一行就给自己开了门 —— deny-first 会变成形式主义。
#    （`.grants/` 已加进 .gitignore：它是运行时状态，不是仓库内容。）
GRANT_STORE = ROOT / ".grants" / "grants.ndjson"


def build_engine() -> PolicyEngine:
    QUARANTINE_ROOT.mkdir(parents=True, exist_ok=True)
    return PolicyEngine(
        allowed_roots=[ARTIFACT_ROOT],
        quarantine_root=QUARANTINE_ROOT,
        audit_path=AUDIT_PATH,
        quarantine_ttl_s=72 * 3600,
        # ★ 授权库放在 runs/ **之外**（`_grants/`）：放在授权根里的话，
        #   被约束的一方就能改它 —— 那等于把钥匙放在被锁的人手边。
        #   engine 里有一条硬检查拦这件事。
        grant_store=GRANT_STORE,
    )


def _parse_knobs(items: list[str]) -> dict:
    out: dict = {}
    for it in items:
        if "=" not in it:
            raise SystemExit(f"❌ 旋钮要写成 key=value，收到：{it!r}")
        k, v = it.split("=", 1)
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="ops 动作（全部经过策略执行点）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_g = sub.add_parser("grant", help="人签发一次性授权（Agent 拿不到这个能力）")
    p_g.add_argument("--verb", required=True,
                     choices=[v.name.lower() for v in Verb],
                     help="read / write / delete / move_outside / force")
    p_g.add_argument("--path", required=True, help="授权覆盖的路径前缀")
    p_g.add_argument("--ttl-s", type=int, default=300)

    p_d = sub.add_parser("delete", help="删除产物（不可逆：默认拒绝）")
    p_d.add_argument("path")
    p_d.add_argument("--grant-id", default=None)
    p_d.add_argument("--note", default="")

    p_r = sub.add_parser("restore", help="从隔离区还原")
    p_r.add_argument("quarantine_id")
    p_r.add_argument("--to", default=None)

    p_k = sub.add_parser("set-knobs", help="改世界旋钮（可逆）")
    p_k.add_argument("service")
    p_k.add_argument("knobs", nargs="+", help="key=value ...")

    sub.add_parser("quarantine", help="列出隔离区内容（只读）")

    args = ap.parse_args()
    engine = build_engine()
    ops = OpsToolBox(engine, actor="human-cli", trace_id="cli")

    if args.cmd == "grant":
        want = Verb[args.verb.upper()]
        g = engine.grant(verb=want, path_prefix=args.path, ttl_s=args.ttl_s)
        print(f"已签发授权：{g.grant_id}")
        print(f"  动词   ：{g.verb.label}")
        print(f"  前缀   ：{g.path_prefix}")
        print(f"  有效期 ：{g.ttl_remaining_s:.0f}s（过期即失效 —— 一次性例外，不是永久后门）")
        return 0

    if args.cmd == "delete":
        res = ops.delete_artifact(args.path, grant_id=args.grant_id, note=args.note)
        print(("✅ " if res.allowed else "⛔ ") + res.reason)
        if res.suggestion:
            print(f"   建议：{res.suggestion}")
        if res.quarantine_id:
            print(f"   隔离区 id：{res.quarantine_id}（可还原）")
        return 0 if res.allowed else 3

    if args.cmd == "restore":
        res = ops.restore_artifact(args.quarantine_id, to=args.to)
        print(("✅ " if res.allowed else "⛔ ") + res.reason)
        return 0 if res.allowed else 3

    if args.cmd == "set-knobs":
        res = ops.set_knobs(args.service, _parse_knobs(args.knobs))
        print(("✅ " if res.allowed else "⛔ ") + res.reason)
        if res.suggestion:
            print(f"   建议：{res.suggestion}")
        return 0 if res.allowed else 3

    if args.cmd == "quarantine":
        entries = ops.list_quarantine()
        if not entries:
            print("隔离区是空的。")
            return 0
        print(f"隔离区里有 {len(entries)} 项：")
        for en in entries:
            left = (en.expires_at - datetime.now().astimezone()).total_seconds()
            print(f"  · {en.quarantine_id}")
            print(f"      原路径：{en.original_path}")
            print(f"      剩余  ：{left / 3600:.1f} 小时　过期={'是' if en.is_expired() else '否'}"
                  + ("（目录）" if en.is_dir else ""))
        print("还原：scripts/ops.py restore <id>")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
