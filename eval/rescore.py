"""离线重算历史评测结果 —— 改了评分规则之后，不重跑 LLM 也能看出影响。

============================ 为什么需要它 ============================

评分规则和聚合逻辑**本身也会有 bug**（harness-log #11 #12 #13 都是这一类）。
修了规则之后，重跑一遍 LLM 才能验证 —— 但那要花钱，而且引入新的随机性。

原始尝试记录（`runs/_eval/*/results.json`）里保存了每次诊断的结论正文，
所以可以直接**用新规则重新判一遍**，与旧判定逐条对比。

这也是 runner.py 里 `load_report()` 那段注释说的同一件事：
**把原始数据留全，修了逻辑就不用再花一遍钱。**

============================ 最重要的用法：检查"误伤" ============================

评分规则改严之后，最大的风险不是漏判，而是**把本来就对的答案判成错的**。
所以这个脚本会把"旧=对、新=错"的条目**单独列出来** ——
那些必须逐条人工看过，确认是"真的答反了"而不是"措辞恰好踩了标记"。

============================ 用法 ============================

    .\\.venv\\Scripts\\python.exe eval\\rescore.py
    .\\.venv\\Scripts\\python.exe eval\\rescore.py --json runs\\_eval\\multi-xxx\\results.json

结果写成 UTF-8 报告（`runs/_eval/_rescore-<时间戳>.md`），
因为控制台是 GBK，中文直接打印会乱码。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# 控制台兜底（AGENTS.md 第 4 条"坑 4"）：encoding 与 errors 缺一不可。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from eval.scenarios import SCENARIOS  # noqa: E402

EVAL_DIR = ROOT / "runs" / "_eval"


def score_both(fid: str, text: str) -> tuple[bool, bool, str, str]:
    """返回 (旧判定, 新判定, 旧说明, 新说明)。"""
    sc = SCENARIOS.get(fid)
    if sc is None:
        return False, False, "(无评分规则)", "(无评分规则)"
    old_ok, _ = sc.judge(text, dismissal_aware=False)
    new_ok, _ = sc.judge(text, dismissal_aware=True)
    old_why = "对" if old_ok else ("错")
    new_why = sc.explain(text)
    return old_ok, new_ok, old_why, new_why


def collect(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for a in data.get("attempts", []):
            fid = a.get("fault_id", "?")
            text = a.get("root_cause", "") or ""
            old_ok, new_ok, _old_why, new_why = score_both(fid, text)
            rows.append(
                {
                    "file": path.parent.name,
                    "agent": data.get("agent", "baseline"),
                    "fault": fid,
                    "round": a.get("round_no"),
                    "old": old_ok,
                    "new": new_ok,
                    "new_why": new_why,
                    "text": text,
                }
            )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="用新评分规则重算历史结果")
    ap.add_argument("--json", nargs="*", help="指定 results.json；默认扫描 runs/_eval/*/results.json")
    args = ap.parse_args()

    paths = [Path(p) for p in args.json] if args.json else sorted(EVAL_DIR.glob("*/results.json"))
    if not paths:
        print("没有找到任何 results.json")
        return 1

    rows = collect(paths)

    changed = [r for r in rows if r["old"] != r["new"]]
    regressed = [r for r in changed if r["old"] and not r["new"]]   # 旧对 → 新错（可能是误伤）
    recovered = [r for r in changed if r["new"] and not r["old"]]   # 旧错 → 新对

    out: list[str] = []
    out.append(f"# 评分规则重算报告（{datetime.now():%Y-%m-%d %H:%M:%S}）\n")
    out.append(f"- 扫描文件：{len(paths)} 个")
    out.append(f"- 尝试总数：{len(rows)} 条")
    out.append(f"- 判定发生变化：**{len(changed)}** 条"
               f"（旧对→新错 {len(regressed)}；旧错→新对 {len(recovered)}）")
    out.append("")

    out.append("## 一、判定发生变化的条目（必须逐条人工确认）\n")
    if not changed:
        out.append("（无 —— 新规则没有改变任何历史判定）\n")
    for r in changed:
        arrow = "旧对 → **新错** ⚠️（可能是误伤，也可能本来就答反了）" if r["old"] else "旧错 → 新对"
        out.append(f"### {r['agent']} / {r['fault']} / 第{r['round']}轮　（{r['file']}）")
        out.append(f"- {arrow}")
        out.append(f"- 新说明：{r['new_why']}")
        out.append(f"- 结论正文：{r['text'][:400]}")
        out.append("")

    out.append("## 二、按场景汇总（旧 → 新）\n")
    out.append("| agent | 场景 | 旧正确数/总数 | 新正确数/总数 | 变化 |")
    out.append("|---|---|---|---|---|")
    keys = sorted({(r["agent"], r["fault"]) for r in rows})
    for agent, fid in keys:
        sub = [r for r in rows if r["agent"] == agent and r["fault"] == fid]
        o = sum(1 for r in sub if r["old"])
        n = sum(1 for r in sub if r["new"])
        mark = "" if o == n else "**变了**"
        out.append(f"| {agent} | {fid} | {o}/{len(sub)} | {n}/{len(sub)} | {mark} |")
    out.append("")

    total_old = sum(1 for r in rows if r["old"])
    total_new = sum(1 for r in rows if r["new"])
    out.append(f"**合计：旧 {total_old}/{len(rows)} → 新 {total_new}/{len(rows)}**\n")

    report = EVAL_DIR / f"_rescore-{datetime.now():%Y%m%d-%H%M%S}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(out), encoding="utf-8")

    print(f"files scanned   : {len(paths)}")
    print(f"attempts        : {len(rows)}")
    print(f"changed         : {len(changed)}  (old-ok->new-bad {len(regressed)}, old-bad->new-ok {len(recovered)})")
    print(f"totals          : old {total_old}/{len(rows)} -> new {total_new}/{len(rows)}")
    print(f"report written  : {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
