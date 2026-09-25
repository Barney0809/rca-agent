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
from eval.scenarios import (  # noqa: E402
    CAUSE_LABELS,
    SCENARIOS,
    Cause,
    keyword_verdict,
)
from rca.llm.provider import DeepSeekClient, LlmConfig  # noqa: E402

EVAL_DIR = ROOT / "runs" / "_eval"

# ⚠️ 这些"原因标签"**只有一份**，在 `eval/scenarios.py` 的 `CAUSE_LABELS` 里。
#    本脚本曾经自己抄了一份（内容当时一致），而 `scenarios.py` 那段的注释恰好写着：
#    "都从这里取。各自写一份的话，两边迟早走散 —— 而'两份判定不一致'这种 bug
#     极难发现（本项目已经栽过一次）。"
#    ⇒ 也就是说：**我抄了那句注释警告过一次的东西**（harness-log #43）。
#      现在直接引用同一份，并且有用例守着"必须是同一个对象，不是副本"。

NA = "n/a"


def keyword_side(fault: str, text: str) -> str:
    """关键词侧的判定（`asserted` / `dismissed` / `absent`），算不出来才是 `NA`。

    P7：这个审计脚本曾经把"算不出来"输出成 `n/a`，**然后又把它计进"不一致"** ——
    于是得到一份误导性的一致率：看起来像"裁判与关键词分歧"，其实只是没算出来。

    ⇒ 两条规则：
      ① 只要能算就必须算出来（未知场景才返回 `NA`）；
      ② `NA` 由 `agreement_summary()` **从分母里排除**，并且要在输出里说出来。

    抽成独立函数不只是为了整洁：它原本内联在 `main()` 里，**根本没法被单独测试**
    （同一个毛病见 #33 的成本分解切段）。
    """
    sc = SCENARIOS.get(fault)
    label = CAUSE_LABELS.get(fault)
    if sc is None or label is None:
        return NA
    # 多故障场景（F8）用它声明的那个 Cause；单故障场景用它自己的 keyword_groups
    # 现场构造一个 —— 因为场景定义里可能没把这条 Cause 单独列出来。
    cause_obj = next((c for c in sc.required_causes if c.name == label), None) or Cause(
        name=label, keyword_groups=sc.keyword_groups
    )
    return keyword_verdict(text, cause_obj)


def agreement_summary(cases: list[dict]) -> tuple[int, int, int]:
    """(一致数, 可比数, 被排除的条数)。

    ⚠️ `NA` **不进分母** —— 否则"没算出来"会被当成"判得不一致"（P7）。
    """
    comparable = [c for c in cases if c["kw"] != NA]
    agree = sum(1 for c in comparable if c["kw"] == c["verdicts"][0])
    return agree, len(comparable), len(cases) - len(comparable)


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
        cause_label = CAUSE_LABELS.get(item["fault"])
        if cause_label is None:
            continue
        # 关键词侧的判定：**任何已知场景都要能算出来**，不许退化成 n/a，
        # 否则"一致率"里会混进一堆"没算出来"，看起来像分歧（P7）。
        kw = keyword_side(item["fault"], item["text"])

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
        if kw == NA:
            flag = "   ← 关键词侧**算不出来**（已从一致率分母里排除）"
        elif kw == verdicts[0]:
            flag = ""
        else:
            flag = "   ← 与关键词判定不一致"
        print(f"  {mark} {item['agent']:<8} {item['fault']} 第{item['round']}轮  [{cause_label}]")
        print(f"        关键词：{kw}　裁判：{verdicts}　{'稳定' if stable else '**不稳定**'}{flag}")

    agree, comparable, na_n = agreement_summary(per_case)
    print()
    print(f"  自一致性：{consistent}/{len(per_case)} 条在 {args.reps} 次判定里结论完全一致")
    print(f"  与关键词判定一致：{agree}/{comparable}", end="")
    if na_n:
        print(f"　（另有 {na_n} 条关键词侧算不出来，**已从分母排除** —— "
              f"它们不是分歧，见 P7）")
    else:
        print()
    print(f"  总成本：¥{total_cost:.6f}")

    print()
    print("── 不一致的样本（**必须人工读原文判谁对**，不许拿关键词当标准答案）──")
    shown = 0
    for c in per_case:
        if c["kw"] != NA and c["kw"] != c["verdicts"][0]:
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
