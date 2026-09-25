"""护栏代理（M3）的机制用例 —— 全部离线，不花钱。

代理是"平台化"的落点：外部 Agent 只跟它打交道。所以这里钉的是四件事：

  1. **工具面**：每次调用的"问了什么/回了什么"都进事件流 —— 护栏判"有没有证据"全靠它；
  2. **上游报错也要记**（`ok=False` + 错误原文），绝不能吞成空字符串
     —— 否则"工具坏了"会看起来像"工具说什么都没有"（#26 的空绿同族）；
  3. **结论面**：`submit_conclusion` 必须真的跑规则并把判定回给对方
     （ADR-0007 决定 3 说"这个接口形状现在就要定下来"）；
  4. **可以改完再交**：每次交卷都单独记录（M2 的教训是"必须能看出改之前是什么样"）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from rca.guard.proxy import GuardedToolFace  # noqa: E402


class _Box:
    """最小的上游工具箱替身：只实现 `call(name, args)`。"""

    def __init__(self, *, raise_on: str = "") -> None:
        self.raise_on = raise_on
        self.calls: list[tuple[str, dict]] = []

    def call(self, name: str, args: dict):
        self.calls.append((name, args))
        if name == self.raise_on:
            raise RuntimeError("上游工具炸了")
        return SimpleNamespace(text=f"[{name}] risk_control_latency_ms = 800.5", ok=True)


def _face(**kw) -> tuple[GuardedToolFace, _Box]:
    box = _Box(**kw)
    return GuardedToolFace(toolbox=box, label="外部 Agent", tool_names=lambda: ["query_metrics"]), box


# ---------------------------------------------------------------- 1) 工具面

def test_tool_call_and_result_are_recorded_in_order() -> None:
    face, box = _face()
    out = face.call("query_metrics", {"service": "payment"})

    assert out["ok"] is True and "800.5" in out["text"]
    assert box.calls == [("query_metrics", {"service": "payment"})], "参数必须原样转发"
    assert [type(e).__name__ for e in face.trace.events] == ["ToolCall", "ToolResult"]
    assert face.trace.results[0].text == out["text"], "返回必须进事件流（护栏要靠它判证据）"
    assert face.n_tool_calls == 1


def test_upstream_failure_is_recorded_not_swallowed() -> None:
    """上游报错 ⇒ 记 `ok=False` + 错误原文，并如实告诉调用方。

    如果把失败吞成空字符串，护栏就会把"工具炸了"读成"工具说没有这件事" ——
    那是把"没测出来"当成"测出来是空的"（#26/#43 同族）。
    """
    face, _ = _face(raise_on="query_metrics")
    out = face.call("query_metrics", {})
    assert out["ok"] is False
    assert "RuntimeError" in out["text"] and "上游工具炸了" in out["text"]
    assert face.trace.results[0].ok is False


# ---------------------------------------------------------------- 2) 结论面

def test_clean_conclusion_is_allowed() -> None:
    face, _ = _face()
    face.call("query_metrics", {"service": "payment"})
    out = face.submit_conclusion("根因是外部风控变慢（risk_control_latency_ms=800.5ms）。")
    assert out["verdict"] == "allow", out["findings"]
    assert out["submission_no"] == 1
    assert face.final_verdict.verdict == "allow"


def test_fabricated_identifier_is_blocked_and_named() -> None:
    """平台化的核心验收：**编出来的指标名**必须当场被抓住、并指名道姓。"""
    face, _ = _face()
    face.call("query_metrics", {"service": "payment"})
    out = face.submit_conclusion("根因是 fabricated_metric_total 异常。")
    assert out["verdict"] == "block"
    rules = [f["rule"] for f in out["findings"]]
    assert rules == ["unsupported_claim"]
    assert out["findings"][0]["subject"] == "fabricated_metric_total"
    assert out["findings"][0]["evidence"], "判定必须带证据指针"


def test_agent_may_fix_and_resubmit_and_both_are_recorded() -> None:
    """改完再交是允许的，但**两次都要留痕**（M2 的教训：看不出改之前是什么样就测不了）。"""
    face, _ = _face()
    face.call("query_metrics", {"service": "payment"})
    first = face.submit_conclusion("根因是 made_up_counter 暴涨。")
    second = face.submit_conclusion("根因是外部风控变慢（risk_control_latency_ms=800.5ms）。")

    assert first["verdict"] == "block" and second["verdict"] == "allow"
    assert [s.no for s in face.submissions] == [1, 2]
    assert "made_up_counter" in face.submissions[0].text
    assert face.final_verdict.verdict == "allow"
    assert face.to_dict()["n_findings"] == 0


# ---------------------------------------------------------------- 3) 没交卷 ≠ 没问题

def test_no_submission_is_a_block_not_a_silent_pass() -> None:
    face, _ = _face()
    face.call("query_metrics", {})
    assert face.final_verdict.verdict == "block"
    assert [f.rule for f in face.final_verdict.findings] == ["missing_conclusion"]


def test_tool_face_exposes_the_names_it_was_given() -> None:
    face, _ = _face()
    assert face.names() == ["query_metrics"]
    assert GuardedToolFace(toolbox=_Box(), label="x").names() == [], "没给清单时不该瞎编"
