"""从录制文件分解 multi 一次诊断的成本 —— 回答"贵在哪"。

用法：
    .\\.venv\\Scripts\\python.exe scripts/cost_breakdown.py runs/_recordings/record-deepseek-flash.ndjson

为什么要有这个脚本：
    D11 发现同配置下 multi 的成本比早先翻了 2.1 倍（¥0.0674 → ¥0.1414），
    而**步数只涨了 7%**。于是有两种假设，**修法完全相反**：

        假设A 上下文变长   → 每步要重发的历史越来越长 → 该做**上下文压缩**
        假设B 缓存命中下降 → 同样的历史没被复用        → 该做**prefix 稳定性**

    ⇒ 按本项目纪律：**先测出来再动手。** 这个脚本就是那个测量。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# DeepSeek flash 非高峰价（元 / 百万 token）。
# 峰值是它的两倍；录制里不区分时段，所以这里统一按非高峰算，
# **比值**（命中 vs 未命中 vs 输出）才是要看的东西。
PRICE_HIT = 0.02
PRICE_MISS = 1.0
PRICE_OUT = 4.0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only_last = "--all" not in sys.argv
    path = Path(args[0]) if args else None
    if path is None or not path.exists():
        print("用法：python scripts/cost_breakdown.py <recordings.ndjson> [--all]")
        print("  默认只分析**最近一次运行**（录制文件是追加的，可能含多次）")
        return 2

    # ⚠️ 录制文件是**追加**写入的（`Recorder` 用 mode="a"），
    #    所以一个文件里可能累积了**好几次运行**。
    #    直接按整个文件统计，会把两三次运行混在一起 —— 我就这么错过一次
    #    （harness-log #33：拿 33 次与「33+24」次比，得出假的「输出 +77%」）。
    #
    #    一次运行的边界很好认：**每次诊断只有 1 条 `coordinator` 调用**，
    #    所以按 coordinator 切段，取最后一段就是最近一次运行。
    raw = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    n_runs = sum(1 for o in raw if o.get("tag") == "coordinator") or 1
    if only_last and n_runs > 1:
        # ⚠️ 一次运行的**结尾**是它自己的 `coordinator` 调用（那是最后一个环节）。
        #    所以第 k 次运行 = 「上一个 coordinator 之后」.. 「本次 coordinator」（含）。
        #    第一版写成"从最后一个 coordinator 往后切"，那只切到 coordinator 自己 —— 又错一次。
        idx = [i for i, o in enumerate(raw) if o.get("tag") == "coordinator"]
        start = idx[-2] + 1 if len(idx) >= 2 else 0
        raw = raw[start: idx[-1] + 1]

    rows = []
    for i, o in enumerate(raw):
        u = (o.get("response") or {}).get("usage") or {}
        hit = int(u.get("prompt_cache_hit_tokens") or 0)
        miss = int(u.get("prompt_cache_miss_tokens") or 0)
        if not hit and not miss:
            # 兜底：有些返回只给 prompt_tokens + cached_tokens
            pt = int(u.get("prompt_tokens") or 0)
            hit = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
            miss = max(pt - hit, 0)
        rows.append({
            "i": i + 1,
            "tag": o.get("tag", ""),
            "n_messages": o.get("n_messages", 0),
            "hit": hit,
            "miss": miss,
            "prompt": hit + miss,
            "out": int(u.get("completion_tokens") or 0),
        })

    tot_hit = sum(r["hit"] for r in rows)
    tot_miss = sum(r["miss"] for r in rows)
    tot_out = sum(r["out"] for r in rows)
    tot_prompt = tot_hit + tot_miss
    cost_hit = tot_hit / 1e6 * PRICE_HIT
    cost_miss = tot_miss / 1e6 * PRICE_MISS
    cost_out = tot_out / 1e6 * PRICE_OUT
    total = cost_hit + cost_miss + cost_out

    print("=" * 92)
    print(f"成本分解：{path.name}　{len(rows)} 次调用")
    print("=" * 92)
    if n_runs > 1:
        scope = "整文件（含多次运行）" if not only_last else f"最近一次运行（文件里共 {n_runs} 次）"
        print(f"  ⚠️ 本文件累积了 {n_runs} 次运行，当前统计范围：{scope}")
        print("     （录制是**追加**写入 —— 按整文件统计会把多次运行混在一起，见 #33）")
        print()
    print(f"  输入：命中 {tot_hit:,} tok　未命中 {tot_miss:,} tok　"
          f"（命中率 {tot_hit / max(tot_prompt, 1):.1%}）")
    print(f"  输出：{tot_out:,} tok")
    print(f"  输入合计 {tot_prompt:,} tok / 输出合计 {tot_out:,} tok "
          f"= {tot_prompt / max(tot_out, 1):.1f} : 1")
    print()
    print("  成本构成（按非高峰价）")
    print(f"    缓存命中输入  ¥{cost_hit:.4f}　{cost_hit / max(total, 1e-9):5.1%}")
    print(f"    未命中输入    ¥{cost_miss:.4f}　{cost_miss / max(total, 1e-9):5.1%}")
    print(f"    输出          ¥{cost_out:.4f}　{cost_out / max(total, 1e-9):5.1%}")
    print(f"    合计          ¥{total:.4f}")

    print()
    print("  逐次调用（上下文是否越滚越大 / 命中率是否掉）")
    print(f"    {'#':>3} {'tag':<22} {'msgs':>4} {'输入':>8} {'命中':>8} {'未命中':>8} {'输出':>6}")
    for r in rows:
        print(f"    {r['i']:>3} {r['tag'][:22]:<22} {r['n_messages']:>4} "
              f"{r['prompt']:>8,} {r['hit']:>8,} {r['miss']:>8,} {r['out']:>6,}")

    # ---- 判定 ----
    half = len(rows) // 2 or 1
    early = rows[:half]
    late = rows[half:]
    avg_msg_e = sum(r["n_messages"] for r in early) / len(early)
    avg_msg_l = sum(r["n_messages"] for r in late) / len(late)
    hit_e = sum(r["hit"] for r in early) / max(sum(r["prompt"] for r in early), 1)
    hit_l = sum(r["hit"] for r in late) / max(sum(r["prompt"] for r in late), 1)

    print()
    print("=" * 92)
    print("  判定")
    print("=" * 92)
    print(f"  前半段：平均 {avg_msg_e:.1f} 条消息，命中率 {hit_e:.1%}")
    print(f"  后半段：平均 {avg_msg_l:.1f} 条消息，命中率 {hit_l:.1%}")
    grew = avg_msg_l - avg_msg_e
    print(f"  上下文增长：{grew:+.1f} 条消息")
    print(f"  命中率变化：{hit_l - hit_e:+.1%}")
    print()
    if cost_miss > cost_out and cost_miss > cost_hit:
        print("  ⇒ **未命中的输入是最大单项** —— 优先做 prefix 稳定性 / 缓存命中。")
    elif cost_out > cost_miss:
        print("  ⇒ **输出是最大单项** —— 优先做结论精简（少写、结构化），"
              "而不是压缩输入。")
    else:
        print("  ⇒ 命中的输入占大头 —— 说明缓存已经在工作，"
              "要继续降本只能减少总输入量（上下文压缩）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
