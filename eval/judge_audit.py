"""裁判审计：把 LLM 裁判接进评分**之前**，先量两件事。

============================ 为什么要先量 ============================

本项目一路上学到的最大一件事：**换一把尺子，结论就可能整个反过来。**
(#16 / #21 / #25 全是"尺子"的故事。)

所以"上了一个 LLM 裁判"本身**不是成绩**。在用它替换关键词判定之前，
必须回答两个问题，而这两个问题**只有数据能回答**：

    ①  自一致性：同一条文本判 K 次，结论一样吗？
        （`temperature=0` 也**不保证**逐字可复现 —— 必须实测，不能假设。）

    ②  分歧在哪：它和关键词判定不一致的地方，**谁对**？
        这一条最重要：**一致率高不等于对** ——
        两把尺子可能一起错（#16 就是这么活了很久的）。

============================ 分歧样本怎么处理 ============================

脚本会把分歧**逐条列出来**（含原文），由人判谁对。
**不用关键词判定的结果去当标准答案** —— 那就成了"用旧尺子校准新尺子"，
是本项目明确防过的事。

============================ 用法 ============================

    .\\.venv\\Scripts\\python.exe eval\\judge_audit.py --sample 6 --reps 3
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from eval.judge import judge_cause  # noqa: E402
from eval.scenarios import SCENARIOS, Cause, keyword_verdict  # noqa: E402
from rca.llm.provider import DeepSeekClient, LlmConfig  # noqa: E402

EVAL_DIR = ROOT / "runs" / "_eval"

# 每个场景要判的"原因"标签。多故障场景（F8）判两条。
# ⚠️ 用场景自己的 `required_causes`，不另写一份 —— 否则两边会走散。
CAUSE_LABEL = {
    "F1": "外部风控变慢",
    "F7": "外部风控变慢",
    "F2": "外部风控变慢",
    "F3": "inventory 本环节处理变慢",
    "F4": "inventory 的重试次数配置漂移",
    "F5": "外部风控错误率升高",
    "F6": "order 内存泄漏",
    "F8": "内存泄漏",
}


def load_attempts() -> list[dict]:
    """从存档里读回全部尝试。"""
    out: list[dict] = []
    for path in sorted(EVAL_DIR.glob("*/results.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        agent = data.get("agent", "baseline")
        for a in data.get("attempts", []):
            text = a.get("root_cause") or ""
            if not text.strip():
                continue          # 空结论没有可判的东西（那是收敛问题，见 #24）
            out.append({
                "file": path.parent.name,
                "agent": agent,
                "fault": a.get("fault_id", "?"),
                "round": a.get("round_no"),
                "text": text,
                "correct": a.get("correct"),
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="裁判审计：自一致性 + 与关键词判定的分歧")
    ap.add_argument("--sample", type=int, default=6, help="抽多少条文本（按场景分层）")
    ap.add_argument("--reps", type=int, default=3, help="每条判几次（测自一致性）")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    # ---- 分层抽样：F8 / F4 优先（这两处才用得上"否定语境"判定）----
    all_at = load_attempts()
    picked: list[dict] = []
    for fid in ("F8", "F4", "F1", "F7"):
        for a in all_at:
            if a["fault"] == fid and len([p for p in picked if p["fault"] == fid]) < 2:
                picked.append(a)
    picked = picked[: args.sample * 2]

    cfg = LlmConfig.from_env()
    client = DeepSeekClient(cfg)

    print("=" * 96)
    print(f"  裁判审计　样本 {len(picked)} 条　每条判 {args.reps} 次")
    print("=" * 96)
    print()

    total_cost = 0.0
    consistent = 0
    per_case: list[dict] = []

    for item in picked:
        cause_label = CAUSE_LABEL.get(item["fault"])
        if cause_label is None:
            continue
        # 关键词侧：必须**任何场景**都能算出同口径的判定，否则"一致率"里
        # 会混进一堆 "n/a"，看起来像分歧、其实只是没算出来。
        #   多故障场景（F8）→ 用它声明的那个 Cause
        #   单故障场景       → 用它自己的 keyword_groups 现场构造一个 Cause
        sc = SCENARIOS.get(item["fault"])
        if sc is None:
            kw = "n/a"
        else:
            cause_obj = next(
                (c for c in sc.required_causes if c.name == cause_label), None
            ) or Cause(name=cause_label, keyword_groups=sc.keyword_groups)
            kw = keyword_verdict(item["text"], cause_obj)

        verdicts: list[str] = []
        for _ in range(args.reps):
            res = judge_cause(client, item["text"], cause_label, model=args.model)
            total_cost += res.cost_yuan
            verdicts.append(res.verdict)

        uniq = sorted(set(verdicts))
        stable = len(uniq) == 1
        consistent += stable
        per_case.append({**item, "cause": cause_label, "kw": kw,
                         "verdicts": verdicts, "stable": stable})

        mark = "✅" if stable else "⚠️"
        flag = "" if (kw == verdicts[0]) else "   ← 与关键词判定不一致"
        print(f"  {mark} {item['agent']:<8} {item['fault']} 第{item['round']}轮  [{cause_label}]")
        print(f"        关键词：{kw}　裁判：{verdicts}　{'稳定' if stable else '**不稳定**'}{flag}")

    agree = sum(1 for c in per_case if c["kw"] == c["verdicts"][0])
    print()
    print(f"  自一致性：{consistent}/{len(per_case)} 条在 {args.reps} 次判定里结论完全一致")
    print(f"  与关键词判定一致：{agree}/{len(per_case)}")
    print(f"  总成本：¥{total_cost:.6f}")

    print()
    print("── 不一致的样本（**必须人工读原文判谁对**，不许拿关键词当标准答案）──")
    shown = 0
    for c in per_case:
        if c["kw"] != c["verdicts"][0]:
            shown += 1
            print(f"  · {c['agent']} {c['fault']} 第{c['round']}轮 [{c['cause']}]")
            print(f"      关键词 {c['kw']} / 裁判 {c['verdicts'][0]}")
            print(f"      原文：{c['text'][:200]}")
    if not shown:
        print("  （无）")

    unstable = [c for c in per_case if not c["stable"]]
    if unstable:
        print()
        print("── ⚠️ 不稳定的样本（同一个输入，裁判给出不同结论）──")
        for c in unstable:
            print(f"  · {c['agent']} {c['fault']} 第{c['round']}轮：{c['verdicts']}")
        print("  ⇒ **temperature=0 不等于可复现**。接进评分前必须解决这一点，")
        print("     否则分数会带上一个「裁判抖动」的噪声带，而且没人知道它多大。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
