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
    "被排除",
    "排除",
    "驳回",
    "被证伪",
    "无法解释",
    "不成立",
    "只是",
    "仅是",
    "次要",
    "放大因素",
)

_CLAUSE_SPLIT = re.compile(r"[。！？；;，,\n]")


@dataclass(frozen=True)
class ScenarioScore:
    """一个场景的评分规则。"""

    fault_id: str
    label: str
    # 每一组至少命中一个词才算通过
    keyword_groups: tuple[tuple[str, ...], ...]
    # 常见的错误结论（用于分析"错在哪里"，不参与判定）
    common_wrong: tuple[str, ...] = field(default=())

    def judge(self, text: str, *, dismissal_aware: bool = True) -> tuple[bool, list[bool]]:
        """返回 (是否答对, 每组的命中情况)。

        ⚠️ 大小写不敏感 —— 模型可能写 "Order" 也可能写 "order"。

        `dismissal_aware=False` 可以退回旧的纯关键词行为 ——
        保留它是为了**能量化这次修正改变了多少结论**（见 eval/rescore.py），
        而不是让历史数字悄悄变化。
        """
        if not dismissal_aware:
            low = text.lower()
            hits = [any(kw.lower() in low for kw in group) for group in self.keyword_groups]
            return all(hits), hits

        clauses = [c for c in _CLAUSE_SPLIT.split(text) if c.strip()]

        hits = []
        for group in self.keyword_groups:
            found = False
            for clause in clauses:
                low_clause = clause.lower()
                if not any(kw.lower() in low_clause for kw in group):
                    continue
                # 关键词在场的这一小句，是在否掉这个原因吗？
                if any(m in clause for m in DISMISSAL_MARKERS):
                    continue
                found = True
                break
            hits.append(found)
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
            if not hit and any(kw.lower() in text.lower() for kw in group)
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
        ),
        ScenarioScore(
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
    ]
}


def describe_all() -> str:
    lines = ["故障  场景                            需要命中的概念组"]
    lines.append("-" * 92)
    for s in SCENARIOS.values():
        groups = "  +  ".join("/".join(g[:3]) for g in s.keyword_groups)
        lines.append(f"{s.fault_id:<4}  {s.label:<30}  {groups}")
    return "\n".join(lines)
