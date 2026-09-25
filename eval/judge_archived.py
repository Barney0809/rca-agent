"""对**已存档的答案**跑 LLM 裁判 —— 让"两把尺子在哪不一致"变成可持久化的证据。

================================================================================
为什么需要它
================================================================================

本项目有两条独立的尺子：

    · **关键词/判据式判定**（主判据，决定 `correct`）—— 免费、可离线复算
    · **LLM 裁判**（独立交叉校验）—— 有少量成本，能看出"措辞对但语义反了"

平时它们只在**跑评测时**（`--judge`）一起出现。问题是：
**已经存档的答案**没法事后补裁判 —— 于是"某个场景上两把尺子分歧在哪"
就只能靠一次性的临时脚本去看，看完就没了（D15 就发生过：结论留在对话里，
别人既看不到、也没法复核）。

这个脚本把它变成一条**可复跑的命令**：读存档 → 逐条跑裁判 → 落盘成证据文件
（`runs/_eval/_judge/<场景>.json`），并把不一致的条目**逐条列出来**。

================================================================================
用法
================================================================================

    # 对一个场景、一组存档跑（默认是"当前每侧一条代表运行"）
    .\\.venv\\Scripts\\python.exe eval\\judge_archived.py --fault F4

    # 指定要看哪些存档
    .\\.venv\\Scripts\\python.exe eval\\judge_archived.py --fault F4 \\
        --archive baseline-20260925-133659 --archive multi-20260925-134153

⚠️ **它会花钱**：每条答案约 ¥0.0006（flash）。上面的 F4 六份存档共 18 条约 **¥0.011**。
   脚本会先打印"将要判几条、预计花费"，然后直接开跑（不做交互确认 —— 本项目里
   交互确认会被绕过，宁可让它便宜且可复跑）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from eval.judge import judge_cause  # noqa: E402
from eval.scenarios import CAUSE_LABELS, SCENARIOS, Cause, keyword_verdict  # noqa: E402
from rca.llm.provider import DeepSeekClient, LlmConfig  # noqa: E402

EVAL_DIR = ROOT / "runs" / "_eval"
OUT_DIR = EVAL_DIR / "_judge"

# 默认在看哪几份存档：每侧各取"当前的一次代表运行"。
# （刻意写死在代码里而不是自动挑：证据要能复查，就不能每次跑都换对象。）
DEFAULT_ARCHIVES = (
    "baseline-20260925-085629",   # baseline 7 场景 × 3 轮
    "multi-20260925-131953",      # multi 7 场景 × 3 轮（D15）
    "baseline-20260925-133659",   # flash baseline F4 @40
    "multi-20260925-133845",      # flash multi F4 @40
    "baseline-20260925-133604",   # v4-pro baseline F4 @40
    "multi-20260925-134153",      # v4-pro multi F4 @40
)


def _cause_for(fault: str) -> Cause | None:
    sc = SCENARIOS.get(fault)
    if sc is None:
        return None
    label = CAUSE_LABELS.get(fault)
    return next((c for c in sc.required_causes if c.name == label), None) or Cause(
        name=label or fault, keyword_groups=sc.keyword_groups
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="对存档答案跑裁判，落盘成可复核的证据")
    ap.add_argument("--fault", default="F4")
    ap.add_argument("--archive", action="append", default=None,
                    help="要看的存档目录名（可重复）；默认一组固定的代表运行")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    aps = args.archive or list(DEFAULT_ARCHIVES)
    cause = _cause_for(args.fault)
    if cause is None:
        print(f"❌ 场景 {args.fault} 不在场景表里")
        return 2
    label = CAUSE_LABELS.get(args.fault, args.fault)

    targets: list[tuple[str, dict]] = []
    for name in aps:
        p = EVAL_DIR / name / "results.json"
        if not p.exists():
            print(f"  （跳过 {name}：本机没有这份存档）")
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        for a in data["attempts"]:
            if a["fault_id"] == args.fault and (a.get("root_cause") or "").strip():
                targets.append((name, a))

    print("=" * 92)
    print(f"  对存档答案跑裁判　场景={args.fault}（{label}）　共 {len(targets)} 条")
    print(f"  预计花费：≈ ¥{len(targets) * 0.0006:.4f}（flash 量级，实际以返回为准）")
    print("=" * 92)

    client = DeepSeekClient(LlmConfig.from_env())
    rows: list[dict] = []
    cost = 0.0
    for name, a in targets:
        text = a["root_cause"]
        kw = keyword_verdict(text, cause)
        res = judge_cause(client, text, label, model=args.model)
        cost += res.cost_yuan
        agree = (kw == res.verdict)
        rows.append({
            "archive": name,
            "fault": args.fault,
            "round": a["round_no"],
            "correct_in_archive": bool(a["correct"]),
            "keyword": kw,
            "judge": res.verdict,
            "agree": agree,
            "judge_why": res.why,
            "answer_sha256_12": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
            "answer": text,
        })
        flag = "一致" if agree else "★不一致"
        print(f"  {name.split('-')[0]:<9} 第{a['round_no']}轮  "
              f"关键词={kw:<9} 裁判={res.verdict:<9} {flag}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{args.fault}.json"
    summary = {
        "fault": args.fault,
        "cause_label": label,
        "n": len(rows),
        "n_agree": sum(1 for r in rows if r["agree"]),
        "n_disagree": sum(1 for r in rows if not r["agree"]),
        "judge_cost_yuan": round(cost, 6),
        "rows": rows,
    }
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")

    print()
    print(f"  一致 {summary['n_agree']}/{summary['n']}；不一致 {summary['n_disagree']} 条")
    print(f"  裁判成本合计 ¥{cost:.6f}")
    print(f"  证据已落盘：{out.relative_to(ROOT)}")
    dis = [r for r in rows if not r["agree"]]
    if dis:
        print()
        print("── ★ 不一致的条目（**必须人读原文判谁对**，不许拿关键词当标准答案）──")
        for r in dis:
            print(f"  · {r['archive']} 第{r['round']}轮　关键词={r['keyword']} / 裁判={r['judge']}")
            print(f"    裁判理由：{r['judge_why'][:160]}")
            print(f"    答案摘要：{r['answer'][:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
