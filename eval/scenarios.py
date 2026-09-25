"""
场景评分：把 Agent 的自由文本结论，判定为"对/错"。

============================ 为什么用关键词组而不是 LLM 裁判 ============================

两种做法：

  LLM 当裁判   —— 更懂语义，但**不可复现、有偏好（偏爱冗长答案）、要花钱**
  关键词组     —— 死板，但**完全确定、零成本、可审计**

D5 需要的是"能反复跑出同一个数字"，所以先用关键词组。
LLM 裁判放到 D10 的评测 harness 里作为补充（需求 FR-4 也要求评测可复现）。

============================ 关键词组的写法 ============================

每个场景给出若干**组**，每组是一组同义词。判定规则：

    **每一组至少命中一个词**，才算答对。

例如 F2（池耗尽，根因在 payment 的外部风控）：

    组 1：风控 / 外部依赖 / 第三方 / risk      ← 必须点到"是外部依赖"
    组 2：延迟 / 变慢 / 响应慢 / 慢            ← 必须点到"是变慢"

而"order 连接池太小"这个答案会**因为组 1 没命中而判错** ——
这正是我们要的：它是**最像根因但其实不是根因**的那个答案。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ================================================================
# 关键修正：关键词必须出现在**主张**它的地方，不能出现在**否掉**它的地方
# ================================================================
#
# 真实事故（D7，F2 第一次跑通多 Agent）：
#
#   协调者的裁决文本是：
#     「…把 order 入口连接池容量砍到 2… 下游 payment 风控变慢只是并发的放大因素，
#       不是触发者。」
#
#   它**采纳了红鲱鱼（配置变更）**，把真根因（外部风控变慢）当作被驳回的干扰项 ——
#   但这句话里同时含"风控"和"变慢"，于是旧的纯关键词判定给出 **100%，判对**。
#
#   **多 Agent 实际上掉进了红鲱鱼陷阱，而分数说它答对了。**
#
# 这类错误和 harness-log #11 #12 #13 是同一族：
#   **数字算出来了，但它测的不是你以为的那个东西。**
#
# 修正思路（保持确定性、零成本、可审计，不引入 LLM 裁判）：
#   1. 把文本切成**小句**（按句号和逗号都切）
#   2. 一个小句只有在包含关键词、**且不含否定/让步标记**时，才算"主张"
#   3. 某一组只要有**任意一个**小句是"主张"，就算该组命中
#
# 为什么按逗号也切：中文里"否掉"和"主张"经常在同一句里靠逗号分开，例如
#   「根因不是 order 的连接池，而是外部风控变慢」→ 切完第一段被标记否掉、
#   第二段正常主张 ⇒ 判对。（若只按句号切，这一句会因含"不是"而整句被误判。）
#
# 标记表刻意**只收明确的否定/让步词**。宁可漏判（把该否掉的当主张），
# 也不要误伤（把正常主张判成否定）—— 后者会让本来就对的答案变错。

DISMISSAL_MARKERS: tuple[str, ...] = (
    "不是根因",
    "不是原因",
    "不是触发",
    "不是问题",
    "非根因",
    "并非",
    "而非",          # ← #21 补：真实措辞"…是被放大的脆弱点而非触发者"
    "被排除",
    "排除",
    "驳回",
    "被证伪",
    "无法解释",
    "不能解释",      # ← #21 补
    "不成立",
    "站不住",        # ← #21 补
    "只是",
    "仅是",
    "次要",
    "放大因素",
    "被放大",        # ← #21 补
    "脆弱点",        # ← #21 补：本次实际出现的措辞
    "伴随",          # ← #21 补
    "无影响",
    "无功能",        # ← #21 补
    "不足以",        # ← #21 补
    "无关",
    # ⚠️ 这张表是**已知有弱点**的机制（harness-log #21）：
    #    它是"逐条把见过的措辞加进去"，天然追不上措辞的多样性 ——
    #    第一次（#16）漏掉了"只是/并非"，第二次（#21）漏掉了"而非/脆弱点"。
    #    真正的解法是 D10 的 LLM 裁判；在那之前，
    #    **每一条新场景都必须先人工读一遍真实答案**，再决定判定是否可信。
)

_CLAUSE_SPLIT = re.compile(r"[。！？；;，,\n]")

# 英文/数字的"词"：刻意**按 `_` 也切开** —— 这样 `w_inventory_downstream_retries`
# 会被切成 `w / inventory / downstream / retries`，便于逐词比较。
_ASCII_TOKEN = re.compile(r"[a-z0-9]+")


def _stem_en(word: str) -> str:
    """英文词形归一（**极简**，只覆盖本项目真遇到的那几种变化）。

    ⚠️ 为什么需要它（#48 的**第二版**修正）：第一版我用"词首前缀"去解决
    `retry` 匹配不上 `retries` —— **那个想法是错的**：`retries` 并不以 `retry` 开头
    （它是 `retri` + `es`，即 y→ie 变形）。实测确认 `_kw_hit(clause, "retry")` 仍是 False。
    真正的机制是**词形变化**，不是前缀。

    规则（对关键词与文本**用同一套**归一 ⇒ "一致性"比"语言学正确"更重要）：
        retries → retry（y→ie 复数）　leaks → leak（加 s）　leaked → leak（-ed）
    归一后按"相等或前缀"比较，从而覆盖 retrying / leaking 这类。
    """
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s"):
        return w[:-1]
    return w


def _kw_hit(haystack_lower: str, kw: str) -> bool:
    """关键词是否命中 —— 对英文做**词形归一**（外加词首前缀），对中文用「子串」。

    ⚠️ 为什么不是简单的子串（这是 harness-log #48 实测出来的）：

        F4 的判据里有 `retry`，而一个**点名了参数名**的答案写的是
        `W_INVENTORY_DOWNSTREAM_RETRIES`。`retries` **既不包含** `retry`
        这个子串、**也不以它开头**（`retri` + `es`），于是那个**语义完全正确**的答案
        被判成"从没提过重试" —— 实测两例，其中 `flash baseline @40` 第 2 轮
        写的就是标准答案本身。

        同一类风险不止一处：F6 的 `leak` 遇上 `leaks/leaked`、`重试` 遇上 `重试次数`。

    ⇒ 规则（对**所有场景统一**，不是给 F4 开小灶）：
        · 纯 ASCII：词形归一后相等、或"某个词以它开头"、或仍是子串（覆盖 `risk_latency_ms`）；
        · 非 ASCII（中文）：仍是子串（中文没有词边界，`重试` 命中 `重试次数`）。
    """
    k = kw.lower()
    if k in haystack_lower:
        return True
    if not k.isascii():
        return False
    ks = _stem_en(k)
    return any(
        tok.startswith(k) or _stem_en(tok) == ks
        for tok in _ASCII_TOKEN.findall(haystack_lower)
    )


def _group_asserted(
    text: str, group: tuple[str, ...], *, dismissal_aware: bool = True
) -> bool:
    """这一组关键词有没有**被主张**（而不是被否掉）。

    ⚠️ 这是全仓库唯一一份"小句 + 否定语境"判定的实现。
       `ScenarioScore.judge` 与 `asserted_causes` 都调它 ——
       刻意不复制第二份：复制出来的第二份迟早会和第一份走散，
       而那种"两份判定不一致"的 bug 极难发现。
    """
    if not dismissal_aware:
        low = text.lower()
        return any(_kw_hit(low, kw) for kw in group)

    for clause in _CLAUSE_SPLIT.split(text):
        if not clause.strip():
            continue
        low_clause = clause.lower()
        if not any(_kw_hit(low_clause, kw) for kw in group):
            continue
        if any(m in clause for m in DISMISSAL_MARKERS):
            continue
        return True
    return False


def _asserted_flags(
    text: str, groups: tuple[tuple[str, ...], ...], *, dismissal_aware: bool = True
) -> list[bool]:
    return [
        _group_asserted(text, g, dismissal_aware=dismissal_aware) for g in groups
    ]


@dataclass(frozen=True)
class Cause:
    """一个"必须被找到的原因"。

    多故障场景（F8）用它把判据**拆成一项一项**，
    这样才能分别回答两个不同的问题（见 runner 里的两轴报告）：

        **召回**：这次尝试里，有没有**任何环节**找到它？
        **结论**：最终答案有没有**主张**它？

    单故障场景不需要它（只有一个原因，"召回"与"结论"必然相同）。
    """

    name: str
    keyword_groups: tuple[tuple[str, ...], ...]

    def asserted(self, text: str) -> bool:
        """这段话有没有主张这个原因（所有组都要被主张）。"""
        return all(_asserted_flags(text, self.keyword_groups))


def asserted_causes(text: str, causes: tuple[Cause, ...]) -> dict[str, bool]:
    """逐个原因判断"这句话有没有主张它"。"""
    return {c.name: c.asserted(text) for c in causes}


@dataclass(frozen=True)
class ScenarioScore:
    """一个场景的评分规则。"""

    fault_id: str
    label: str
    # 每一组至少命中一个词才算通过
    keyword_groups: tuple[tuple[str, ...], ...]
    # 常见的错误结论（用于分析"错在哪里"，不参与判定）
    common_wrong: tuple[str, ...] = field(default=())
    # ⚠️ 非空表示**这个场景已作废，不得参与聚合**（harness-log #20）
    invalidated_reason: str = ""
    # 多故障场景：**必须同时找到**的原因清单（空 = 单原因场景）
    # 见 `Cause` 的注释与 runner 里的"两轴报告"
    required_causes: tuple[Cause, ...] = ()

    def judge(self, text: str, *, dismissal_aware: bool = True) -> tuple[bool, list[bool]]:
        """返回 (是否答对, 每组的命中情况)。

        ⚠️ 大小写不敏感 —— 模型可能写 "Order" 也可能写 "order"。

        `dismissal_aware=False` 可以退回旧的纯关键词行为 ——
        保留它是为了**能量化这次修正改变了多少结论**（见 eval/rescore.py），
        而不是让历史数字悄悄变化。
        """
        if not dismissal_aware:
            low = text.lower()
            hits = [any(_kw_hit(low, kw) for kw in group) for group in self.keyword_groups]
            return all(hits), hits

        hits = _asserted_flags(text, self.keyword_groups)
        return all(hits), hits

    def explain(self, text: str) -> str:
        ok, hits = self.judge(text)
        if ok:
            return "✅ 命中全部关键词组（且出现在主张位置，不是被否掉的位置）"
        missed = [
            "/".join(group) for group, hit in zip(self.keyword_groups, hits) if not hit
        ]
        detail = "；".join(missed)

        # 一个特别值得单独指出的情况：词**出现了**，但**出现在被否掉的小句里**。
        # 这正是"看起来答对了、其实答反了"的那种答案（见文件开头的真实事故）。
        dismissed = [
            "/".join(group)
            for group, hit in zip(self.keyword_groups, hits)
            if not hit and any(_kw_hit(text.lower(), kw) for kw in group)
        ]
        if dismissed:
            return (
                f"❌ 关键词出现了，但**出现在被否掉/降级的位置**：{'；'.join(dismissed)}"
                "（即：它把这几个概念当作干扰项排除掉了）"
            )
        return "❌ 缺少关键概念：" + detail


SCENARIOS: dict[str, ScenarioScore] = {
    s.fault_id: s
    for s in [
        ScenarioScore(
            fault_id="F1",
            label="外部依赖变慢",
            keyword_groups=(
                ("风控", "外部依赖", "第三方", "risk"),
                ("延迟", "变慢", "响应慢", "耗时升", "慢"),
            ),
            common_wrong=("order 自身问题", "数据库慢"),
        ),        ScenarioScore(
            fault_id="F2",
            label="连接池耗尽（被下游变慢放大）",
            # ★ 这是全项目最关键的一条判据。
            #   "order 连接池太小" 看起来最像根因，但它解释不了
            #   "为什么 payment 的风控耗时同时飙升" —— 所以判错。
            keyword_groups=(
                ("风控", "外部依赖", "第三方", "risk"),
                ("延迟", "变慢", "响应慢", "耗时升", "慢"),
            ),
            common_wrong=("把 order 的连接池调大", "连接池配置过小"),
            # ⚠️ 本场景**已作废**（harness-log #20）。
            #
            # 对照实验证明它出反了：那条被当作"红鲱鱼"的变更（池 64→2）
            # **就是真根因**（只把池改小、下游完全健康 → 851 个 5xx；
            # 池保持 64、风控同样 800ms → 0 个 5xx）。
            # 而声明的真根因（风控变慢）只是把失败率从 56% 抬到 97% 的放大器。
            #
            # 后果：**判分会把答对的扣分、把答错的加分。**
            #
            # 它保留在目录里作为**反例留档**（"我们出错过一道题"比正确答案更有价值），
            # 但**不得参与任何准确率聚合** —— 否则坏题目会持续污染所有数字。
            # 红鲱鱼职责已由 **F7** 承担。
            invalidated_reason=(
                "harness-log #20：对照实验证明本场景声明的答案是反的"
                "（那条变更 64→2 就是真根因），判分会把正确答案判错。"
                "已由 F7 承担红鲱鱼职责；本场景仅作反例留档，不参与聚合。"
            ),
        ),
        ScenarioScore(
            fault_id="F3",
            label="本环节处理变慢",
            keyword_groups=(
                ("inventory", "库存"),
                ("慢", "延迟", "变慢", "耗时"),
            ),
            common_wrong=("payment 慢", "网络问题"),
        ),
        ScenarioScore(
            fault_id="F4",
            label="重试风暴（配置漂移）",
            # 症状在 payment（QPS 暴涨），根因在 inventory 的配置。
            # 答"payment 被打爆需要扩容"会因为没有"重试/inventory"而判错。
            keyword_groups=(
                ("重试", "retry"),
                ("inventory", "库存"),
            ),
            common_wrong=("payment 需要扩容", "请求量自然增长"),
        ),
        ScenarioScore(
            fault_id="F5",
            label="外部依赖报错",
            keyword_groups=(
                ("风控", "外部依赖", "第三方", "risk"),
                ("报错", "失败", "错误率", "不可用", "拒绝"),
            ),
            common_wrong=("代码 bug", "数据库故障"),
        ),
        ScenarioScore(
            fault_id="F6",
            label="内存泄漏（对照组：症状与根因同源）",
            keyword_groups=(
                ("内存", "memory", "泄漏", "leak"),
                ("order",),
            ),
            common_wrong=("依赖问题",),
        ),
        ScenarioScore(
            fault_id="F7",
            label="无关变更干扰（红鲱鱼 / 真根因未记录）",
            # ★★ 这是 F2 的重做版（harness-log #20）。
            #
            # 判据和 F1 **完全一样**（因为真根因就是同一件事：外部风控变慢）。
            # 差别只在于 F7 的变更日志里**多了一条无关变更**
            # （order 的池获取超时 2000→1200，从未被触及）。
            #
            # 用同一组关键词是有意的：
            #   (F1, F7) 构成一对严格对照 —— 物理条件相同、只有"有没有无关变更"不同。
            #   于是"抗不抗得住无关变更"被单独隔离出来，而不会再混进别的东西。
            #
            # 那条变更**已被对照实验证明是无关的**：
            #   · 单独施加它 → 什么都不发生
            #   · 单独施加风控变慢 → 事故出现（= F1）
            # 详见 docs/04-故障目录.md 的 F7 小节。
            keyword_groups=(
                ("风控", "外部依赖", "第三方", "risk"),
                ("延迟", "变慢", "响应慢", "耗时升", "慢"),
            ),
            common_wrong=(
                "池获取超时被调小导致失败",
                "把连接池等待上限改回 2000ms",
                "order 连接池配置变更导致请求失败",
            ),
        ),
        ScenarioScore(
            fault_id="F8",
            label="多故障叠加（风控变慢 + 内存泄漏）",
            # ★★ 本项目唯一一个"必须同时找到两个原因"的场景。
            #
            # 价值在于它测的是**停止条件**，而不是找证据的能力：
            # 一个 Agent 找到"风控慢 800ms"之后，它的答案已经自洽了 ——
            # 此时它会不会继续查下去？单 Agent 只有一个上下文、一条推理链，
            # 一旦形成结论就容易收工；三个专职 Agent 各自只盯一路信号，
            # 理论上更可能各抓一个。
            #
            # ⚠️ 三组必须**全部**命中，缺一组即判不完整 —— 这是有意的。
            #    但这也意味着这条判据**比别的场景更严**，
            #    所以它成立的前提被对照实验验证过：
            #      臂A 只泄漏        → 150ms，leak = 764 MiB
            #      臂B 只风控慢      → 905ms，风控实测 801ms，无泄漏
            #      臂C 叠加（=F8）   → 919ms，风控实测 801ms，leak = 764 MiB
            #    两个故障的证据在同一窗口内完好共存、互不掩盖（吻合到 0.1%），
            #    所以"两个都必须答出来"不是刁难。
            keyword_groups=(
                ("风控", "外部依赖", "第三方", "risk"),
                ("慢", "延迟", "变慢", "响应慢", "耗时升"),
                ("内存", "memory", "泄漏", "leak"),
            ),
            common_wrong=(
                "只答出外部风控变慢（漏了内存泄漏）",
                "只答出内存泄漏（漏了外部风控变慢）",
                "把两者当成同一个原因",
            ),
            # ★ 两个原因**各自**成一项，好让"召回"与"结论"分开算。
            #
            # 为什么必须分开：F8 上真实发生的是
            #   「metrics 专员**找到了**泄漏，但被交叉质证说服改口、被裁决降级」——
            # 这是第三种结局："找到了，但没当成根因"。
            # 单一的关键词组判定**表达不了它**：要么算对（看到词就通过），
            # 要么算错（被否掉就不通过），而这两种都不对。
            required_causes=(
                Cause(
                    name="外部风控变慢",
                    keyword_groups=(
                        ("风控", "外部依赖", "第三方", "risk"),
                        ("慢", "延迟", "变慢", "响应慢", "耗时升"),
                    ),
                ),
                Cause(
                    name="内存泄漏",
                    keyword_groups=(("内存", "memory", "泄漏", "leak"),),
                ),
            ),
        ),
    ]
}


def describe_all() -> str:
    lines = ["故障  场景                            需要命中的概念组"]
    lines.append("-" * 92)
    for s in SCENARIOS.values():
        groups = "  +  ".join("/".join(g[:3]) for g in s.keyword_groups)
        lines.append(f"{s.fault_id:<4}  {s.label:<30}  {groups}")
    return "\n".join(lines)

def keyword_verdict(text: str, cause: Cause) -> str:
    """关键词侧的**三分类**判定，用来和 LLM 裁判对拍。

    ⚠️ 原来的 keyword 判定只有布尔值（主张 / 没主张），
       而"没主张"里混着两件完全不同的事：
           dismissed —— 提到了，但明确否掉了（#16/#21 两次假阳性都出在这儿）
           absent    —— 压根没提
       要和裁判**同口径**比较，就必须把这两者分开。
       分开的依据很直接：关键词出现过没有。
    """
    if cause.asserted(text):
        return "asserted"

    # ⚠️ "提到过"必须是**每一组都出现过**，不能是"任意一组出现过"。
    #
    #    这个 bug 是审计脚本当场抓出来的：F4 的判据是
    #        （重试/retry）+（inventory/库存）
    #    而一段**从没提过重试配置**的答案里，因为传播链写了
    #    "payment→inventory→order"，就含了 `inventory` ——
    #    于是被误判成"提到了但否掉了"（dismissed），而真相是 **absent**。
    #
    #    一句话：**判据是合取，就不能用析取去判断"它提没提过"。**
    low = text.lower()
    present = [
        any(_kw_hit(low, kw) for kw in group) for group in cause.keyword_groups
    ]
    return "dismissed" if all(present) else "absent"

# ================================================================
# 每个场景的"原因标签"：给 LLM 裁判用的人话说法
# ================================================================
#
# ⚠️ **只此一份**：`eval/runner.py`（评分路径）与 `eval/judge_audit.py`（审计）
#    都从这里取。各自写一份的话，两边迟早走散 ——
#    而"两份判定不一致"这种 bug 极难发现（本项目已经栽过一次）。
CAUSE_LABELS: dict[str, str] = {
    "F1": "外部风控变慢",
    "F2": "外部风控变慢",
    "F3": "inventory 本环节处理变慢",
    "F4": "inventory 的重试次数配置漂移",
    "F5": "外部风控错误率升高",
    "F6": "order 内存泄漏",
    "F7": "外部风控变慢",
    "F8": "内存泄漏",
}
