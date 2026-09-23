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

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScenarioScore:
    """一个场景的评分规则。"""

    fault_id: str
    label: str
    # 每一组至少命中一个词才算通过
    keyword_groups: tuple[tuple[str, ...], ...]
    # 常见的错误结论（用于分析"错在哪里"，不参与判定）
    common_wrong: tuple[str, ...] = field(default=())

    def judge(self, text: str) -> tuple[bool, list[bool]]:
        """返回 (是否答对, 每组的命中情况)。

        ⚠️ 大小写不敏感 —— 模型可能写 "Order" 也可能写 "order"。
        """
        low = text.lower()
        hits = [any(kw.lower() in low for kw in group) for group in self.keyword_groups]
        return all(hits), hits

    def explain(self, text: str) -> str:
        ok, hits = self.judge(text)
        if ok:
            return "✅ 命中全部关键词组"
        missed = [
            "/".join(group) for group, hit in zip(self.keyword_groups, hits) if not hit
        ]
        return "❌ 缺少关键概念：" + "；".join(missed)


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
