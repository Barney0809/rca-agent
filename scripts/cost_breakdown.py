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
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if path is None or not path.exists():
        print("用法：python scripts/cost_breakdown.py <recordings.ndjson>")
        return 2

    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        o = json.loads(line)
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
