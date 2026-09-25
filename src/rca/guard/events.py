"""护栏的标准事件流 —— **这就是"平台"的接口形状**（D21 / M1）。

任何 Agent（自家的、第三方的）只要能产出这串事件，就能被护栏看着。
判定核心**只认这串事件**，不认任何框架类型 —— 所以换框架只是再写一个翻译层。

============================ 三个设计约束（都来自 ADR-0007）============================

1. **判据不得读取标准答案**：本模块与 `rules.py` 里出现的判据，
   一律只用"证据在不在"这类可自证的性质。谁都不许 import `eval.scenarios`。
2. **判断面必须单独回传**：MCP 代理只能看到工具调用与返回（行动面），
   看不到"它最后那句结论"。所以结论是一条**显式的 `Claim` 事件** ——
   外部 Agent 必须调一次 `submit_conclusion`，或者由适配器读它的 stdout。
3. **缺证据 ≠ 没问题**：如果对方只给了工具事件、没给结论事件，
   护栏要报"无法判定"，**而不是默认放行**（同 harness-log #26 的空绿教训）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 事件

@dataclass(frozen=True)
class ToolCall:
    """它要调什么工具。"""

    step: int                 # 全局递增序号：证据指针靠它定位
    name: str
    args: str = ""
    phase: str = ""           # investigate / cross_exam（自家流程用；外部可为空）
    role: str = ""            # logs / metrics / change


@dataclass(frozen=True)
class ToolResult:
    """工具回了什么 —— **数值证据都在这里**。"""

    step: int
    name: str
    text: str
    ok: bool = True
    phase: str = ""
    role: str = ""


@dataclass(frozen=True)
class Claim:
    """它主张了什么。

    ⚠️ `kind="final"` 才是"结论"；中途断言可以被两条规则忽略
       （M1 只判结论，避免把推理过程里的试错算成缺陷）。
    """

    step: int
    text: str
    kind: str = "final"       # final / intermediate
    phase: str = ""
    role: str = ""


Event = ToolCall | ToolResult | Claim


# ---------------------------------------------------------------- Trace

@dataclass
class Trace:
    """一次运行的完整事件流 + 它是谁产生的（用于报告，不参与判定）。"""

    events: list[Event] = field(default_factory=list)
    label: str = ""
    stop_reason: str = ""

    def add(self, event: Event) -> None:
        self.events.append(event)

    # ---- 取用
    @property
    def calls(self) -> list[ToolCall]:
        return [e for e in self.events if isinstance(e, ToolCall)]

    @property
    def results(self) -> list[ToolResult]:
        return [e for e in self.events if isinstance(e, ToolResult)]

    @property
    def claims(self) -> list[Claim]:
        return [e for e in self.events if isinstance(e, Claim)]

    @property
    def final_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.kind == "final"]

    def evidence_text(self) -> str:
        """所有工具返回拼起来 —— 判定"有没有证据"只看这里。"""
        return "\n".join(r.text for r in self.results)

    def evidence_lines(self) -> list[str]:
        out: list[str] = []
        for r in self.results:
            for line in r.text.splitlines():
                if line.strip():
                    out.append(line)
        return out

    def pointer(self, step: int) -> str:
        """把 step 变成人看得懂的证据指针（哪一阶段、哪个角色、第几步）。"""
        for e in self.events:
            if e.step == step:
                who = "/".join(x for x in (getattr(e, "phase", ""), getattr(e, "role", "")) if x)
                name = getattr(e, "name", "")
                head = f"{who} 第{getattr(e, 'step', step)}步" if who else f"第{step}步"
                return f"{head} {name}".strip()
        return f"第{step}步"


# ---------------------------------------------------------------- 抽取工具
#
# 这些正则就是判据的"眼睛"。它们必须**确定性**、可单测、可被变异体打红。

#: 指标名/参数名/字段名这类**标识符**（至少 5 个字符，避开 a/of/the 这类噪声）
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{4,}")

#: 数字（含科学计数法）：4.22e+09 / 8049 / 800.5 / 98
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")

#: 带单位的测量值（这些数字**必须**可溯源，否则就是幻觉数字）
UNIT_NUMBER_RE = re.compile(
    r"(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(ms|毫秒|秒|s\b|GB|MB|KB|%|次|倍)",
    re.IGNORECASE,
)

#: "有参照"的措辞：出现在某个指标附近时，说明它不是孤零零一个绝对值
BASELINE_WORDS = ("基线", "净增", "增量", "对比", "差异", "Δ", "→", "由 ", "before", "after")


def numbers_in(text: str) -> list[float]:
    """抽数字（字符串 → float）。解析不了的（如 1e999）丢掉，不抛异常。"""
    out: list[float] = []
    for m in NUMBER_RE.finditer(text):
        try:
            out.append(float(m.group(0)))
        except ValueError:      # pragma: no cover - 正则已保证形态，这里是兜底
            continue
    return out


def identifiers_in(text: str) -> list[str]:
    """抽标识符（指标名/参数名样式），保持出现顺序、去重。"""
    seen: dict[str, None] = {}
    for m in IDENTIFIER_RE.finditer(text):
        seen.setdefault(m.group(0), None)
    return list(seen)
