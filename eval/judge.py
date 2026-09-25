"""LLM 裁判：判断一段结论**有没有主张**某个原因。

============================ 为什么需要它 ============================

关键词判定在本项目上翻过两次车，而且是**同一类**：

  #16  「…风控变慢**只是**并发的放大因素，**不是触发者**」→ 被算作"主张了风控"
  #21  「…内存泄漏…是**被放大的脆弱点而非触发者**」→ 同样被算作"主张"

两次都靠**往词表里补词**修掉，但那是**追着措辞跑**：
它是"逐条把见过的说法加进去"，天然追不上语言的多样性。
（#21 的元教训：变异测试只能证明"能抓住作者想象过的错误"。）

============================ 它必须被验证，而不是被相信 ============================

本项目一路上学到的最大一件事：**换一把尺子，结论就可能整个反过来。**
所以"上了 LLM 裁判"本身不是成绩 —— 成绩是**它能复现人工判定**。

`FIXTURES` 里每一条都是**我逐字读过、手工判定过**的真实答案
（来自 `runs/_eval/` 里的存档，一字未改）。验收标准：

    **裁判必须复现全部人工判定**，一条不一致就不算通过。

============================ 三种结局，不是两种 ============================

    asserted   主张了这个原因
    dismissed  提到了，但**明确降级/否掉**了它
    absent     根本没提

为什么要三分：F8 上真实发生的就是"**找到了，但没当成根因**"（那条泄漏）。
二分法只能把它算成"对"或"错"，两种都不对（详见 docs/06 §四）。

============================ 用法 ============================

    # 只跑人工标注的固定样本，看裁判与人工判定的一致率（几厘钱）
    .\\.venv\\Scripts\\python.exe eval\\judge.py --validate

    # 单条试一下
    .\\.venv\\Scripts\\python.exe eval\\judge.py --text "…" --cause "内存泄漏"
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from rca.agents.baseline import extract_json  # noqa: E402
from rca.llm.provider import DeepSeekClient, LlmConfig  # noqa: E402

ASSERTED = "asserted"
DISMISSED = "dismissed"
ABSENT = "absent"

JUDGE_SYSTEM = """\
你是故障诊断报告的**严格评审**。你的唯一任务是判断：这份报告**有没有主张**
某个特定原因 —— 不是"有没有出现过相关的词"。

三个选项，必须选一个：

  asserted   报告**主张**这个原因（把它当成原因/根因/触发因素）
  dismissed  报告**提到了**它，但**明确降级或否掉**了它
             （例如说它"只是伴随现象""是被放大的脆弱点""不是触发者""并非根因"）
  absent     报告根本没提它

⚠️ 最容易判错的是 asserted 与 dismissed 之间：
   **"提到了"和"主张了"是两件事。**
   报告经常为了解释"为什么否掉它"而详细描述它 —— 那仍然是 dismissed。

