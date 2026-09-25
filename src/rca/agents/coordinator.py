"""
Coordinator：交叉举证与裁决。

============================ 它要解决的是什么问题 ============================

D5 的 baseline 在六个场景里五个满分，**唯一稳定失败的是 F2（0/3）**。

F2 的答案长这样（三轮都类似）：

    "order 的连接池大小被从 64 误改为 2（05:44:18 的配置变更），
     导致连接池瞬间耗尽，1483 个请求等待 400ms 后失败。"

它**引用了真实变更、带了准确时间戳、机制自洽** —— 唯一的问题是它错了。
真根因是 payment 的外部风控从 30ms 劣化到 800ms（不在变更记录里，只在指标里）。

**它失败的方式不是"没找到证据"，而是停在了一个自洽的错误结论上。**

============================ 三个环节 ============================

    第一轮  三个专职 Agent 各自调查           → 3 份假设（含 needs_from_others）
    第二轮  交叉质证：互相尝试证伪            → 3 份修正后的假设
    第三轮  Coordinator 裁决                  → 根因 + 证据链 + 被驳回项 + 分歧

============================ 两个关键设计决定 ============================

**决定 1：第二轮只给"结论"，不给"原始数据"。**

    如果给原始数据，隔离就没了 —— 每个 Agent 都能看到全部，
    那三轮下来等于回到单 Agent，多 Agent 只是更贵。

    只给结论，隔离仍在：**每个 Agent 只能用自己的数据说话**。
    这逼出真正的对抗：你能用你的数据证伪谁？不能就直说。

**决定 2：第二轮允许再次调用工具。**

    否则它会"凭记忆吵架"。允许回查是刻意的 ——
    F2 里 LogsAgent 自己的日志里**是有**"外部风控响应缓慢 耗时=800ms"这条的，
    它第一次没能把它和池耗尽联系起来。给它一次回查的机会，
    就有可能自己发现。

============================ 裁决不是"三个取两个" ============================

Coordinator 的职责**不是投票**（需求 E4 明确说忌"民主投票"），
而是：**接受一条，并说清楚其他几条为什么解释不了**。

所以裁决结果的必填项里有一项是 `rejected`：
    每条被驳回的假设，都要写明"**它解释不了什么**"。
    这一项比"选哪个"更重要 —— 它是可被质证的。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..llm.provider import DeepSeekClient, LlmResult
from ..tools import RestrictedToolBox, RunContext
from .baseline import extract_json
from .roles import ALL_ROLES, Role
from .specialist import Hypothesis, SpecialistAgent

# ================================================================
# 第二轮：交叉质证
# ================================================================

CROSS_EXAM_PROMPT = """\
下面是你的三位同事（含你自己）在**各自只能看到一路数据**的前提下得出的结论。

⚠️ 你**看不到他们的原始数据** —— 你只能用自己的数据说话。

{colleagues}

现在请你：

1. **尝试证伪同事的结论**：用**你自己的证据**，指出哪一条同事结论站不住脚？
   如果你手上的数据支持某条同事结论，也要明说。
2. **接受质证**：同事的结论有没有**动摇**你的结论？如果有，修正它。
3. **给出修正后的结论**。如果经过质证你认为别人的结论更站得住，就改口 ——
   **改口不是失败，坚持一个被证伪的结论才是。**

