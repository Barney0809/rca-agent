"""对**已归档的轨迹**跑护栏 —— 零成本双侧验证（D21 / M1）。

============================ 它回答两个问题 ============================

    1. 坏轨迹：护栏**必须触发**（否则它没用）
    2. 好轨迹：护栏**不许误伤**（否则它会打断正确的推理）

这两侧都**零 API 成本** —— 用的是已经归档的 trace 与结论文本（D19 那套思路的延伸）。
任何人克隆后都能自己复跑，这正是"护栏有用"能被称为**测量**而不是声称的原因。

============================ 用法 ============================

    # 默认：对最新的 multi 归档跑一遍，打印"报没报" vs "答案对不对"的对照表
    python scripts/guard_replay.py

    # 看某一次尝试的逐条发现（带证据指针）
    python scripts/guard_replay.py --attempt F4:3

    # 把发现写进护栏账本 runs/_guard.ndjson
    python scripts/guard_replay.py --audit

⚠️ 输出的对照表里**没有**"准确率"之类的分数：护栏不判对错（它看不到答案），
   这里只是把"它报了什么"和"答案对不对"并排放着让人看 ——
   相关系数是给人读的，不是护栏的输出。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from rca.guard import (  # noqa: E402
    append_audit,
    inject_form_defect,
    judge,
    run_diagnostics,
    run_rules,
)
from rca.guard.adapters import iter_archived_attempts, trace_from_archived  # noqa: E402

EVAL_DIR = ROOT / "runs" / "_eval"


def resolve_run(name: str | None) -> Path:
    """找要跑的归档运行目录。默认取**最新的 multi-***（按名字，名字末位是时间戳）。"""
    if name:
        p = Path(name)
        return p if p.is_absolute() else (ROOT / p)
    candidates = sorted(p for p in EVAL_DIR.glob("multi-*") if (p / "results.json").exists())
    if not candidates:
        raise SystemExit(f"找不到归档运行：{EVAL_DIR}/multi-*/results.json")
    return candidates[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="对已归档轨迹跑护栏（零成本）")
    ap.add_argument("--run", default=None, help="归档运行目录（默认：最新的 multi-*）")
    ap.add_argument("--attempt", default=None, help="只看一次，格式 F4:3")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--audit", action="store_true", help="把发现写进 runs/_guard.ndjson")
    ap.add_argument(
        "--inject", choices=["identifier", "number", "both"], default=None,
        help="注入一个**形式缺陷**（凭空发明的指标名 / 查无此数的数值）—— "
             "这是「必须触发」那一侧的验收：真实归档里没有这种缺陷",
    )
    args = ap.parse_args()

    run_dir = resolve_run(args.run)

    # ---- 「必须触发」那一侧：注入一个真实归档里不存在的**形式缺陷**
    if args.inject:
        label = args.attempt or "F1:2"
        for attempt, trace_path in iter_archived_attempts(run_dir):
            if f"{attempt.get('fault_id')}:{attempt.get('round_no')}" != label or trace_path is None:
                continue
            clean = trace_from_archived(attempt, trace_path)
            clean_verdict = judge(run_rules(clean))
            dirty = inject_form_defect(clean, args.inject)
            dirty_verdict = judge(run_rules(dirty))
            print("=" * 96)
            print(f"  注入形式缺陷（{args.inject}）   基线尝试={label}（真实归档，答案"
                  f"{'对' if attempt.get('correct') else '错'}）")
            print("=" * 96)
            print(f"\n  注入前：判定={clean_verdict.verdict}  发现={len(clean_verdict.findings)} 条")
            print(f"  注入后：判定={dirty_verdict.verdict}  发现={len(dirty_verdict.findings)} 条")
            for f in dirty_verdict.findings:
                print(f"    [{f.severity.upper()}] {f.rule}：{f.subject}")
            caught = {f.rule for f in dirty_verdict.findings}
            want = {"unsupported_claim"} if args.inject == "identifier" else {"unverifiable_number"}
            if args.inject == "both":
                want = {"unsupported_claim", "unverifiable_number"}
            print(f"\n  期望抓住：{'、'.join(sorted(want))}")
            print(f"  实际抓住：{'、'.join(sorted(caught)) or '(一条都没有)'}")
            ok = want <= caught and clean_verdict.ok
            print(f"\n  {'✅ 通过' if ok else '❌ 不通过'} —— "
                  f"注入的缺陷{'被抓到' if want <= caught else '被漏掉'}；"
                  f"基线干净轨迹{'零发现' if clean_verdict.ok else '被误报'}")
            return 0 if ok else 1
        raise SystemExit(f"归档里找不到尝试 {label}（或它没有 trace）")

    rows: list[dict] = []
    for attempt, trace_path in iter_archived_attempts(run_dir):
        label = f"{attempt.get('fault_id')}:{attempt.get('round_no')}"
        if args.attempt and label != args.attempt:
            continue
        if trace_path is None:
            rows.append({"label": label, "correct": bool(attempt.get("correct")),
                         "skipped": "归档里没有 trace"})
            continue
        trace = trace_from_archived(attempt, trace_path)
        verdict = judge(run_rules(trace))
        diags = run_diagnostics(trace)      # 诊断量：不计入判定（见 rules.DIAGNOSTIC_RULES）
        if args.audit:
            append_audit(verdict, label=f"{run_dir.name}/{label}")
        rows.append({
            "label": label,
            "correct": bool(attempt.get("correct")),
            "verdict": verdict.verdict,
            "counts": verdict.counts(),
            "diagnostics": {f.rule: sum(1 for g in diags if g.rule == f.rule) for f in diags},
            "findings": [{"rule": f.rule, "severity": f.severity, "subject": f.subject,
                          "detail": f.detail, "evidence": list(f.evidence)}
                         for f in verdict.findings],
            "diagnostic_findings": [{"rule": f.rule, "subject": f.subject, "detail": f.detail}
                                    for f in diags],
        })

    if args.json:
        print(json.dumps({"run": run_dir.name, "rows": rows}, ensure_ascii=False, indent=2))
        return 0

    print("=" * 96)
    print(f"  护栏（M1）对归档轨迹的判定   run={run_dir.name}  尝试数={len(rows)}")
    print("=" * 96)
    if args.attempt:
        for r in rows:
            print(f"\n  {r['label']}  correct={r.get('correct')}  判定={r.get('verdict', r.get('skipped'))}")
            for f in r.get("findings", []):
                print(f"    [{f['severity'].upper()}] {f['rule']}：{f['subject']}")
                print(f"        {f['detail']}")
                print(f"        证据：{'、'.join(f['evidence']) or '(无)'}")
            for f in r.get("diagnostic_findings", []):
                print(f"    [诊断量·不计入判定] {f['rule']}：{f['subject']}")
                print(f"        {f['detail']}")
        return 0

    print(f"\n  {'尝试':<8}{'答案对不对':<12}{'护栏判定':<10}规则明细")
    for r in rows:
        if "skipped" in r:
            print(f"  {r['label']:<8}{str(r['correct']):<12}{'—':<10}{r['skipped']}")
            continue
        detail = "、".join(f"{k}×{v}" for k, v in sorted(r["counts"].items())) or "—"
        print(f"  {r['label']:<8}{str(r['correct']):<12}{r['verdict']:<10}{detail}")

    fired = [r for r in rows if r.get("counts")]
    bad = [r for r in rows if r.get("correct") is False]
    good = [r for r in rows if r.get("correct") is True]
    print("\n  ── 双侧对照（这才是重点）──")
    print(f"     坏答案 {len(bad)} 次，护栏报出 {sum(1 for r in bad if r.get('counts'))} 次")
    print(f"     好答案 {len(good)} 次，护栏报出 {sum(1 for r in good if r.get('counts'))} 次"
          f"   ← 这一列就是**误伤**")
    print(f"     共报出 {len(fired)} 次；明细：",
          "、".join(f"{k}×{v}" for k, v in
                    sorted({k: sum(r["counts"].get(k, 0) for r in rows) for k in
                            {k for r in rows for k in r.get("counts", {})}}.items())))
    print("\n  ⚠️ 护栏不判对错（它看不到答案）。这张表只把两件事并排给人看 ——")
    print("     测出误伤就改规则，或如实报出来，不要假装没有。")

    diag_hits = [r for r in rows if r.get("diagnostics")]
    diag_good = sum(1 for r in diag_hits if r.get("correct") is True)
    print("\n  ── 诊断量（**不计入判定**，只给人看）──")
    print(f"     unbased_metric 报出 {len(diag_hits)} 次，其中 {diag_good} 次是**正确**结论"
          " ⇒ 精确率约 7%，已降级")
    print("     理由：区分「该不该拿这个指标当主根因」是**配权判断**，需要标准答案 ——")
    print("     这正是 ADR-0007 决定 1 说的表达力边界，这次是用测量证实的（见 rules.py）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
