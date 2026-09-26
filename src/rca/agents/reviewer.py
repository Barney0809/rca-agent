"""软提醒：**证据审查者**（D21 / M2）。

============================ 它是什么、不是什么 ============================

**是**：一个独立的 LLM 角色，拿协调者的结论去对照三个专员的证据材料，
指出"哪条主张材料支撑不住"，然后给协调者**一次**自我修正的机会。

**不是**（ADR-0007 决定 2）：
  · **不是拦截**：它只能提醒，拦不拦是硬层的事；
  · **不是裁判**：它的输出**不进 `correct` 判定** ——
    否则"护栏让分数变好"就变成自己夸自己（#16/#21 的教训）；
  · **不是记忆**：它只看这一轮的材料，不跨轮次。

============================ 为什么必须是 fail-safe ============================

这是**新增的一条 LLM 调用路径**。新路径的典型事故是"它坏了，整轮白跑"：
三个专员 + 全部质证 + 裁决的钱都已经花了（harness-log #15 就是这么亏的）。
所以 `review_evidence()` **任何异常都吞掉并如实记录**（`error` 字段），
调用方拿到的是"这次没审成"，而不是一次崩溃。

============================ 为什么它可能有用（也可能没用）============================

M1 的硬规则在 21 次真实轨迹上 **0 次触发** —— 它们只抓形式缺陷。
想让结论**更准**，只能靠这一层：让 Agent 在交卷前被问一句"你这条证据在哪"。
但这是**要花钱的**（每次多一次调用），而且**可能测出负结论** ——
M2 的任务就是用开关护栏的 2×2 对照把这件事**量出来**，而不是假设它有用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .baseline import extract_json

REVIEWER_PROMPT = """\
你是**证据审查者**。你不负责重写结论，也**不知道**正确答案是什么。

你只做一件事：拿协调者给出的结论，逐条对照三位专员提供的证据材料，指出

  1. **哪一条主张在材料里找不到支撑**；
  2. 或者 **材料里存在自相矛盾**（两个专员的观测互相打架），而结论没有解释。

规则：
- 只依据材料判断，**不要引入材料之外的事实**；
- "材料里没有"不等于"事实上不成立" —— 措辞要说清是哪一种；
- **不要**提格式、语气、篇幅这类意见；
- 最多 3 条，按严重程度排序；一条都没有就返回空数组。

输出 JSON（不要其他文字）：
{"issues": [{"claim": "被质疑的主张（原话或摘要）",
             "problem": "为什么材料支撑不住它，或它与材料的哪一条矛盾",
             "evidence_needed": "还需要什么证据才能成立"}]}
"""

REVISION_SUFFIX = """\

【证据审查者的意见】下面这些只是提醒，**不一定对**：
{issues}

请重新输出同一份 JSON 结论：
- **只修正上面这些点**，其余判断与措辞保持不变；
- 如果你认为审查者的某条意见站不住，就在相应位置写明理由，并**保持原判断**；
- 不要因为被质疑就把结论改得更含糊 —— 有证据支撑的部分要照旧说死。
"""


@dataclass
class Review:
    """一次审查的结果。`error` 非空表示"没审成"，**不是**"审过了没问题"。"""

    issues: list[dict] = field(default_factory=list)
    raw_text: str = ""
    cost_yuan: float = 0.0
    input_tokens: int = 0            # ★ #71：成本的原料
    output_tokens: int = 0
    parse_ok: bool = False
    error: str = ""

    @property
    def ran(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        return {
            "n_issues": len(self.issues),
            "issues": self.issues,
            "cost_yuan": round(self.cost_yuan, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "parse_ok": self.parse_ok,
            "error": self.error,
            "ran": self.ran,
        }


def format_issues(issues: list[dict]) -> str:
    """把审查意见排成给协调者看的一段话（它读到的是文本，不是 JSON）。"""
    lines: list[str] = []
    for i, issue in enumerate(issues, 1):
        claim = str(issue.get("claim") or "").strip()
        problem = str(issue.get("problem") or "").strip()
        needed = str(issue.get("evidence_needed") or "").strip()
        lines.append(f"{i}. 主张：{claim}")
        lines.append(f"   问题：{problem}")
        if needed:
            lines.append(f"   需要：{needed}")
    return "\n".join(lines) if lines else "（审查者没有提出问题）"


def build_revision_suffix(issues: list[dict]) -> str:
    return REVISION_SUFFIX.replace("{issues}", format_issues(issues))


def review_evidence(
    client,
    material: str,
    verdict_text: str,
    *,
    model: str | None = None,
    max_issues: int = 3,
) -> Review:
    """审一遍结论。**任何异常都不许往上抛** —— 见模块开头的 fail-safe 说明。"""
    user = (
        "【三位专员的证据材料】\n" + material + "\n\n"
        "【协调者的结论原文】\n" + verdict_text + "\n\n"
        "请按系统提示的格式输出审查结果。"
    )
    try:
        result = client.chat(
            messages=[
                {"role": "system", "content": REVIEWER_PROMPT},
                {"role": "user", "content": user},
            ],
            model=model,
            tools=None,          # 审查者只读材料，不查数据（它没有授权）
            tag="reviewer",
        )
    except Exception as exc:                      # noqa: BLE001 —— fail-safe 是刻意的
        return Review(error=f"{type(exc).__name__}: {exc}")

    review = Review(
        raw_text=result.text,
        cost_yuan=result.cost_yuan,
        input_tokens=int(result.usage.get("prompt_tokens", 0) or 0),      # ★ #71
        output_tokens=int(result.usage.get("completion_tokens", 0) or 0),
    )
    parsed = extract_json(result.text)
    if not isinstance(parsed, dict):
        review.error = "审查者没有返回可解析的 JSON"
        return review
    raw_issues = parsed.get("issues")
    if raw_issues is None:
        raw_issues = []
    if not isinstance(raw_issues, list):
        review.error = "issues 字段不是数组"
        return review
    review.issues = [i for i in raw_issues if isinstance(i, dict)][:max_issues]
    review.parse_ok = True
    return review
