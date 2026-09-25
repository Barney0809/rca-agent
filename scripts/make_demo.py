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
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "runs" / "_eval"
OUT = ROOT / "demo" / "rca-demo.html"

# 本脚本是**直接执行**的（不经 pytest，所以拿不到 pyproject 里的 pythonpath），
# 而它现在需要两样东西 —— 按仓库里其它脚本的写法自己挂路径：
#   · `src`  → 真实计价模型（is_peak_hour）
#   · 仓库根 → `eval.scenarios` 的**当前判据**（页面要按当前判据重算准确率，见 #48）
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from rca.llm.provider import is_peak_hour  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def e(x) -> str:
    return html.escape(str(x), quote=True)


def _status_cls(status: str) -> str:
    """状态列的颜色：机器证明过的才是"好"，靠人的一律显眼。"""
    if "🟢" in status:
        return "ok"
    if "🟡" in status or "⚠" in status:
        return "warn"
    return "bad"


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


# 故障目录里的一句话描述（只用于第三节的标题）。
# ⚠️ **不能无条件套用** —— 第三节的场景是**挑出来**的（优先 F8，没有 F8 才退回 F1），
#    如果描述写死在模板里，退回 F1 的那天页面会拿 F8 的说明去描述 F1 的轨迹。
#    未知故障一律留空，宁可少说。
FAULT_NOTE = {
    "F1": "外部风控变慢（下游延迟）",
    "F8": "多故障叠加：外部风控变慢 + order 内存泄漏",
}


def tier_of(data: dict) -> tuple[str, str]:
    """计价时段 + **它的来源**。

    ⚠️ 存档里的 `pricing_tier` 是**后来才加的字段** —— 早于它的存档里没有这个键。
       第一版直接 `data.get("pricing_tier", "")`，于是在页面上渲染出一个
       **空白单元格**（`baseline <code></code>`），而那正是"只与同时段比较"
       这句话的落点。空白看起来像"没这回事"，实际是"当时没记"。
       自检脚本用 `baseline <code></code>` 这个模式抓到了它。

    ⇒ 存档没记就按 `started_at` 用**真实计价模型**重算，并把「重算」写出来 ——
       读者能看出哪个数字是当场记的、哪个是事后推的。
    """
    rec = str(data.get("pricing_tier") or "").strip()
    if rec:
        return rec, "存档记录"
    started = str(data.get("started_at") or "").strip()
    if started:
        try:
            when = datetime.fromisoformat(started)
        except ValueError:
            return "未记录", "时刻不可解析"
        return ("peak" if is_peak_hour(when) else "off_peak"), "按开始时刻重算"
    return "未记录", "无开始时刻，无法重算"


def rescore_acc(data: dict) -> float | None:
    """按**当前判据**重算准确率（只读存档里的答案文本，不花一分钱）。

    ⚠️ 为什么页面上要同时给两个数（#48 之后加的）：

        存档里的 `correct` 是**当时那版判据**给出的结论；而判据本身后来修过
        （关键词匹配漏了英文词形：`retry` 匹配不上 `retries`）。
        · 只显示存档值 → 拿一个**已知有缺陷**的判定当结论；
        · 只显示重算值 → 抹掉了"存档当时是什么样"，也就丢掉了可追溯性。

        ⇒ 两个都给，并注明差异是从哪来的。这也是"本页由存档生成"的一部分：
          重算所用的是**同一份存档文本** + 当前的判据代码。

    无效场景（F2）与未知场景按原样保留，不参与重算。
    """
    try:
        from eval.scenarios import SCENARIOS, Cause, keyword_verdict
    except Exception:  # noqa: BLE001 —— 取不到判据就不显示这一栏，而不是编一个数
        return None

    at = [a for a in data["attempts"]
          if not (SCENARIOS.get(a["fault_id"])
                  and SCENARIOS[a["fault_id"]].invalidated_reason)]
    if not at:
        return None
    ok = 0
    for a in at:
        sc = SCENARIOS.get(a["fault_id"])
        if sc is None:
            ok += bool(a["correct"])
            continue
        v = keyword_verdict(a.get("root_cause") or "",
                            Cause(a["fault_id"], sc.keyword_groups))
        ok += (v == "asserted")
    return ok / len(at)