最后一次回复必须是纯 JSON：
{
  "revised_claim": "修正后的结论",
  "confidence": 0.0,
  "supports": ["我支持的结论属于谁：logs/metrics/change/self"],
  "falsifies": ["我能证伪谁的结论"],
  "evidence_against": ["我用来证伪的具体证据（必须是我自己数据里真实看到的）"],
  "changed": false,
  "why_changed": "如果改变了结论，说明是什么证据让你改变；否则填空字符串"
}
"""


@dataclass
class CrossExam:
    """一个 Agent 在交叉质证后的回应。"""

    role: str
    original_claim: str
    revised_claim: str = ""
    confidence: float = 0.0
    supports: list[str] = field(default_factory=list)
    falsifies: list[str] = field(default_factory=list)
    evidence_against: list[str] = field(default_factory=list)
    changed: bool = False
    why_changed: str = ""

    parse_ok: bool = False
    steps: int = 0
    tool_calls: int = 0
    repeat_calls: int = 0            # ★ 打转统计（D9）
    cost_yuan: float = 0.0
    finished: bool = False
    raw_text: str = ""
    trace: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "original_claim": self.original_claim,
            "revised_claim": self.revised_claim,
            "confidence": round(self.confidence, 2),
            "supports": self.supports,
            "falsifies": self.falsifies,
            "evidence_against": self.evidence_against,
            "changed": self.changed,
            "why_changed": self.why_changed,
            "parse_ok": self.parse_ok,
            # ⚠️ `finished` 必须存。原本漏了它，于是 results.json 里
            #    交叉质证只有 parse_ok 没有 finished，
            #    报告说"收敛率 0%"时**根本查不出是哪个 Agent 没收敛**（harness-log #18）。
            #    存档的价值就在于事后能定位问题；少一个字段就少一条线索。
            "finished": self.finished,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "repeat_calls": self.repeat_calls,     # ★ 打转统计（D9）
            "cost_yuan": round(self.cost_yuan, 6),
        }


def _format_colleagues(own: Hypothesis, others: list[Hypothesis]) -> str:
    lines = ["【你的结论】", f"  {own.claim or '（空）'}", ""]
    lines.append("【同事的结论】")
    for h in others:
        lines.append(f"  · {h.name}：{h.claim or '（空）'}")
        for e in h.evidence[:3]:
            lines.append(f"      - {e[:150]}")
    return "\n".join(lines)


def build_cross_exam_message(own: Hypothesis, others: list[Hypothesis]) -> str:
    """渲染第二轮的 user message。

    ⚠️ 这个函数存在的唯一理由，是让"渲染"变成一个**可以被测试直接调用**的纯函数。
       `cross_examine` 要发网络请求，测试调不动；
       如果渲染逻辑藏在它内部，那么"有人把 replace 改回 format"这个回归
       **跑测试也抓不到** —— 因为测试根本执行不到那一行。

    真实缺陷（本次）：`CROSS_EXAM_PROMPT` 里含有 JSON 输出模板，
    其中的 `{` `}` 会被 `str.format` 当成占位符，直接抛
    `KeyError: '\n  "revised_claim"'`。改用 replace 后修复。
    """
    return CROSS_EXAM_PROMPT.replace("{colleagues}", _format_colleagues(own, others))


def cross_examine(
    client: DeepSeekClient,
    ctx: RunContext,
    role: Role,
    own: Hypothesis,
    others: list[Hypothesis],
    *,
    model: str | None = None,
    max_steps: int,
) -> CrossExam:
    """让一个专职 Agent 对同事的结论做交叉质证。

    ⚠️ 它仍然**只能调自己那一个工具**（RestrictedToolBox）——
       交叉质证不该打开隔离的门。

    ⚠️ `max_steps` 刻意**不给默认值** —— 调用方必须显式决定。

       原因（harness-log #17）：这里原来写死 `max_steps=4`，而 `--max-steps`
       根本到不了它。实测中指标 Agent 的交叉质证**正好用满 4 步**却没产出结论
       （`parse_ok=False`），于是整体收敛率被记成 0%。
       更糟的是报告据此建议"提高 `--max-steps` 重测" ——
       而那个参数**改不动这个预算**，建议无效。

       这正是 #13（随手定的 `max_steps` 改变了"准确率"）在另一个地方复发：
       **一个没人量过的预算数字，会悄悄变成结论的一部分。**
       所以这里不留默认值：要么显式传，要么报错。
    """
    # ⚠️ 延迟导入：contract 反向依赖 baseline.extract_json
    from .contract import repair_messages

    local_ctx = ctx.fork()
    box = RestrictedToolBox(local_ctx, frozenset({role.tool}), role=role.key)
    nudged = False      # 「JSON 催促」只做一次（见 contract.py）
    messages: list[dict] = [
        {"role": "system", "content": role.system_prompt},
        {
            "role": "user",
            # ⚠️ 这里用 replace 而不是 str.format。
            #    因为 prompt 里含有 JSON 输出模板，那些 `{` `}` 会被 format
            #    当成占位符，直接抛 KeyError（踩过一次）。
            #    渲染逻辑抽到 build_cross_exam_message，好让测试能直接覆盖它。
            "content": build_cross_exam_message(own, others),
        },
    ]

    x = CrossExam(role=role.key, original_claim=own.claim)
    cost = 0.0

    for step in range(1, max_steps + 1):
        x.steps = step
        result: LlmResult = client.chat(
            messages=messages,
            model=model,
            tools=box.specs(),
            # ★ 输出上限（D11）：**只当兜底，不用来降本**。
            #
            #   实测教训（很值钱，别删）：
            #     起因是「输出占单次诊断成本的 72.3%」这个测量，于是我先设了 800。
            #     结果 —— **4 次回复被截断在 800/1200，JSON 被切一半 → 解析失败
            #     → 触发 JSON 催促(#27) → 又多跑一轮**：
            #         调用数 33 → **57（+73%）**
            #         输出合计 14,646 → **25,883 tok（+77%）**
            #     即：**总输出反而涨了 77%，成本没降。**
            #
            #   ⇒ 结论：**"给输出加上限"这个降本方向被实测否决。**
            #      上限压到能省钱的水平，就一定会截断结构化输出；
            #      而截断的代价（重跑一轮）比省下的那点输出贵得多。
            #   ⇒ 真正的降本方向是**提示词层面要求结构化/简短**（不截断）。
            #      那需要改 prompt 并重跑验证 —— 尚未做。
            #
            #   2400 是观察到的最大非截断输出（1626）之上的兜底值，
            #   正常回复永远碰不到它；它只防"跑飞了"。
            max_tokens=2400,
            tag=f"crossexam/{role.key}/{local_ctx.run_id}",
        )
        cost += result.cost_yuan

        if result.tool_calls:
            messages.append(result.raw["choices"][0]["message"])
            for tc in result.tool_calls:
                out = box.call(tc.name, tc.arguments)
                x.trace.append({"step": step, "tool": tc.name, "args": tc.arguments,
                                "result": out})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
            continue

        x.raw_text = result.text
        parsed = extract_json(result.text)
        if parsed:
            x.revised_claim = str(parsed.get("revised_claim", "")).strip()
            for key, attr in (("supports", "supports"), ("falsifies", "falsifies"),
                              ("evidence_against", "evidence_against")):
                v = parsed.get(key) or []
                setattr(x, attr, [str(i) for i in v] if isinstance(v, list) else [str(v)])
            try:
                x.confidence = float(parsed.get("confidence", 0.0))
            except (TypeError, ValueError):
                x.confidence = 0.0
            x.changed = bool(parsed.get("changed", False))
            x.why_changed = str(parsed.get("why_changed", "") or "")
            x.parse_ok = True
        else:
            # 抠不出 JSON → **先催一次**（与 baseline / specialist 同一份逻辑，见 contract.py）
            if not nudged:
                nudged = True
                messages = repair_messages(messages, result.text)
                continue

            x.revised_claim = result.text.strip()
        x.finished = True
        break

    x.tool_calls = local_ctx.tool_calls
    x.repeat_calls = local_ctx.repeat_calls      # ★ 打转统计（D9）
    x.cost_yuan = cost
    return x


# ================================================================
# 第三轮：裁决
# ================================================================

COORDINATOR_PROMPT = """\
你是故障根因分析团队的**协调者（Coordinator）**。