只输出 JSON，不要任何其他文字：
{"verdict": "asserted|dismissed|absent", "why": "一句话说明依据（引用原文片段）"}
"""


@dataclass
class JudgeResult:
    verdict: str
    why: str
    cost_yuan: float = 0.0
    raw: str = ""


def judge_cause(
    client: DeepSeekClient,
    text: str,
    cause: str,
    *,
    model: str | None = None,
) -> JudgeResult:
    """判断 `text` 有没有主张 `cause` 这个原因。"""
    result = client.chat(
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"【要判断的原因】{cause}\n\n"
                    f"【诊断报告原文】\n{text}\n\n"
                    f"请判断：这份报告对「{cause}」是 asserted / dismissed / absent？"
                ),
            },
        ],
        model=model,
        tools=None,
        tag="judge",
    )

    parsed = extract_json(result.text) or {}
    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in (ASSERTED, DISMISSED, ABSENT):
        # 裁判自己没按契约输出 → 明确标成 unknown，**不许猜**
        # （猜就等于把"裁判失败"伪装成一条判定结论）
        verdict = "unknown"
    return JudgeResult(
        verdict=verdict,
        why=str(parsed.get("why", "")).strip(),
        cost_yuan=result.cost_yuan,
        raw=result.text,
    )


# ================================================================
# 人工标注样本：**逐字来自真实存档**，一字未改
# ================================================================
#
# ⚠️ 这些标注是我**逐条读过原文**之后手工下的判断，
#    不是用关键词规则生成的 —— 否则就成了"用旧尺子校准新尺子"。

FIXTURES: list[dict] = [
    {
        "id": "f8-multi-verdict-dismisses-the-leak",
        "source": "runs/_eval/multi-20260925-072846（D7 第一次跑通多 Agent）",
        "cause": "内存泄漏",
        "expected": DISMISSED,
        "note": "裁决把泄漏降级成「被放大的脆弱点而非触发者」—— 这是 #21 那次假阳性的原文",
        "text": (
            "payment 所依赖的外部风控服务响应变慢（约 800ms/次），导致 payment 扣款耗时被拉长，"
            "order 与 inventory 的耗时只是逐层叠加继承该延迟；"
            "order 的 4.22e+09 字节内存泄漏在本窗口内无功能/资源影响，是被放大的脆弱点而非触发者。"
        ),
    },
    {
        "id": "f8-multi-verdict-asserts-the-risk-slowdown",
        "source": "同上",
        "cause": "外部风控变慢",
        "expected": ASSERTED,
        "note": "同一段文本里，风控变慢**确实**是被主张的 —— 用于确认裁判不是「一律判 dismissed」",
        "text": (
            "payment 所依赖的外部风控服务响应变慢（约 800ms/次），导致 payment 扣款耗时被拉长，"
            "order 与 inventory 的耗时只是逐层叠加继承该延迟；"
            "order 的 4.22e+09 字节内存泄漏在本窗口内无功能/资源影响，是被放大的脆弱点而非触发者。"
        ),
    },
    {
        "id": "f4-baseline-dismisses-the-retry-change",
        "source": "runs/_eval/baseline-20260924-055414 第1轮（旧任务口径）",
        "cause": "inventory 的重试次数配置漂移",
        "expected": DISMISSED,
        "note": "「只是放大了错误量，并非根因」—— #16 那次假阳性的原文",
        "text": (
            "最下游的外部风控依赖不可用（risk control unavailable），导致 payment 扣款失败，"
            "并沿 payment→inventory→order 逐层向上传播为 502 失败；"
            "inventory 的重试次数变更只是放大了错误量，并非根因。"
        ),
    },
    {
        "id": "f7-baseline-never-mentions-the-decoy",
        "source": "runs/_eval/baseline-20260925-073928 第1轮",
        "cause": "order 的池获取超时被调小（那条无关变更）",
        "expected": ABSENT,
        "note": "它压根没提那条变更 —— 这才是 F7 区分不出能力的原因（docs/06 §三）",
        "text": (
            "外部风控依赖响应变慢（每次调用约800ms），该延迟沿 payment→inventory→order "
            "逐层向上传播，导致整条下单链路耗时升高"
            "（order 968ms / inventory 886ms / payment 812ms，其中风控占801ms）。"
        ),
    },
    {
        "id": "f8-baseline-new-task-asserts-both",
        "source": "runs/_eval/baseline-20260925-083xxx 第3轮（新任务：列出所有异常）",
        "cause": "内存泄漏",
        "expected": ASSERTED,
        "note": "任务改成「列出所有异常」之后，它把泄漏**当成独立问题**列了出来",
        "text": (
            "外部风控（payment 的下游依赖）响应缓慢：单次风控调用约 800ms，"
            "占 payment 总耗时 818ms 的绝大部分，并逐层放大为 inventory 899ms、order 1032ms "
            "的整体下单变慢；这是纯延迟劣化，非我方故障，也无配置变更。；"
            "order 服务存在内存泄漏：本时段 leak_bytes_total 增量约 4.22e+09 字节（约 4.2GB），"
            "与风控延迟互不相干，修复风控后该泄漏依然存在。"
        ),
    },
    {
        "id": "f2-multi-verdict-dismisses-the-risk-slowdown",
        "source": "runs/_eval/multi-20260924-063813（F2，后来判定场景答案是反的）",
        "cause": "外部风控变慢",
        "expected": DISMISSED,
        "note": "「只是并发的放大因素，不是触发者」—— 这里判 dismissed 是对的，"
                "而当时的关键词判定给了它满分",
        "text": (
            "05:44:18 的配置变更 order.W_ORDER_POOL_SIZE 64→2 把 order 入口连接池容量砍到 2，"
            "使 order 在正常并发下立即饱和（in_flight=2），97% 的 create_order 请求在 400ms 内"
            "拿不到连接而失败；下游 payment 风控变慢只是并发的放大因素，不是触发者。"
        ),
    },
]


def validate(model: str | None = None, verbose: bool = True) -> int:
    """跑固定样本，报告裁判与人工判定的一致率。"""
    cfg = LlmConfig.from_env()
    client = DeepSeekClient(cfg)

    if verbose:
        print("=" * 96)
        print("  LLM 裁判验证 —— 必须复现**人工判定**（逐字来自真实存档）")
        print("=" * 96)

    agree = 0
    total_cost = 0.0
    mismatches: list[dict] = []

    for fx in FIXTURES:
        res = judge_cause(client, fx["text"], fx["cause"], model=model)
        total_cost += res.cost_yuan
        ok = res.verdict == fx["expected"]
        agree += ok
        if not ok:
            mismatches.append({**fx, "got": res.verdict, "why": res.why})
        if verbose:
            mark = "✅" if ok else "❌"
            print(f"  {mark} {fx['id']}")
            print(f"        人工：{fx['expected']}　裁判：{res.verdict}　¥{res.cost_yuan:.6f}")
            if not ok:
                print(f"        裁判理由：{res.why}")

    n = len(FIXTURES)
    print()
    print(f"  一致率：{agree}/{n}　总成本：¥{total_cost:.6f}")
    if mismatches:
        print()
        print("  ⚠️ 不一致的样本 —— 裁判**没有通过验收**：")
        for m in mismatches:
            print(f"    · {m['id']}：人工 {m['expected']} / 裁判 {m['got']}")
            print(f"      原文：{m['text'][:120]}…")
    else:
        print("  ✅ 裁判复现了全部人工判定 —— 通过验收。")
    return 0 if agree == n else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 裁判（判断'有没有主张某个原因'）")
    ap.add_argument("--validate", action="store_true", help="跑固定样本，验证裁判是否复现人工判定")
    ap.add_argument("--text", help="直接判一段文本")
    ap.add_argument("--cause", help="要判断的原因")
    ap.add_argument("--model", default=None)
    ap.add_argument("--adversarial", action="store_true",
                    help="在刻意避开已知标记词的对抗样本上比较两把尺子")
    args = ap.parse_args()

    if args.adversarial:
        return compare_on_adversarial(args.model)
    if args.validate:
        return validate(args.model)
    if args.text and args.cause:
        client = DeepSeekClient(LlmConfig.from_env())
        res = judge_cause(client, args.text, args.cause, model=args.model)
        print(json.dumps(
            {"verdict": res.verdict, "why": res.why, "cost_yuan": round(res.cost_yuan, 6)},
            ensure_ascii=False, indent=2,
        ))
        return 0
    ap.print_help()
    return 2


# ================================================================
# 对抗样本：**故意不含任何已知否定标记**的降级措辞
# ================================================================
#
# 为什么需要它们：`FIXTURES` 上的 6/6 证明不了裁判比关键词判定更强 ——
# 因为 #16/#21 之后词表已经补上了那两种措辞，**两边都会判对**。
#
# 裁判唯一可能的价值是"抗**没见过**的说法"。这只能这样测：
# 构造几句**降级/否掉**的说法，且刻意**避开 `DISMISSAL_MARKERS` 里的每一个词**。
#
# ⚠️ 这些是**合成**的样本（不是存档原文），所以单独列一张表，
#    不和人工标注的真实样本混在一起 —— 混在一起会让人以为
#    "人工标注了 10 条"，那是不诚实的。

ADVERSARIAL: list[dict] = [
    {
        "id": "adv-1-companion-result",
        "cause": "内存泄漏",
        "expected": DISMISSED,
        "note": "「伴生结果 / 与根因无涉」—— 不含任何已知标记词",
        "text": (
            "外部风控响应变慢约 800ms 是本次延迟上升的原因。"
            "order 的内存泄漏属于这次问题暴露出来的伴生结果，与根因无涉。"
        ),
    },
    {
        "id": "adv-2-no-causal-link",
        "cause": "内存泄漏",
        "expected": DISMISSED,
        "note": "「没有因果关系」—— 同样不含已知标记词",
        "text": (
            "本次全链路变慢来自最下游的风控延迟。"
            "order 侧的内存增长与本次故障之间没有因果关系。"
        ),
    },
    {
        "id": "adv-3-would-still-happen",
        "cause": "内存泄漏",
        "expected": DISMISSED,
        "note": "「即便…也照样会」—— 一种很自然的否掉方式，但词表里没有",
        "text": (
            "根因是外部风控变慢。即便把内存泄漏修好，这次延迟照样会发生。"
        ),
    },
]


def compare_on_adversarial(model: str | None = None) -> int:
    """在**对抗样本**上比关键词判定与裁判 —— 这是唯一能体现裁判价值的比较。"""
    from eval.scenarios import Cause, keyword_verdict

    cfg = LlmConfig.from_env()
    client = DeepSeekClient(cfg)
    cause_obj = Cause(name="内存泄漏", keyword_groups=(("内存", "memory", "泄漏", "leak"),))

    print("=" * 96)
    print("  对抗样本：刻意避开所有已知否定标记的降级措辞")
    print("=" * 96)
    kw_ok = judge_ok = 0
    cost = 0.0
    for fx in ADVERSARIAL:
        kw = keyword_verdict(fx["text"], cause_obj)
        res = judge_cause(client, fx["text"], fx["cause"], model=model)
        cost += res.cost_yuan
        kw_ok += kw == fx["expected"]
        judge_ok += res.verdict == fx["expected"]
        print(f"  {fx['id']}")
        print(f"      期望 {fx['expected']}")
        print(f"      关键词 {kw}  {'✅' if kw == fx['expected'] else '❌ 被绕过'}")
        print(f"      裁判   {res.verdict}  {'✅' if res.verdict == fx['expected'] else '❌'}")
    n = len(ADVERSARIAL)
    print()
    print(f"  关键词判定：{kw_ok}/{n}　LLM 裁判：{judge_ok}/{n}　成本 ¥{cost:.6f}")
    if kw_ok < n and judge_ok == n:
        print("  ⇒ **裁判的价值在这里被测到了**：词表被没见过的措辞绕过，裁判没有。")
    elif judge_ok < n:
        print("  ⇒ 裁判也没全对 —— 那就更不能拿它替换关键词判定。")
    else:
        print("  ⇒ 两边都对：说明这几句其实被词表覆盖了，本组对抗样本无效，需要重设计。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())