def totals(data: dict) -> dict:
    at = data["attempts"]
    n = len(at)
    faults = sorted({a["fault_id"] for a in at})
    tier, tier_src = tier_of(data)
    acc_now = rescore_acc(data)
    return {
        "n": n,
        "acc": sum(1 for a in at if a["correct"]) / n,
        # 按**当前判据**重算出来的准确率（可能与存档值不同，见 #48）
        "acc_now": acc_now,
        "acc_changed": acc_now is not None
        and abs(acc_now - sum(1 for a in at if a["correct"]) / n) > 1e-9,
        "steps": sum(a["steps"] for a in at) / n,
        "cost": sum(a["cost_yuan"] for a in at) / n,
        "conv": sum(1 for a in at if a["finished"]) / n,
        "json": sum(1 for a in at if a["parse_ok"]) / n,
        "total": sum(a["cost_yuan"] for a in at),
        # 样本口径**从存档推导**，不写死。写死的话，换一次运行页面就会
        # 继续宣称"7 场景 × 3 轮"（见 harness-log #37）。
        "scen": len(faults),
        "faults": faults,
        "rounds": len({a["round_no"] for a in at}),
        "tier": tier,
        "tier_src": tier_src,
        # 同样不许留白：没记就写"未记录"（#37 的推广形式）
        "started_at": str(data.get("started_at") or "").strip() or "未记录",
    }


def load_trace(data: dict, fault: str, rnd: int) -> list:
    for a in data["attempts"]:
        if a["fault_id"] == fault and a["round_no"] == rnd:
            tp = a.get("trace_path", "")
            if tp and (ROOT / tp).exists():
                return json.loads((ROOT / tp).read_text(encoding="utf-8"))
    return []


OVERVIEW_HEADING = "封堵状态总览"