三名专员各自只能看到一路数据（日志 / 指标 / 变更），他们已经做过一轮交叉质证。
现在由你裁决。

**你不投票，你做判断。** 你的任务不是"少数服从多数"，而是：

1. 判断哪一条结论**最可能是根因** —— 依据是"它能解释多少其他现象"，
   而不是"有多少人支持它"。
2. 对**每一条被驳回的结论**，写明「**它解释不了什么**」。
   这一项比"选哪个"更重要 —— 它是可被质证的。
3. 如果存在真实分歧（有人坚持另一条结论且给出了证据），
   在 `dissent` 里如实记录，**不要抹平**。

特别注意一类情况：

    一个结论可能**看起来最显眼、证据最"硬"**（例如一条带时间戳的配置变更），
    但它未必是根因。要问：**如果它是根因，那么其他同事观察到的现象能不能被解释？**
    如果解释不了，那它就只是**被放大的脆弱点**，而不是触发者。

★ 还有一类情况同样重要（2026-09-25 新增）：

    **同一段时间里可能同时存在多件互不相干的事。**
    如果你把某个现象降级成"伴随现象 / 被放大的脆弱点"，
    请先自问：**它是不是一个独立的、需要单独处置的问题？**
    是的话，它必须作为**单独一条**列进 `root_causes`，
    而不是被"驳回"掉 —— **驳回的作用是否掉错误的因果解释，不是否掉另一个真实存在的问题。**

    判据：**去掉另一个原因，它会不会自己消失？**
      会消失  → 它确实是伴随现象，可以只在 `rejected` 里说明
      不会消失 → **它是一条独立原因，必须单独列出**

