"""生成 L3 公开实例：**一个自包含的 HTML**，不需要服务器、不需要前端框架。

================================= 为什么是"一个静态文件" =================================

需求里写得很清楚：**极简单页**，不引前端大屏、不引框架。
而且这个项目的 FR-C 是**可复现** —— 所以这个页面不是"我截了张图"，
而是**由存档生成**：换一次运行、重跑一条命令，页面内容跟着变。

    .\\.venv\\Scripts\\python.exe scripts\\make_demo.py
    # → demo/rca-demo.html（双击就能看，不需要起服务）

================================= 页面上放什么 =================================

不是"我们做了个多 Agent 系统"这种话，而是**面试官能自己核对的东西**：

  1. 一句话结论（含**负结果**）
  2. 定稿数字表：baseline vs multi（同配置、同时段）
  3. **一次真实诊断的完整轨迹** —— 三个专员各调了什么工具、返回了什么、
     交叉质证怎么反驳、裁决怎么定案（数据来自 `traces/`，即 #29 补的那块）
  4. **诚实清单**：作废的题目、被推翻的结论、口径边界
  5. 封堵清单摘要（从 `docs/harness-log.md` 的总览表抽）

  ⚠️ 第 4 节是刻意放在**这么靠前**的位置的。
     一个把自己的错误逐条列出来的项目，比一个只报喜的项目可信得多 ——
     而"可信"正是这个东西要证明的唯一一件事。
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "runs" / "_eval"
OUT = ROOT / "demo" / "rca-demo.html"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def e(x) -> str:
    return html.escape(str(x), quote=True)


def latest(pattern: str) -> Path | None:
    """挑"最完整的那一次运行"。

    ⚠️ **不能按文件名取最后一个** —— 名字末位是时间戳，而最后一次可能只是一次
       **冒烟**（比如 `--judge` 只跑 2 个场景）。第一版就是这么写的，
       结果页面上展示的是 2 次尝试的冒烟数据，而不是 21 次的正式 baseline。
       （自检脚本当场抓到：页面上找不到 ¥0.0172。）

    ⇒ 按**尝试数**取最大的那个；并列时按名字取最后。
    """
    best: tuple[int, str, Path] | None = None
    for f in EVAL_DIR.glob(pattern):
        try:
            n = len(json.loads(f.read_text(encoding="utf-8"))["attempts"])
        except Exception:  # noqa: BLE001
            continue
        cand = (n, f.parent.name, f)
        if best is None or cand[:2] > best[:2]:
            best = cand
    return best[2] if best else None


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def totals(data: dict) -> dict:
    at = data["attempts"]
    n = len(at)
    return {
        "n": n,
        "acc": sum(1 for a in at if a["correct"]) / n,
        "steps": sum(a["steps"] for a in at) / n,
        "cost": sum(a["cost_yuan"] for a in at) / n,
        "conv": sum(1 for a in at if a["finished"]) / n,
        "json": sum(1 for a in at if a["parse_ok"]) / n,
        "total": sum(a["cost_yuan"] for a in at),
        "tier": data.get("pricing_tier", ""),
    }


def load_trace(data: dict, fault: str, rnd: int) -> list:
    for a in data["attempts"]:
        if a["fault_id"] == fault and a["round_no"] == rnd:
            tp = a.get("trace_path", "")
            if tp and (ROOT / tp).exists():
                return json.loads((ROOT / tp).read_text(encoding="utf-8"))
    return []


def seal_table() -> list[tuple[str, str]]:
    """从 harness-log 的总览表抽出（编号, 一句话描述）。"""
    log = (ROOT / "docs" / "harness-log.md").read_text(encoding="utf-8")
    out: list[tuple[str, str]] = []
    for line in log.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        head = cells[0].replace("*", "").strip()
        if re.fullmatch(r"#\d+|P\d+", head):
            desc = re.sub(r"[*`]", "", cells[1])[:64]
            out.append((head, desc))
    return out


def trace_html(step: dict) -> str:
    tool = e(step.get("tool", "?"))
    res = e((step.get("result") or "")[:900])
    args = e((step.get("args") or "")[:200])
    return (
        f'<details><summary><code>{tool}</code> <span class="dim">{args}</span></summary>'
        f"<pre>{res}</pre></details>"
    )


def build() -> str:
    base_p = latest("baseline-*/results.json")
    multi_p = latest("multi-*/results.json")
    if base_p is None:
        raise SystemExit("找不到 baseline 的 results.json")
    base, multi = load(base_p), load(multi_p) if multi_p else None
    # ★ 把选中的来源打出来 —— 免得又静默用错了一次冒烟运行
    print(f"  baseline 取：{base_p.parent.name}（{len(base['attempts'])} 次尝试）")
    if multi:
        print(f"  multi    取：{multi_p.parent.name}（{len(multi['attempts'])} 次尝试）")
    b = totals(base)
    m = totals(multi) if multi else None

    # 挑一次"多故障"的诊断来展示完整轨迹（F8：分工的价值与失效都在这里）
    demo_fault = "F8" if multi and any(a["fault_id"] == "F8" for a in multi["attempts"]) else "F1"
    demo_round = 1
    trace = load_trace(multi, demo_fault, demo_round) if multi else []
    verdict = ""
    if multi:
        for a in multi["attempts"]:
            if a["fault_id"] == demo_fault and a["round_no"] == demo_round:
                verdict = a.get("root_cause", "")

    rows = seal_table()
    seals = "".join(
        f"<tr><td><code>{e(i)}</code></td><td>{e(d)}</td></tr>" for i, d in rows
    )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RCA-Agent · 公开实例</title>
<style>
 :root{{--fg:#1a1a1a;--dim:#6b7280;--line:#e5e7eb;--bg:#fff;--accent:#0f766e;--warn:#b45309}}
 @media (prefers-color-scheme:dark){{:root{{--fg:#e8e8e8;--dim:#9ca3af;--line:#333;--bg:#111;--accent:#5eead4;--warn:#fbbf24}}}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--fg);
   font:16px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
 main{{max-width:920px;margin:0 auto;padding:48px 20px 96px}}
 h1{{font-size:30px;margin:0 0 6px}} h2{{font-size:20px;margin:44px 0 12px;
   padding-bottom:6px;border-bottom:1px solid var(--line)}}
 h3{{font-size:16px;margin:24px 0 8px;color:var(--accent)}}
 .lead{{color:var(--dim);margin:0 0 8px}}
 table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:15px}}
 th,td{{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}}
 th{{background:color-mix(in srgb,var(--fg) 4%,transparent);font-weight:600}}
 .num{{font-variant-numeric:tabular-nums}}
 .dim{{color:var(--dim);font-size:13px}}
 pre{{background:color-mix(in srgb,var(--fg) 5%,transparent);padding:10px;
   overflow:auto;font-size:12.5px;line-height:1.5;border-radius:6px}}
 code{{font-family:ui-monospace,Consolas,monospace;font-size:.92em}}
 details{{border:1px solid var(--line);border-radius:6px;padding:6px 10px;margin:6px 0}}
 summary{{cursor:pointer}}
 .warn{{color:var(--warn);font-weight:600}}
 blockquote{{margin:12px 0;padding:10px 14px;border-left:3px solid var(--accent);
   background:color-mix(in srgb,var(--accent) 7%,transparent)}}
</style></head><body><main>

<h1>RCA-Agent · 公开实例</h1>
<p class="lead">用确定性机制约束 LLM 的<strong>判断</strong>与<strong>行动</strong> ——
一个把「测量本身可信」当成第一目标的故障根因分析实验。</p>
<p class="dim">本页由存档生成（<code>scripts/make_demo.py</code>），不是截图。</p>

<h2>一、一句话结论</h2>
<blockquote>
 <strong>多 Agent 没有可测的价值增量。</strong>
 在同一个世界里，单 Agent 用 <span class="num">¥{b['cost']:.4f}</span>/次拿到
 <span class="num">{b['acc']:.0%}</span> 准确率；
 三专员 + 交叉质证 + 裁决用 <span class="num">¥{m['cost']:.4f}</span>/次
 （<strong>{m['cost']/b['cost']:.1f}×</strong>）拿到
 <span class="num">{m['acc']:.0%}</span> —— <strong>准确率没有任何优势</strong>。
</blockquote>
<p class="dim">比这个负结果本身更重要的，是它<strong>是被测量出来的</strong>：
一路上有六七次得到过相反的结论，每一次都是「尺子」坏了（见第四节）。</p>

<h2>二、定稿数字（同配置 · 同时段 · 同一评分路径）</h2>
<table>
<tr><th>指标</th><th>baseline（单 Agent）</th><th>multi（三专员+质证+裁决）</th><th>倍数</th></tr>
<tr><td>样本</td><td class="num">{b['n']} 次尝试（7 场景 × 3 轮）</td>
    <td class="num">{m['n']} 次尝试（F1/F8 × 3 轮）</td><td>—</td></tr>
<tr><td>准确率</td><td class="num"><strong>{b['acc']:.0%}</strong></td>
    <td class="num"><strong>{m['acc']:.0%}</strong></td><td>—</td></tr>
<tr><td>步数（LLM 轮次）</td><td class="num">{b['steps']:.1f}</td>
    <td class="num">{m['steps']:.1f}</td><td class="num">{m['steps']/b['steps']:.1f}×</td></tr>
<tr><td><strong>成本 / 次诊断</strong></td><td class="num"><strong>¥{b['cost']:.4f}</strong></td>
    <td class="num"><strong>¥{m['cost']:.4f}</strong></td>
    <td class="num"><strong>{m['cost']/b['cost']:.1f}×</strong></td></tr>
<tr><td>收敛率</td><td class="num">{b['conv']:.0%}</td><td class="num">{m['conv']:.0%}</td><td>—</td></tr>
<tr><td>JSON 合规率</td><td class="num">{b['json']:.0%}</td><td class="num">{m['json']:.0%}</td><td>—</td></tr>
</table>
<p class="dim">计价时段：baseline <code>{e(b['tier'])}</code> ·
multi <code>{e(m['tier'])}</code>（峰时单价是谷时的 2 倍，<strong>只与同时段比较</strong>）。
有效场景 7 个 —— <strong>F2 已作废</strong>（它的标准答案是反的，见第四节）。
⚠️ multi 只跑了 F1/F8 两个场景，其余五个<strong>未测</strong>。</p>

<h2>三、一次真实诊断的完整轨迹</h2>
<p class="dim">场景 <code>{e(demo_fault)}</code>（多故障叠加：外部风控变慢 + order 内存泄漏），
第 {demo_round} 轮。下面是<strong>原样存档</strong>的工具调用与返回 ——
页面不替它修饰任何东西。</p>
<h3>最终裁决</h3>
<blockquote>{e(verdict)}</blockquote>
<h3>三个专员 + 交叉质证各自做了什么</h3>
{"".join(f'<h3>{e(p.get("phase","?"))} / {e(p.get("role","?"))}</h3>' + "".join(trace_html(s) for s in p.get("steps", [])) for p in trace) or '<p class="dim">（这次运行没有留下轨迹 —— 轨迹存档是后来才补上的，#29）</p>'}

<h2>四、诚实清单（刻意放在前面）</h2>
<h3>作废的题目</h3>
<p><strong>F2「连接池耗尽」</strong>：它声明的标准答案是「外部风控变慢是根因、池耗尽是症状」。
用对照实验一测——<strong>只把池改小、下游完全健康时，照样有 851 个 5xx</strong>；
而池保持 64、风控同样 800ms 时，<strong>5xx = 0</strong>。
⇒ 那道题把答对的扣分、把答错的加分。已作废（保留为反例）。</p>

<h3>被推翻过的结论（每次都是「尺子」坏了）</h3>
<table>
<tr><th>曾经以为</th><th>真相</th><th>性质</th></tr>
<tr><td>baseline 83.3%，多 Agent 有希望</td><td>评分把「被否掉的提及」算作命中</td><td>评分口径错</td></tr>
<tr><td>多 Agent 在 F2 上失败，命题被否定</td><td>场景答案是反的，它答的才对</td><td>题目错</td></tr>
<tr><td>多 Agent 在一次对照里 3/3 vs 0/3，突破</td><td>裁决把第二个故障降级了，100% 是假阳性</td><td>评分口径错</td></tr>
<tr><td>单 Agent 找不到第二个故障，需要分工</td><td>任务从没要求它「找全」</td><td>任务定义错</td></tr>
<tr><td>成本涨了 2.1 倍</td><td>我的计价模型漏了法定节假日，虚高一倍</td><td>计价模型错</td></tr>
<tr><td>给输出加上限能省 23% 成本</td><td>截断 JSON → 那几次结论作废，收敛率归零</td><td>省了钱，赔了有效性</td></tr>
<tr><td>输出量涨了 77%</td><td>录制文件是追加的，我按整文件统计</td><td>测量工具错</td></tr>
</table>

<h3>口径边界（数字必须连同这些一起读）</h3>
<ul>
<li>multi 只测了 <strong>F1/F8</strong> 两个场景；其余五个未测</li>
<li>每侧仅 <strong>3 轮</strong>；只测 <code>deepseek-flash</code>，未测 <code>v4-pro</code></li>
<li>评分 = 关键词判定（主）+ LLM 裁判（独立交叉校验，<strong>不参与</strong>正确性计算）</li>
<li>被诊断系统是<strong>自建</strong>的，不是生产系统 —— 故障是注入的，不是自然发生的</li>
</ul>

<h2>五、封堵清单（{len(rows)} 条）</h2>
<p class="dim">项目规则：<strong>一条错误只有在回归用例能变红之后，才算封堵。</strong>
下表的「能变红」由变异测试证明 —— 每个变异体会把缺陷重新注回一份代码副本，
用例必须变红，否则报 <code>NOT SEALED</code>。</p>
<table><tr><th>编号</th><th>缺陷</th></tr>{seals}</table>

</main></body></html>
"""


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    html_text = build()
    OUT.write_text(html_text, encoding="utf-8")
    kb = len(html_text.encode("utf-8")) / 1024
    print(f"已生成：{OUT.relative_to(ROOT)}（{kb:.0f} KB，自包含、无需服务器）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
