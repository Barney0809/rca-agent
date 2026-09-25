"""AC-10 的**端到端**演示：真强杀进程 → 真续跑。

为什么要有这个脚本（而不是只靠用例）：

    用例里"进程被杀"是用**异常**模拟的（`tests/test_graph_loop.py`）——
    那能证明检查点逻辑对，但**证明不了**"进程真的没了、SQLite 里真的留下了东西"。
    AC-10 的原话是「**进程被杀后**可断点续跑」，所以这一步得真杀一次：
    本脚本用 `os._exit(9)` 在最不该死的时候硬退出（**不跑任何清理**），
    然后由**另一个进程**接着跑完。

用法
----

    :: 第一次：跑到第 2 步就"断电"
    .\\.venv\\Scripts\\python.exe scripts\\demo_resume.py --fault F1 --kill-after-steps 2

    :: 第二次：**同一个线程**续跑（它从第 3 步接着走，不重做前两步）
    .\\.venv\\Scripts\\python.exe scripts\\demo_resume.py --fault F1 --resume

    :: 看检查点里有什么
    .\\.venv\\Scripts\\python.exe scripts\\demo_resume.py --fault F1 --show

⚠️ 它会**花钱**（真实调用 LLM，一次几步约 ¥0.02–0.05），也需要**被测世界在跑**
   （要读日志/指标；用 `scripts/inject_fault.py scenario F1` 造一次运行）。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from eval.runner import discover_runs  # noqa: E402
from rca.agents.graph_loop import (  # noqa: E402
    DEFAULT_CHECKPOINT_DB,
    build_graph,
    open_checkpointer,
    state_to_diagnosis,
    thread_id_for,
)
from rca.llm.provider import DeepSeekClient, LlmConfig  # noqa: E402
from rca.tools import RunContext  # noqa: E402


class KillingClient:
    """包一层真客户端：数到第 N 次调用就**硬退出**（模拟进程被杀）。

    `os._exit` 而不是 `sys.exit` / 异常 —— 后者都会被 Python 的清理逻辑兜住，
    那就不算"被杀"。这里要的正是**没有任何清理**地消失。
    """

    def __init__(self, inner, kill_after_calls: int) -> None:
        self.inner = inner
        self.kill_after = kill_after_calls
        self.n = 0

    def chat(self, **kw):
        self.n += 1
        if self.n > self.kill_after:
            print(f"\n💀 第 {self.n} 次调用前**强杀进程**（os._exit(9)，不做任何清理）", flush=True)
            sys.stdout.flush()
            os._exit(9)
        return self.inner.chat(**kw)


def build_ctx(run_dir: str | None, fault: str | None) -> RunContext:
    """拿到一次诊断的上下文（照 `scripts/run_specialists.py` 的写法，别自己发明）。

    ⚠️ `discover_runs()` 返回的是 **dict**（故障 → 最新一次场景目录），不是列表 ——
       我第一版按列表用，当场 `TypeError`。
    """
    if run_dir:
        d = ROOT / run_dir if not Path(run_dir).is_absolute() else Path(run_dir)
    else:
        runs = discover_runs([fault] if fault else None)
        if not runs:
            raise SystemExit(
                "没有可用场景。先跑：python scripts/inject_fault.py scenario " + (fault or "F1")
            )
        fid = fault or sorted(runs)[-1]
        d = runs[fid]
    return RunContext.from_run_dir(d)


def main() -> int:
    ap = argparse.ArgumentParser(description="LangGraph 断点续跑演示（AC-10）")
    ap.add_argument("--fault", default="F1")
    ap.add_argument("run_dir", nargs="?", default=None)
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--kill-after-steps", type=int, default=None,
                    help="跑到第 N 次模型调用就强杀自己（模拟进程被杀）")
    ap.add_argument("--resume", action="store_true", help="从检查点续跑（输入 None）")
    ap.add_argument("--show", action="store_true", help="只打印检查点里有什么")
    ap.add_argument("--db", default=str(DEFAULT_CHECKPOINT_DB))
    args = ap.parse_args()

    ctx = build_ctx(args.run_dir, args.fault)
    tid = thread_id_for(ctx, fault_id=args.fault, round_no=args.round)
    db = Path(args.db)

    if args.show:
        print(f"检查点：{db}")
        if not db.exists():
            print("  （还没有）")
            return 0
        con = sqlite3.connect(str(db))
        try:
            rows = con.execute("select thread_id, count(*) from checkpoints group by thread_id").fetchall()
        finally:
            con.close()
        for thread_id, n in rows:
            mark = "  ← 本次" if thread_id == tid else ""
            print(f"  {thread_id}：{n} 个检查点{mark}")
        print(f"  本次线程 id：{tid}")
        return 0

    cfg = {"configurable": {"thread_id": tid}}
    inner = DeepSeekClient(LlmConfig.from_env())
    client = KillingClient(inner, args.kill_after_steps) if args.kill_after_steps else inner

    print(f"线程 id：{tid}")
    print(f"检查点：{db}")
    print(f"模式  ：{'续跑（输入 None）' if args.resume else '全新运行'}")
    print(f"上下文：run_id={ctx.run_id}  日志模板={len(getattr(ctx.log_view, 'templates', []) or [])}\n")

    with open_checkpointer(db) as saver:
        graph = build_graph(client, ctx, model=None, max_steps=args.max_steps,
                            checkpointer=saver)
        payload = None if args.resume else {}
        # durability="sync"：**每一步落盘之后再往下走** —— 否则"被杀"时最后一步可能还没写
        final = graph.invoke(payload, config=cfg, durability="sync")

    diag = state_to_diagnosis(final)
    print("─" * 78)
    print(f"步数 {diag.steps}　工具 {diag.tool_calls}　成本 ¥{diag.cost_yuan:.4f}　"
          f"收敛={'是' if diag.finished else '否'}　JSON={'OK' if diag.parse_ok else '失败'}")
    for i, c in enumerate(diag.root_causes, 1):
        print(f"  原因{i}：{c[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