def seal_table() -> list[tuple[str, str, str]]:
    """从 harness-log 的**封堵状态总览表**抽出（编号, 一句话描述, 状态）。

    ⚠️ 两个坑，都是补用例时抓到的：

    1. **不能"抓文件里所有像清单的表格行"。** 文件里还有别的表也以编号开头
       （`P5~P9` 那节的逐条表就是这样），第一版于是让 `P5`~`P9` **各出现两次**，
       页面写着"封堵清单（50 条）"而真实条目是 45 条。（我此前手工核对时
       拿页面和同一个解析器比，那是个恒真式 —— 50 = 50，什么也没证明。）
    2. **不能把状态丢掉。** 总览表里 `P1`/`P2`/`P3` 是"⚠️/❌ 靠人"，
       而页面上原有那句话是"每条都能由变异测试证明能变红" ——
       把那三条**也说成了机器证明过的**。状态列必须如实带上。

    ⇒ 只扫总览小节（标题 → 下一个 `## `），再按编号去重（首次出现优先）。
    """
    log = (ROOT / "docs" / "harness-log.md").read_text(encoding="utf-8")
    lines = log.splitlines()

    start = next((i for i, ln in enumerate(lines) if ln.startswith("##") and OVERVIEW_HEADING in ln), None)
    if start is not None:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        lines = lines[start:end]

    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for line in lines:
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        head = cells[0].replace("*", "").strip()
        if not re.fullmatch(r"#\d+|P\d+", head) or head in seen:
            continue
        seen.add(head)
        desc = re.sub(r"[*`]", "", cells[1])[:64]
        status = next((c for c in reversed([re.sub(r"[*`]", "", c) for c in cells[2:]]) if c), "")
        out.append((head, desc, status[:24]))
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
    # ⚠️ 第二版模板无条件引用 multi 的字段，所以 multi 缺失时**在这里就报清楚**，
    #    而不是等 f-string 抛一个 `NoneType is not subscriptable`。
    if multi_p is None:
        raise SystemExit("找不到 multi 的 results.json")
    base, multi = load(base_p), load(multi_p)
    # ★ 把选中的来源打出来 —— 免得又静默用错了一次冒烟运行
    print(f"  baseline 取：{base_p.parent.name}（{len(base['attempts'])} 次尝试）")
    print(f"  multi    取：{multi_p.parent.name}（{len(multi['attempts'])} 次尝试）")
    b, m = totals(base), totals(multi)

    # 样本口径与计价时段**全部由存档推出**（#37：写死的那些迟早会和存档对不上）
    b_sample = f"{b['n']} 次尝试（{b['scen']} 场景 × {b['rounds']} 轮）"
    m_faults = "/".join(m["faults"])
    m_sample = f"{m['n']} 次尝试（{m_faults} × {m['rounds']} 轮）"
    untested = b["scen"] - m["scen"]
    print(f"  样本口径：baseline {b_sample}；multi {m_sample}")
    print(f"  计价时段：baseline {b['tier']}（{b['tier_src']}）"
          f"；multi {m['tier']}（{m['tier_src']}）")

    # ★ 结论的**措辞也由数字推出**（#37 的推广）：不能再手打"准确率没有任何优势" ——
    #   一旦 multi 补齐到全部场景、准确率变了，手打的句子就会当场变成假话。
    #
    #   ⚠️ 用**当前判据**的数字下结论（`acc_now`），存档值只作为对照显示：
    #      拿一个"已知有缺陷的判定"当结论，等于把 #48 修过的错又背回去。
    b_eff = b["acc_now"] if b.get("acc_now") is not None else b["acc"]
    m_eff = m["acc_now"] if m.get("acc_now") is not None else m["acc"]
    acc_delta_pp = (m_eff - b_eff) * 100
    if abs(acc_delta_pp) < 0.05:
        headline = "多 Agent 没有可测的价值增量。"
        verdict_word = "准确率<strong>没有变化</strong>"
    elif acc_delta_pp < 0:
        headline = "多 Agent 不仅没有价值增量，而且<strong>更差</strong>。"
        verdict_word = f"准确率反而<strong>低了 {abs(acc_delta_pp):.1f} 个百分点</strong>"
    else:
        headline = "多 Agent <strong>更准</strong>，但要为此付出高得多的成本。"
        verdict_word = f"准确率<strong>高了 {acc_delta_pp:.1f} 个百分点</strong>"
    print(f"  结论措辞（按当前判据）：{headline}／{verdict_word}")

    # ★ 两个口径：**存档当时的判定** vs **按当前判据重算**（#48）
    #
    #   ⚠️ 为什么两个都要给：只给存档值 = 拿一个已知有缺陷的判定当结论；
    #      只给重算值 = 抹掉"存档当时是什么样"。重算只用同一份存档文本 + 当前判据代码，
    #      零成本、可复算 —— 这是"本页由存档生成"的一部分。
    acc_shown = f"{m_eff * 100:.1f}%"
    if m["acc_now"] is not None and m["acc_changed"]:
        acc_shown = (f"{m_eff * 100:.1f}%<span class='dim'>（按当前判据）</span> / "
                     f"{m['acc'] * 100:.1f}%<span class='dim'>（存档判定）</span>")
    acc_two_calibers = ""
    if m["acc_changed"] or b["acc_changed"]:
        b_now = f" → 按当前判据 <strong>{b['acc_now'] * 100:.1f}%</strong>" if b.get("acc_now") is not None else ""
        m_now = f" → 按当前判据 <strong>{m['acc_now'] * 100:.1f}%</strong>" if m.get("acc_now") is not None else ""
        acc_two_calibers = (
            "<p class='dim'>⚠️ <strong>两个口径都给</strong>：存档里的判定是"
            "<strong>当时那版判据</strong>给出的；判据后来修过一次"
            "（关键词匹配漏了英文词形：<code>retry</code> 匹配不上 <code>retries</code>，"
            "见 harness-log #48），所以同一份存档按今天的判据重算会不一样。"
            f"baseline 存档 {b['acc'] * 100:.1f}%{b_now}；"
            f"multi 存档 {m['acc'] * 100:.1f}%{m_now}。"
            "上面的结论用的是<strong>按当前判据</strong>的那个数；"
            "重算只用同一份存档文本 + 当前判据代码，不花一分钱、可自行复跑。</p>"
        )

    # 两侧样本是否对等（决定"能不能直接比准确率"）
    same_sample = b["scen"] == m["scen"] and b["rounds"] == m["rounds"]
    if same_sample:
        sample_note = ("两侧覆盖**同一批**场景与轮数 ⇒ 这是**可对等比较**。")
    else:
        sample_note = (f"⚠️ multi 只跑了 {m_faults} 这 {m['scen']} 个场景，"
                       f"其余 {untested} 个<strong>未测</strong> —— "
                       f"「准确率」的差别只能在这 {m['scen']} 个场景上说。")
    print(f"  样本对等：{same_sample}")

    # 挑一次"多故障"的诊断来展示完整轨迹（F8：分工的价值与失效都在这里）
    demo_fault = "F8" if "F8" in m["faults"] else m["faults"][0]
    demo_round = 1
    demo_note = FAULT_NOTE.get(demo_fault, "")
    trace = load_trace(multi, demo_fault, demo_round)
    verdict = ""
    for a in multi["attempts"]:
        if a["fault_id"] == demo_fault and a["round_no"] == demo_round:
            verdict = a.get("root_cause", "")
    if not demo_note:
        print(f"  ⚠️ 场景 {demo_fault} 不在 FAULT_NOTE 里 —— 标题不带描述（宁可少说）")

    rows = seal_table()
    n_sealed = sum(1 for _i, _d, s in rows if "🟢" in s)
    n_human = len(rows) - n_sealed
    human_ids = "、".join(i for i, _d, s in rows if "🟢" not in s)
    print(f"  封堵清单：{len(rows)} 条（变异测试证明 {n_sealed}，靠人 {n_human}）")
    seals = "".join(
        f'<tr><td><code>{e(i)}</code></td><td>{e(d)}</td>'
        f'<td class="{_status_cls(s)}">{e(s)}</td></tr>'
        for i, d, s in rows
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
 .ok{{color:var(--accent)}}
 .bad{{color:#b91c1c;font-weight:600}}
 @media (prefers-color-scheme:dark){{.bad{{color:#fca5a5}}}}
 blockquote{{margin:12px 0;padding:10px 14px;border-left:3px solid var(--accent);
   background:color-mix(in srgb,var(--accent) 7%,transparent)}}
</style></head><body><main>

<h1>RCA-Agent · 公开实例</h1>
<p class="lead">用确定性机制约束 LLM 的<strong>判断</strong>与<strong>行动</strong> ——
一个把「测量本身可信」当成第一目标的故障根因分析实验。</p>
<p class="dim">本页由存档生成（<code>scripts/make_demo.py</code>），不是截图 ——
而<strong>存档与录制也随仓库提交</strong>（<code>runs/_eval/</code>），
所以重跑一次生成器就能把这一页**逐字节复现**出来：不需要 API key，不花一分钱。</p>

<h2>一、一句话结论</h2>
<blockquote>
 <strong>{headline}</strong>
 在同一个世界里，单 Agent 用 <span class="num">¥{b['cost']:.4f}</span>/次拿到
 <span class="num">{b['acc'] * 100:.1f}%</span> 准确率；
 三专员 + 交叉质证 + 裁决用 <span class="num">¥{m['cost']:.4f}</span>/次
 （<strong>{m['cost']/b['cost']:.1f}×</strong>）拿到
 <span class="num">{acc_shown}</span> —— {verdict_word}。
</blockquote>
{acc_two_calibers}
<p class="dim">比这个负结果本身更重要的，是它<strong>是被测量出来的</strong>：
一路上有七八次得到过相反的结论，每一次都是「尺子」坏了（见第四节）。</p>

<h2>二、定稿数字（同配置 · 同时段 · 同一评分路径）</h2>
<table>
<tr><th>指标</th><th>baseline（单 Agent）</th><th>multi（三专员+质证+裁决）</th><th>倍数</th></tr>
<tr><td>样本</td><td class="num">{e(b_sample)}</td>
    <td class="num">{e(m_sample)}</td><td>—</td></tr>
<tr><td>准确率</td><td class="num"><strong>{b['acc'] * 100:.1f}%</strong></td>
    <td class="num"><strong>{m['acc'] * 100:.1f}%</strong></td><td>—</td></tr>
<tr><td>步数（LLM 轮次）</td><td class="num">{b['steps']:.1f}</td>
    <td class="num">{m['steps']:.1f}</td><td class="num">{m['steps']/b['steps']:.1f}×</td></tr>
<tr><td><strong>成本 / 次诊断</strong></td><td class="num"><strong>¥{b['cost']:.4f}</strong></td>
    <td class="num"><strong>¥{m['cost']:.4f}</strong></td>
    <td class="num"><strong>{m['cost']/b['cost']:.1f}×</strong></td></tr>
<tr><td>收敛率</td><td class="num">{b['conv']:.0%}</td><td class="num">{m['conv']:.0%}</td><td>—</td></tr>
<tr><td>JSON 合规率</td><td class="num">{b['json']:.0%}</td><td class="num">{m['json']:.0%}</td><td>—</td></tr>
</table>
<p class="dim">计价时段：baseline <code>{e(b['tier'])}</code>（{e(b['tier_src'])}） ·
multi <code>{e(m['tier'])}</code>（{e(m['tier_src'])}）
—— 峰时单价是谷时的 2 倍，<strong>只与同时段比较</strong>。
起跑时刻：baseline <code>{e(b['started_at'])}</code> ·
multi <code>{e(m['started_at'])}</code>（法定节假日<strong>全天</strong>按谷时计费）。
有效场景 {b['scen']} 个 —— <strong>F2 已作废</strong>（它的标准答案是反的，见第四节）。
⚠️ {sample_note}</p>

<h2>三、一次真实诊断的完整轨迹</h2>
<p class="dim">场景 <code>{e(demo_fault)}</code>{('（' + e(demo_note) + '）') if demo_note else ''}，
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
<tr><td>multi 在 F4 上失败，一定是评分口径冤枉了它</td><td>读代码 + 对存档实测 + 两把尺子逐条核对：<strong>不是</strong>尺子问题，multi 真的更差</td><td>我的怀疑错，结论对</td></tr>
</table>

<h3>口径边界（数字必须连同这些一起读）</h3>
<ul>
<li>两侧样本：baseline {e(b_sample)}；multi {e(m_sample)}</li>
<li>每侧仅 <strong>3 轮</strong>；只测 <code>deepseek-flash</code>（v4-pro 另做过一组对照，
  见 <code>docs/06</code>）</li>
<li>评分 = 关键词判定（主）+ LLM 裁判（独立交叉校验，<strong>不参与</strong>正确性计算）</li>
<li>被诊断系统是<strong>自建</strong>的，不是生产系统 —— 故障是注入的，不是自然发生的</li>
<li>F4 上 multi 明显更差（1/3），两把尺子逐条核对过 —— 见
  <code>docs/harness-log.md</code> #44</li>
<li>⚠️ <strong>幅度不稳定</strong>：给出的差距全部来自 F4 一个场景，而单场景每轮只跑 3 次 ——
  同配置重跑 baseline，它在 F4 上自己就从 3/3 变成 2/3。三次配对实验里<strong>方向一致</strong>，
  但幅度在 0~67 个百分点之间摆。所以本页给的是<strong>方向</strong>，不是<strong>幅度</strong> —— 见 #46</li>
</ul>

<h2>五、封堵清单（{len(rows)} 条）</h2>
<p class="dim">项目规则：<strong>一条错误只有在回归用例能变红之后，才算封堵。</strong>
其中 <strong>{n_sealed}</strong> 条的「能变红」由<strong>变异测试</strong>证明 ——
每个变异体会把缺陷重新注回一份代码副本，用例必须变红，否则报 <code>NOT SEALED</code>。
剩下的 <strong>{n_human}</strong> 条（{e(human_ids)}）是<strong>流程缺陷</strong>，
它们<strong>无法</strong>用变异测试证明，状态列里如实写着「靠人」。</p>
<table><tr><th>编号</th><th>缺陷</th><th>状态</th></tr>{seals}</table>

</main></body></html>
"""


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    html_text = build()
    # ⚠️ 必须显式 `newline="\n"`。
    #    `.gitattributes` 写着 `* text=auto eol=lf`，而 Python 的文本模式在 Windows 上
    #    默认把 "\n" 翻译成 CRLF —— 于是**每跑一次生成器，克隆里就多一次"假修改"**
    #    （`git status` 报 M，`git diff` 却是空的），陌生人会以为仓库脏了。
    #    这正是 `.gitattributes` 第 1 行想避免的那种噪音。（#39）
    OUT.write_text(html_text, encoding="utf-8", newline="\n")
    kb = len(html_text.encode("utf-8")) / 1024
    print(f"已生成：{OUT.relative_to(ROOT)}（{kb:.0f} KB，自包含、无需服务器）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
