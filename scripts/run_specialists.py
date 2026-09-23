"""
跑三个专职 Agent，把它们的结论并排打印出来。

============================ 用法 ============================

    python scripts/run_specialists.py                 # 用最新场景
    python scripts/run_specialists.py r-20260924-...  # 指定场景
    python scripts/run_specialists.py --fault F2      # 指定某种故障的最新场景

============================ 这一步看什么 ============================

D6 只做"三个 Agent 各自出结论"，**还没有 Coordinator**（那是 D7）。

所以这里要看的是三件事：

  1. **职责边界生效了吗** —— 每个 Agent 是否只用自己那一个工具
  2. **它们真的不确定吗** —— 每个 Agent 都该有盲区，结论应当是不完整的
  3. **`needs_from_others` 有用吗** —— 它们说出的"我需要什么"，
     是不是恰好指向别的 Agent 手里的证据

第 3 条最关键：**D7 的交叉举证就靠这个字段驱动。**
如果三个 Agent 都说"我不需要别的"，那说明隔离设计失败了。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from rca.agents.specialist import investigate_all, Hypothesis  # noqa: E402
from rca.llm.provider import DeepSeekClient, LlmConfig  # noqa: E402
from rca.tools import RunContext  # noqa: E402

from eval.runner import discover_runs  # noqa: E402
from eval.scenarios import SCENARIOS  # noqa: E402


def print_hypothesis(h: Hypothesis) -> None:
    print(f"\n─── {h.name} ───")
    print(f"  结论（置信度 {h.confidence:.2f}）：{h.claim or '（无）'}")
    if h.evidence:
        print("  证据：")
        for e in h.evidence[:4]:
            print(f"    · {e[:170]}")
    if h.needs_from_others:
        print("  我需要其他同事提供：")
        for n in h.needs_from_others[:4]:
            print(f"    ? {n[:170]}")
    print(
        f"  [步数 {h.steps}  工具 {h.tool_calls}  越权尝试 {h.denied_tool_calls}  "
        f"成本 ¥{h.cost_yuan:.4f}  {h.elapsed_s:.1f}s  "
        f"JSON={'OK' if h.parse_ok else '失败'}  收敛={'是' if h.finished else '否'}]"
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="跑三个专职 Agent")
    p.add_argument("run_dir", nargs="?", default=None)
    p.add_argument("--fault", default=None, help="用该故障的最新场景，如 F2")
    p.add_argument("--model", default=None)
    p.add_argument("--max-steps", type=int, default=6)
    p.add_argument("--sequential", action="store_true", help="串行跑（默认并发）")
    args = p.parse_args(argv)

    if args.run_dir:
        run_dir = ROOT / args.run_dir if not Path(args.run_dir).is_absolute() else Path(args.run_dir)
    else:
        runs = discover_runs([args.fault] if args.fault else None)
        if not runs:
            print("没有可用场景。先跑：python scripts/inject_fault.py scenario F2")
            return 1
        fid = args.fault or sorted(runs)[-1]
        run_dir = runs[fid]

    ctx = RunContext.from_run_dir(run_dir)
    scenario = ctx.scenario
    fid = scenario.get("fault_id")

    print("=" * 92)
    print(f"  三个专职 Agent —— 场景 {run_dir.name}  故障={fid}")
    print("=" * 92)
    print(f"  日志：{ctx.log_view.total_lines} 行 → {len(ctx.log_view.templates)} 个模板")
    print(f"  指标：{len(ctx.metrics)} 个服务")
    print(f"  变更：{len(ctx.changes)} 条")
    print()
    print(f"  标准答案：{scenario.get('ground_truth')}")
    print("  ⚠️ 注意：标准答案**不会**给 Agent 看，这里只用于人工对照。")

    cfg = LlmConfig.from_env()
    client = DeepSeekClient(cfg)
    hyps = investigate_all(
        client, ctx, model=args.model, max_steps=args.max_steps,
        parallel=not args.sequential,
    )

    for h in hyps:
        print_hypothesis(h)

    # ---- 汇总 ----
    print()
    print("=" * 92)
    print("  汇总")
    print("=" * 92)
    total_cost = sum(h.cost_yuan for h in hyps)
    total_tools = sum(h.tool_calls for h in hyps)
    total_denied = sum(h.denied_tool_calls for h in hyps)
    wall = max(h.elapsed_s for h in hyps)
    print(f"  总成本 ¥{total_cost:.4f}   工具调用 {total_tools}   越权尝试 {total_denied}")
    print(f"  墙钟（并发，取最慢者）{wall:.1f}s"
          f"   串行将需要 {sum(h.elapsed_s for h in hyps):.1f}s")
    print(f"  JSON 解析成功 {sum(1 for h in hyps if h.parse_ok)}/{len(hyps)}"
          f"   收敛 {sum(1 for h in hyps if h.finished)}/{len(hyps)}")

    if total_denied:
        print(f"\n  ⚠️ 有 {total_denied} 次越权调用 —— 隔离机制起了作用（拒绝了），")
        print("     但如果次数多，说明 prompt 里的职责边界没讲清楚。")

    needers = [h for h in hyps if h.needs_from_others]
    print(f"\n  提出「我需要其他同事提供…」的 Agent：{len(needers)}/{len(hyps)}"
          f"   ← 这个数字决定 D7 的交叉举证能不能跑起来")
    if len(needers) < len(hyps):
        for h in hyps:
            if not h.needs_from_others:
                print(f"    ⚠️ {h.role} 没有提出任何需求（隔离设计可能失效）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
