"""M2 软提醒（证据审查者）的机制用例 —— **全部离线，不花一分钱**。

封堵四件事：

  1. **fail-safe**：审查者是新增的 LLM 调用路径，它坏了**绝不能**让整轮白跑
     （三个专员 + 质证 + 裁决的钱都已经花了，见 #15）；
  2. **"没审成" ≠ "审过了没问题"**：解析失败/异常必须留下 `error`，不能悄悄返回空 issues；
  3. **一次自我修正、最多 3 条意见**：不迭代 —— 否则成本不可控，第二次起就是自己说服自己；
  4. **不进判分**：审查意见只进"修订输入"，`correct` 只看结论文本（ADR-0007 决定 2）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from rca.agents.reviewer import (  # noqa: E402
    build_revision_suffix,
    format_issues,
    review_evidence,
)

COORD = ROOT / "src" / "rca" / "agents" / "coordinator.py"
RUNNER = ROOT / "eval" / "runner.py"


class _Result:
    def __init__(self, text: str, cost: float = 0.01) -> None:
        self.text = text
        self.cost_yuan = cost


class _FakeClient:
    """记录每次调用；按 tag 返回预置文本，或按预置异常抛出。"""

    def __init__(self, reply: str = "", *, boom: Exception | None = None) -> None:
        self.reply = reply
        self.boom = boom
        self.calls: list[dict] = []

    def chat(self, *, messages, model=None, tools=None, tag="", **kw):  # noqa: ANN001, ANN003
        self.calls.append({"tag": tag, "messages": messages, "tools": tools})
        if self.boom is not None:
            raise self.boom
        return _Result(self.reply)


MATERIAL = "【logs】payment WARNING ×8049\n【metrics】risk_control_latency_ms = 800.5\n"


# ---------------------------------------------------------------- 1) fail-safe

def test_reviewer_exception_is_swallowed_and_recorded() -> None:
    """审查者抛错时：**不往上抛**，但要如实记下"没审成"。"""
    client = _FakeClient(boom=RuntimeError("上游 500"))
    review = review_evidence(client, MATERIAL, "结论：外部风控变慢。")
    assert review.ran is False
    assert "RuntimeError" in review.error
    assert review.issues == []
    assert client.calls[0]["tag"] == "reviewer"
    assert client.calls[0]["tools"] is None, "审查者不该拿到工具（它没有授权）"


# ---------------------------------------------------------------- 2) 没审成 ≠ 没问题

@pytest.mark.parametrize(
    "bad_reply",
    ["这不是 JSON", '{"issues": "应该是个数组"}'],
    # ⚠️ 显式给 ASCII id：参数化用例的真实 node id 是 `名字[方括号里的 id]`，
    #    变异体里的 `expect_red` 必须写**完整 node id** —— 否则变异检查器会报
    #    "用例名过期"（本次就踩了一次，#47 那一族）。
    ids=["not-json", "issues-not-a-list"],
)
def test_unparsable_review_is_an_error_not_an_empty_pass(bad_reply: str) -> None:
    review = review_evidence(_FakeClient(bad_reply), MATERIAL, "结论：X。")
    assert review.ran is False and review.error
    assert review.issues == []


def test_empty_issue_list_is_a_successful_review() -> None:
    review = review_evidence(_FakeClient('{"issues": []}'), MATERIAL, "结论：X。")
    assert review.ran is True
    assert review.parse_ok is True
    assert review.issues == []


# ---------------------------------------------------------------- 3) 上限与渲染

def test_at_most_three_issues_are_kept() -> None:
    five = "".join(
        '{"claim": "主张%d", "problem": "缺证据", "evidence_needed": "指标值"},' % i
        for i in range(5)
    )
    review = review_evidence(_FakeClient(f'{{"issues": [{five[:-1]}]}}'), MATERIAL, "结论：X。")
    assert len(review.issues) == 3, "最多 3 条 —— 多了会变成一堵墙，模型只会敷衍"


def test_revision_suffix_carries_the_issues_and_the_permission_to_disagree() -> None:
    issues = [{"claim": "内存泄漏是根因", "problem": "材料里只有单点值", "evidence_needed": "基线"}]
    text = build_revision_suffix(issues)
    assert "内存泄漏是根因" in text and "基线" in text
    assert "保持原判断" in text, "审查者不一定对 —— 必须允许协调者说明理由后不改"


def test_format_issues_handles_missing_fields() -> None:
    assert format_issues([]) == "（审查者没有提出问题）"
    assert "主张" in format_issues([{"claim": "x"}])


# ---------------------------------------------------------------- 4) 结构不变量（不参与判分）

def test_reviewer_never_touches_the_answer_or_the_scoring_path() -> None:
    """两条结构性检查（都在源码层，跑一次几毫秒）：

    · 审查者的输出只出现在 `adjudicate(extra_suffix=…)` 里，不进 `correct`；
    · 评分仍然只吃**结论文本**。
    """
    coord = COORD.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")

    # 评分只吃结论文本（护栏字段绝不参与）
    assert "correct, _ = score.judge(verdict_text)" in runner
    assert "score.judge(" in runner and "guard" not in runner.split("score.judge(")[1][:80]

    # 审查意见只以"附在同一条 user 消息后的后缀"形式进入裁决
    assert "extra_suffix=build_revision_suffix(review.issues)" in coord

    # 一次自我修正、不迭代：adjudicate 在流水线里只被调用两次（首次裁决 + 修订一次）
    call_sites = coord.count("= adjudicate(")
    assert call_sites == 2, f"adjudicate 的调用点变成 {call_sites} 处 —— 修正必须只有一次（不迭代）"
    assert "revised = adjudicate(" in coord, "修订那一处不见了？"

    # 修订前/后的对照必须留档，否则"修订帮了还是帮了倒忙"无法回答
    assert 'out.guard_review["pre_revision_root_cause"] = out.verdict.root_cause' in coord
    assert "revision_hurt" in runner, "前/后对照没接到存档里"
