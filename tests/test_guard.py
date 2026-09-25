"""护栏（D21 / M1）的回归用例。

封堵的是这四件事：

  1. **形式缺陷必须被抓住**（凭空发明的指标名 / 查无此数的数值）——
     这是"护栏有用"的唯一可自证方向（真实坏答案坏在配权上，形式规则看不见）。
  2. **缺证据 ≠ 没问题**：没有结论事件时必须显式报 `missing_conclusion`，
     而不是返回空列表（#26 的空绿教训）。
  3. **判据不得依赖标准答案**（ADR-0007 决定 1）——
     既要有结构性守卫（不许 import 评分侧），也要有行为守卫（换个错的答案，判定不变）。
  4. **被降级的规则不许悄悄回到线上**：`unbased_metric` 实测精确率 7%，
     已降级为诊断量；这条用例防它被"顺手"加回 `RULES`。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from rca.guard import (  # noqa: E402
    DIAGNOSTIC_RULES,
    RULES,
    Claim,
    ToolCall,
    ToolResult,
    Trace,
    inject_form_defect,
    judge,
    run_diagnostics,
    run_rules,
)

GUARD_DIR = ROOT / "src" / "rca" / "guard"


def _trace(*, conclusion: str = "", with_claim: bool = True) -> Trace:
    """一条很小的合成轨迹：两步工具 + 一条结论。

    刻意做成合成而不是用归档：变异副本会排除 `runs/`，
    依赖归档的用例在副本里只能 skip —— 那就等于没有守卫。
    """
    trace = Trace(label="synthetic")
    trace.add(ToolCall(step=1, name="query_metrics", args='{"service": "payment"}',
                       phase="investigate", role="metrics"))
    trace.add(ToolResult(
        step=1, name="query_metrics", phase="investigate", role="metrics",
        text="[payment]\n  risk_control_latency_ms = 800.5\n  pool_in_flight = 0\n",
    ))
    trace.add(ToolCall(step=2, name="get_changes", args="{}", phase="investigate", role="change"))
    trace.add(ToolResult(step=2, name="get_changes", phase="investigate", role="change",
                         text="inventory.W_INVENTORY_DOWNSTREAM_RETRIES: 1 -> 5"))
    if with_claim:
        trace.add(Claim(step=3, text=conclusion or "根因是外部风控变慢（risk_control_latency_ms=800.5ms）。",
                        kind="final", phase="conclude"))
    return trace


# ---------------------------------------------------------------- 1) 形式缺陷

def test_injected_fabricated_identifier_is_caught() -> None:
    verdict = judge(run_rules(inject_form_defect(_trace(), "identifier")))
    assert verdict.verdict == "block"
    assert [f.rule for f in verdict.findings] == ["unsupported_claim"]
    assert verdict.findings[0].subject == "fabricated_metric_total"
    assert verdict.findings[0].evidence, "每条发现都必须带证据指针"


def test_injected_hallucinated_number_is_caught() -> None:
    verdict = judge(run_rules(inject_form_defect(_trace(), "number")))
    assert "unverifiable_number" in [f.rule for f in verdict.findings]


def test_clean_trace_is_silent() -> None:
    """干净轨迹必须**零发现** —— 误伤比漏报更致命（会打断正确推理）。"""
    verdict = judge(run_rules(_trace()))
    assert verdict.ok, verdict.lines()
    assert verdict.verdict == "allow"


def test_derived_and_unit_converted_numbers_are_not_flagged() -> None:
    """模型自己算出来的比率、以及换了单位的写法，都**不算**幻觉数字。

    这两条是实测误伤（7 次）换来的：
      · 4433/1400 = 3.166 写成 "3.2"（四舍五入）
      · 证据里是 4.22e+09 字节，结论里写 "4.22GB"（单位换算）
    """
    trace = _trace()
    trace.add(Claim(step=4, text="耗时占比 800.5/818 = 97.9%，约 98%；内存 4.22GB。",
                    kind="final", phase="conclude"))
    # 证据里补两个支撑值
    trace.add(ToolResult(step=9, name="query_logs", phase="investigate", role="logs",
                         text="总耗时=818\n内存=4.22e+09\n"))
    verdict = judge(run_rules(trace))
    assert verdict.ok, verdict.lines()


# ---------------------------------------------------------------- 2) 缺结论 ≠ 没问题

def test_missing_conclusion_is_a_block_not_an_empty_list() -> None:
    findings = run_rules(_trace(with_claim=False))
    assert [f.rule for f in findings] == ["missing_conclusion"]
    assert findings[0].severity == "block"
    assert judge(findings).verdict == "block"


# ---------------------------------------------------------------- 3) 判据不得依赖答案

def test_guard_never_imports_the_answer_side() -> None:
    """ADR-0007 决定 1 的**结构性**守卫：护栏不许 import 评分侧（那里有标准答案）。"""
    offenders: list[str] = []
    for path in sorted(GUARD_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [f"{path.name}: import {a.name}" for a in node.names
                              if a.name.split(".")[0] in ("eval", "scenarios")]
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".")[0]
                if mod in ("eval", "scenarios"):
                    offenders.append(f"{path.name}: from {node.module} import …")
    assert not offenders, f"护栏 import 了评分侧（等于偷看答案）：{offenders}"
    for path in sorted(GUARD_DIR.glob("*.py")):
        assert "SCENARIOS" not in path.read_text(encoding="utf-8"), \
            f"{path.name} 里出现了 SCENARIOS —— 护栏不许碰标准答案"


def test_verdict_does_not_change_when_the_answer_changes() -> None:
    """**行为**守卫：判定只取决于"证据在不在"，与哪条才是真的无关。

    三条结论用**同一条轨迹**：
      · 点名了一个证据里**有**的指标 ⇒ 静默；
      · 点了两个证据里**都没有**的指标（一个"像真的"、一个"像假的"）⇒ 判定**完全一样**。
    护栏看不到答案，也不该表现得像是看到了 —— 这正是 ADR-0007 决定 1 的行为版。
    """
    supported = _trace(conclusion="根因是 risk_control_latency_ms 变慢。")
    fabricated_a = _trace(conclusion="根因是 inventory_stock_leak_total 泄漏。")
    fabricated_b = _trace(conclusion="根因是 payment_gateway_timeout 超时。")

    assert run_rules(supported) == [], "证据里有的指标不该被报"
    a = [f.rule for f in run_rules(fabricated_a)]
    b = [f.rule for f in run_rules(fabricated_b)]
    assert a == b == ["unsupported_claim"], (a, b)


# ---------------------------------------------------------------- 4) 降级的规则不许回线上

def test_demoted_rule_is_not_back_in_the_shipped_set() -> None:
    """`unbased_metric` 实测精确率 7%（19 个正确结论里报 14 个）⇒ 只能是诊断量。

    如果它回到了 `RULES`，那么"好答案零误伤"这条不变量立刻被破坏：
    F8 这类"泄漏确实是真故障但指标只有一个采样值"的正确结论会被报出来。
    """
    assert "unbased_metric" not in {r.__name__.replace("rule_", "") for r in RULES}
    assert any(r.__name__ == "rule_unbased_metric" for r in DIAGNOSTIC_RULES)
    # 它在真实意义上的行为：单点值的指标被当根因 ⇒ 只在诊断量里出现
    trace = _trace(conclusion="根因是 risk_control_latency_ms 变慢。")
    assert run_rules(trace) == []
    assert [f.rule for f in run_diagnostics(trace)] == ["unbased_metric"]


@pytest.mark.parametrize("bad", ["ERROR", "WARNING", "RuntimeError"])
def test_log_levels_are_not_treated_as_metric_names(bad: str) -> None:
    """实测误伤：结论里写「零 ERROR」曾被当成"点名了指标 ERROR"报了一条 block。"""
    trace = _trace(conclusion=f"整窗口零 {bad}，链路全部成功。")
    assert run_rules(trace) == []
