"""
降维验证工具 —— 对一个已跑完的场景做降维，并给出验收证据。

============================ 用法 ============================

    python scripts/reduce_run.py                 # 用最近一次场景
    python scripts/reduce_run.py r-20260924-0430 # 指定场景

============================ 它验证什么 ============================

D3 的验收标准有两条，缺一不可：

  1) **压缩率**：3–8 万行 → 几千 token（能装进上下文）
  2) **关键信号不丢**：本场景故障的"期望信号关键词"在降维结果里**仍然找得到**

第 2 条是重点。压缩率高但把证据压没了，等于没做 ——
所以这里拿场景记录里的 `expect_log_keywords` 逐条对拍。

    「压缩率」证明装得下；「信号存活」证明有用。
    两个都有，才叫降维而不是丢数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
# src 布局的惯例：把 src/ 加进搜索路径，于是包名是 `rca` 而不是 `src.rca`
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import tiktoken  # noqa: E402

from rca.telemetry.collect import (  # noqa: E402
    collect_changes,
    collect_logs,
    collect_metrics,
    load_scenario,
)
from rca.telemetry.reduce import reduce_logs  # noqa: E402

RUNS_DIR = ROOT / "runs"
ENC = tiktoken.get_encoding("cl100k_base")


def tokens_of(text: str) -> int:
    return len(ENC.encode(text))


def latest_run() -> Path | None:
    if not RUNS_DIR.exists():
        return None
    dirs = [d for d in RUNS_DIR.iterdir() if d.is_dir()]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def render_metrics(metrics: dict[str, dict[str, float]]) -> str:
    """把三个服务的指标渲染成紧凑文本。

    只挑**有诊断价值**的序列，不是全量倒出来 ——
    全量指标有几百条，绝大多数在一次故障里没有意义（而且费 token）。
    """
    # 白名单：这几类指标才可能指示故障
    interesting = (
        "requests_total",
        "handler_duration_ms",
        "downstream_duration_ms",
        "downstream_calls_total",
        "downstream_exhausted_total",
        "pool_in_flight",
        "pool_limit",
        "risk_control_duration_ms",
        "stock_level",
        "leak_bytes",
    )
    lines: list[str] = []
    for svc, series in sorted(metrics.items()):
        picked = [
            (k, v) for k, v in sorted(series.items())
            if k.split("{")[0] in interesting
        ]
        if not picked:
            continue
        lines.append(f"[{svc}]")
        for k, v in picked:
            lines.append(f"  {k} = {v:g}")
    return "\n".join(lines) if lines else "（无）"


def render_changes(changes: list[dict]) -> str:
    if not changes:
        return "（本场景无变更记录）"
    return "\n".join(
        f"  {c.get('ts')}  {c.get('target')}.{c.get('key')}: "
        f"{c.get('from')} → {c.get('to')}  by={c.get('by')}"
        for c in changes
    )


def main() -> int:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_run()
    if run_dir is None or not run_dir.exists():
        print("找不到任何场景目录。先跑一个：")
        print("    python scripts/inject_fault.py scenario F1")
        return 1

    scenario = load_scenario(run_dir)
    label = scenario.get("name", "(未知)")
    fault_id = scenario.get("fault_id")

    print("=" * 88)
    print(f"  降维验证 —— {run_dir.name}  故障={fault_id} {label}")
    print("=" * 88)

    # ---------------- 采集 ----------------
    records, unparsed = collect_logs(run_dir)
    view = reduce_logs(records)
    view.unparsed_lines = unparsed

    metrics = collect_metrics()
    changes = collect_changes(run_dir)

    if not records:
        print("\n⚠️  没读到日志。")
        print(f"    期望位置：{run_dir / 'logs'}")
        print("    如果场景是老版本驱动跑出来的（没有抓日志），重新跑一次即可。")
        return 1

    # ---------------- 压缩率 ----------------
    raw_text = "\n".join(r.raw for r in records)
    raw_tokens = tokens_of(raw_text)

    log_view_text = view.render()
    log_tokens = tokens_of(log_view_text)

    metrics_text = render_metrics(metrics)
    metrics_tokens = tokens_of(metrics_text)

    changes_text = render_changes(changes)
    changes_tokens = tokens_of(changes_text)

    print()
    print("## 压缩率")
    print(f"  原始日志     {len(records):>8} 行   {raw_tokens:>9} tokens")
    print(f"  降维后       {len(view.templates):>8} 个模板 {log_tokens:>9} tokens"
          f"   ← 压缩到 {log_tokens / max(raw_tokens, 1):.3%}")
    print(f"  指标视图                {metrics_tokens:>9} tokens")
    print(f"  变更视图                {changes_tokens:>9} tokens")
    total = log_tokens + metrics_tokens + changes_tokens
    print(f"  ── 三路合计             {total:>9} tokens"
          f"   ← 原始的 {total / max(raw_tokens, 1):.3%}")

    print()
    print(f"  未解析行数 {view.unparsed_lines}"
          f"（占 {view.unparsed_lines / max(len(records) + view.unparsed_lines, 1):.1%}）")
    print(f"  按频次裁剪掉的 INFO {view.dropped_info_lines} 行"
          f"（ERROR / WARNING **一条未裁**）")

    # ---------------- 关键信号存活（重点）----------------
    print()
    print("## 关键信号存活（对拍）")
    keywords = scenario.get("expect_log_keywords") or []
    if not keywords:
        print("  本场景未声明期望信号（baseline 场景通常没有）—— 跳过。")
        survived = []
    else:
        survived = []
        for kw in keywords:
            hit = kw in log_view_text
            survived.append(hit)
            print(f"  {'✅' if hit else '❌'} 「{kw}」")
        ok = all(survived)
        print()
        print(f"  结论：{len([s for s in survived if s])}/{len(keywords)} 条信号存活"
              f"  {'✅ 关键信号未丢' if ok else '❌ 有信号被压掉了，降维不可信'}")

    # ---------------- 打印三路视图（Agent 实际会看到的东西）----------------
    print()
    print("=" * 88)
    print("  Agent 实际会看到的三路视图（原样打印，便于人工审阅）")
    print("=" * 88)

    print("\n───── 日志视图（LogsAgent 可见）─────")
    print(log_view_text)

    print("\n───── 指标视图（MetricsAgent 可见）─────")
    print(metrics_text)

    print("\n───── 变更视图（ChangeAgent 可见）─────")
    print(changes_text)

    # ---------------- 结论 ----------------
    print()
    print("=" * 88)
    print("  D3 验收")
    print("=" * 88)
    target_ok = total <= 12000
    print(f"  压缩率    原始 {raw_tokens} → 合计 {total} tokens"
          f"  {'✅' if target_ok else '⚠️ 仍偏大'}")
    print(f"  信号存活  {len([s for s in survived if s])}/{len(keywords) if keywords else 0}"
          f"  {'✅' if (not keywords or all(survived)) else '❌'}")

    if fault_id:
        print()
        print(f"  ⚠️ 注意：本文件打印了场景的**标准答案**（是给人和评测看的）。")
        print(f"     Agent 只允许看到上面三路视图，不能看到 scenario.json 与 _knobs。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