最后一次回复必须是纯 JSON：
{
  "root_causes": ["一条一个根本原因", "存在第二个独立原因就写第二条"],
  "evidence_chain": ["支撑上述结论的证据，注明来自哪一路"],
  "confidence": 0.0,
  "accepted": "logs / metrics / change",
  "rejected": [
    {"role": "logs", "claim": "……", "why_not": "它解释不了什么"}
  ],
  "dissent": ["如果有人在质证后仍坚持另一条结论，在这里如实记录"]
}

⚠️ `root_causes` 是**列表**。只有一个原因时写一条即可；
  **有多条独立原因时必须全部列出，只列一个即为不完整。**
"""


@dataclass
class Verdict:
    """最终裁决。"""

    root_cause: str = ""
    # ★ 2026-09-25 起任务改成"列出**所有**异常及其根因"，所以结论是列表。
    #   `root_cause` 保留为"列表拼起来的文本"（评分与存档一直用它，
    #   改名会让历史数据全部对不上）—— 与 baseline.Diagnosis 同一处理。
    root_causes: list[str] = field(default_factory=list)
    evidence_chain: list[str] = field(default_factory=list)
    confidence: float = 0.0
    accepted: str = ""
    rejected: list[dict] = field(default_factory=list)
    dissent: list[str] = field(default_factory=list)

    parse_ok: bool = False
    cost_yuan: float = 0.0
    raw_text: str = ""

    def to_dict(self) -> dict:
        return {
            "root_cause": self.root_cause,
            "root_causes": self.root_causes,
            "evidence_chain": self.evidence_chain,
            "confidence": round(self.confidence, 2),
            "accepted": self.accepted,
            "rejected": self.rejected,
            "dissent": self.dissent,
            "parse_ok": self.parse_ok,
            "cost_yuan": round(self.cost_yuan, 6),
        }


def _format_for_coordinator(hyps: list[Hypothesis], exams: list[CrossExam]) -> str:
    by_role = {e.role: e for e in exams}
    lines: list[str] = []
    for h in hyps:
        x = by_role.get(h.role)
        lines.append(f"═══ {h.name} ═══")
        lines.append(f"  质证前结论：{h.claim or '（空）'}")
        if h.evidence:
            lines.append("  它的证据：")
            for e in h.evidence[:4]:
                lines.append(f"    - {e[:170]}")
        if x:
            lines.append(f"  质证后结论：{x.revised_claim or '（空）'}")
            if x.changed:
                lines.append(f"  ★ 它改变了结论：{x.why_changed[:170]}")
            if x.falsifies:
                lines.append(f"  它声称能证伪：{'、'.join(x.falsifies)}")
            if x.evidence_against:
                lines.append("  它用来证伪的证据：")
                for e in x.evidence_against[:3]:
                    lines.append(f"    - {e[:170]}")
            if x.supports:
                lines.append(f"  它支持：{'、'.join(x.supports)}")
        lines.append("")
    return "\n".join(lines)


def adjudicate(
    client: DeepSeekClient,
    hyps: list[Hypothesis],
    exams: list[CrossExam],
    *,
    model: str | None = None,
) -> Verdict:
    """协调者裁决。一次 LLM 调用，产出结构化结论。"""
    # ⚠️ 这里**不能**传 max_tokens：`DeepSeekClient.chat()` 的签名里没有这个参数，
    #    输出长度统一由 `config.max_tokens` 决定。
    #    曾经在这里写了 `max_tokens=2048`，结果整个多 Agent 闭环跑完三个专职 Agent
    #    和全部交叉质证（钱已经花了）之后，在**最后一步裁决**才抛：
    #        TypeError: DeepSeekClient.chat() got an unexpected keyword argument 'max_tokens'
    #    封堵见 tests/test_agents.py::test_every_chat_call_matches_the_client_signature
    #    —— 那条用例会扫描全部 chat() 调用点并比对真实签名，这类错误不会再溜过去。
    result: LlmResult = client.chat(
        messages=[
            {"role": "system", "content": COORDINATOR_PROMPT},
            {"role": "user", "content": _format_for_coordinator(hyps, exams)},
        ],
        model=model,
        tools=None,          # 协调者不查数据 —— 它只裁决
        # ★ 输出上限（D11）：同交叉质证 —— **只当兜底**。
        #   裁决要写 root_causes + evidence_chain + rejected[].why_not + dissent，
        #   实测 1293 tok 且**曾被 1200 截断**（截断它的代价尤其大：
        #   被截掉的正是"为什么否掉"那部分，而那才是可被质证的内容）。
        max_tokens=2400,
        tag="coordinator",
    )

    v = Verdict(cost_yuan=result.cost_yuan, raw_text=result.text)
    parsed = extract_json(result.text)
    if parsed:
        # 与 baseline 用**同一个**解析函数：两种格式都认（新的列表 / 旧的单数），
        # 并且容忍"该写数组却写了一句话"。理由见该函数的注释。
        from .baseline import _parse_root_causes

        v.root_causes = _parse_root_causes(parsed)
        v.root_cause = "；".join(v.root_causes)
        ec = parsed.get("evidence_chain") or []
        v.evidence_chain = [str(i) for i in ec] if isinstance(ec, list) else [str(ec)]
        try:
            v.confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            v.confidence = 0.0
        v.accepted = str(parsed.get("accepted", "")).strip()
        rj = parsed.get("rejected") or []
        v.rejected = rj if isinstance(rj, list) else []
        ds = parsed.get("dissent") or []
        v.dissent = [str(i) for i in ds] if isinstance(ds, list) else [str(ds)]
        v.parse_ok = True
    else:
        v.root_cause = result.text.strip()
    return v


# ================================================================
# 全流程
# ================================================================

@dataclass
class MultiAgentResult:
    """一次多 Agent 诊断的完整结果（三个环节的成本与产物都在这里）。"""

    hypotheses: list[Hypothesis] = field(default_factory=list)
    cross_exams: list[CrossExam] = field(default_factory=list)
    verdict: Verdict = field(default_factory=Verdict)

    elapsed_s: float = 0.0
    n_llm_calls: int = 0
    total_tool_calls: int = 0
    denied_tool_calls: int = 0
    repeat_calls: int = 0            # ★ 打转统计（D9）

    @property
    def total_cost_yuan(self) -> float:
        return (
            sum(h.cost_yuan for h in self.hypotheses)
            + sum(c.cost_yuan for c in self.cross_exams)
            + self.verdict.cost_yuan
        )

    def to_dict(self) -> dict:
        return {
            "root_cause": self.verdict.root_cause,
            "confidence": round(self.verdict.confidence, 2),
            "accepted": self.verdict.accepted,
            "n_rejected": len(self.verdict.rejected),
            "dissent": self.verdict.dissent,
            "n_changed_after_crossexam": sum(1 for c in self.cross_exams if c.changed),
            "n_falsify_claims": sum(len(c.falsifies) for c in self.cross_exams),
            "elapsed_s": round(self.elapsed_s, 1),
            "n_llm_calls": self.n_llm_calls,
            "total_tool_calls": self.total_tool_calls,
            "denied_tool_calls": self.denied_tool_calls,
            "cost_yuan": round(self.total_cost_yuan, 6),
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "cross_exams": [c.to_dict() for c in self.cross_exams],
            "verdict": self.verdict.to_dict(),
        }


def diagnose_multi(
    client: DeepSeekClient,
    ctx: RunContext,
    *,
    model: str | None = None,
    max_steps: int = 14,
    cross_exam_steps: int = 14,
    roles: tuple[Role, ...] = ALL_ROLES,
) -> MultiAgentResult:
    """跑完整的多 Agent 流程：三个专员 → 交叉质证 → 协调者裁决。

    并行度说明：
        第一轮与第二轮各自**并发**（三个 Agent 互相独立）。
        并发是多 Agent 相对单 Agent 的天然优势之一：延迟 ≈ 最慢的那个，
        而不是三者之和。

    注意 `max_steps` 默认与 baseline 一致（14）——
        **对照实验里预算必须相同**，否则又是 harness-log #13 那个坑。

    `cross_exam_steps` 当前也取 14，但**这是一个尚未实测校准的临时值**：
        原先写死 4，实测发现指标 Agent 正好用满 4 步仍没产出结论（harness-log #17）。
        先给足预算、保证"没跑完"不会被混进结论里；
        真实需要多少步，由 D12 按实测分布定稿。
    """
    # ⚠️ 这里**不要**再 `from concurrent.futures import ThreadPoolExecutor` ——
    #    本模块顶部已经导入过它了。重复导入会遮蔽外层名字（ruff F811），
    #    而且让人误以为这是函数内的局部依赖。
    started = time.perf_counter()
    out = MultiAgentResult()

    # ---------- 第一轮：各自调查 ----------
    def _investigate(role: Role) -> Hypothesis:
        return SpecialistAgent(
            client, role, model=model, max_steps=max_steps
        ).investigate(ctx)

    with ThreadPoolExecutor(max_workers=len(roles)) as pool:
        out.hypotheses = list(pool.map(_investigate, roles))
    first = out.hypotheses

    # ---------- 第二轮：交叉质证 ----------
    def _exam(role: Role) -> CrossExam:
        own = next(h for h in first if h.role == role.key)
        others = [h for h in first if h.role != role.key]
        return cross_examine(
            client, ctx, role, own, others, model=model, max_steps=cross_exam_steps
        )

    with ThreadPoolExecutor(max_workers=len(roles)) as pool:
        out.cross_exams = list(pool.map(_exam, roles))

    # ---------- 第三轮：裁决 ----------
    out.verdict = adjudicate(client, out.hypotheses, out.cross_exams, model=model)

    out.elapsed_s = time.perf_counter() - started
    out.n_llm_calls = sum(h.steps for h in out.hypotheses) + sum(
        c.steps for c in out.cross_exams
    ) + 1
    out.total_tool_calls = sum(h.tool_calls for h in out.hypotheses) + sum(
        c.tool_calls for c in out.cross_exams
    )
    out.denied_tool_calls = sum(h.denied_tool_calls for h in out.hypotheses)
    out.repeat_calls = sum(h.repeat_calls for h in out.hypotheses) + sum(
        c.repeat_calls for c in out.cross_exams
    )
    return out
