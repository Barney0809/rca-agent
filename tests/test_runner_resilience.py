"""运行器的**抗崩**用例 —— 钱花掉了就别丢数据（D21/M2 实测撞出来的）。

真实事故（2026-09-25，跑 M2 的护栏开臂时）：

    openai.APIStatusError: Error code: 402 - Insufficient Balance

账户余额不足，发生在**第一次尝试**里 ⇒ 整个 21 次尝试的运行直接抛异常结束。
那一次没跑完所以没损失，但**换一次运行就可能是"跑完 20 次、第 21 次撞上"**，
20 次已经花掉的钱和结果会一起丢。

修法两条（都在这条用例里钉住）：
  1. 单次尝试失败**跳过并继续**（一次网络抖动不该毁掉整轮）；
  2. **致命错误**（401/402/403）立刻停，但把已完成的尝试**留在 report 里**，
     并把原因写进 `report.aborted_reason` —— 让调用方能存档、能说清为什么。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from eval import runner  # noqa: E402


class _FakeAttempt:
    """够用的 Attempt 替身：run() 只把它塞进 list，再读 correct 打印。"""

    fault_id = "F1"
    round_no = 1
    correct = True
    steps = 1
    tool_calls = 1
    cost_yuan = 0.0
    root_cause = "ok"
    explanation = ""


def _install(monkeypatch, tmp_path, *, fail_on_round: int, exc: BaseException) -> list[int]:
    """把 discover_runs / RunContext / 单次尝试 全换成假的，调用次数记在返回的 list 里。"""
    calls: list[int] = []

    monkeypatch.setattr(runner, "discover_runs", lambda fault_ids=None: {"F1": tmp_path})
    monkeypatch.setattr(
        runner.RunContext, "from_run_dir",
        classmethod(lambda cls, run_dir, **kw: SimpleNamespace(run_id="fake")),
    )
    monkeypatch.setattr(runner, "BaselineAgent", lambda *a, **kw: object())

    def fake_slice(*args, **kwargs):
        rnd = args[3]
        calls.append(rnd)
        if rnd == fail_on_round:
            raise exc
        return _FakeAttempt()

    monkeypatch.setattr(runner, "_run_baseline_slice", fake_slice)
    monkeypatch.setattr(runner, "LlmConfig", SimpleNamespace(from_env=lambda: SimpleNamespace(
        api_key="x", base_url="http://127.0.0.1:9", model_cheap="m", model_strong="m",
        thinking=False, timeout_s=1, max_tokens=16)))
    return calls


def _api_error(code: int, message: str) -> Exception:
    """造一个形态与 openai.APIStatusError 一致的异常（不依赖那个库的内部结构）。"""
    return RuntimeError(f"Error code: {code} - {{'error': {{'message': '{message}'}}}}")


def test_transient_failure_skips_the_round_and_continues(monkeypatch, tmp_path) -> None:
    calls = _install(monkeypatch, tmp_path, fail_on_round=2, exc=RuntimeError("连接被重置"))
    report = runner.run(fault_ids=["F1"], rounds=3, agent="baseline", verbose=False)
    assert calls == [1, 2, 3], "第 2 轮失败后应当继续跑第 3 轮"
    assert [a.round_no for a in report.attempts] == [1, 1], "失败的那一轮不该留下记录"
    assert report.aborted_reason == "", "非致命错误不该让整轮放弃"


def test_fatal_balance_error_aborts_but_keeps_completed_attempts(monkeypatch, tmp_path) -> None:
    calls = _install(monkeypatch, tmp_path, fail_on_round=3,
                     exc=_api_error(402, "Insufficient Balance"))
    report = runner.run(fault_ids=["F1"], rounds=5, agent="baseline", verbose=False)

    assert calls == [1, 2, 3], "致命错误之后不该再试"
    assert len(report.attempts) == 2, "前两次已经跑完 —— 它们**必须**留在 report 里"
    assert "余额不足" in report.aborted_reason
    assert report.aborted_reason.startswith("F1 第3轮"), report.aborted_reason


@pytest.mark.parametrize(
    "text,expect",
    [
        ("Error code: 402 - Insufficient Balance", True),
        ("Error code: 401 - Authentication Fails", True),
        ("Error code: 403 - Forbidden", True),
        ("Error code: 500 - Internal Server Error", False),
        ("连接超时", False),
    ],
    ids=["402", "401", "403", "500", "timeout"],
)
def test_only_auth_and_balance_errors_are_fatal(text: str, expect: bool) -> None:
    assert bool(runner._fatal_api_error(RuntimeError(text))) is expect
