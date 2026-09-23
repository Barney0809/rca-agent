"""
评测层专项用例 —— 盯住"**数字测的是不是你以为的那个东西**"。

============================ 为什么这一层需要专门的用例 ============================

D5 的 baseline 数字**连续作废两次**，两次都不是"算错了"：

  #11  噪声带算错 —— 对**单次尝试的真假值**求 min/max，
       于是恒为 [0,1]；把「布尔值只有两种取值」伪装成了统计量。
  #13  max_steps 是随手定的 —— 恰好卡在某个场景需要的最少步数之下，
       于是把「没跑完就停了」（配置问题）记成了「答错了」（能力问题）。

两次都属于同一个家族：**数字算出来了，但它测的不是你以为的那个东西。**

这类错误比"算错"危险得多 —— 算错会露馅，而这个不会。
你要么读不出它的含义，要么读出一个错误但看起来很专业的含义。
"""

from __future__ import annotations

from eval.runner import Attempt, Report


def _attempt(
    *,
    fault_id: str = "F1",
    round_no: int = 1,
    correct: bool = True,
    steps: int = 5,
    cost: float = 0.01,
    finished: bool = True,
    parse_ok: bool = True,
) -> Attempt:
    return Attempt(
        fault_id=fault_id,
        round_no=round_no,
        correct=correct,
        explanation="",
        root_cause="",
        steps=steps,
        tool_calls=steps * 3,
        cost_yuan=cost,
        input_tokens=1000,
        output_tokens=200,
        elapsed_s=10.0,
        finished=finished,
        parse_ok=parse_ok,
    )


def _report(attempts: list[Attempt], rounds: int = 3) -> Report:
    r = Report(model="test-model", mode="live", rounds=rounds, started_at="t")
    r.attempts = attempts
    return r


# ================================================================
# regression_#11：噪声带必须算在"轮"上，不是算在"单次尝试"上
# ================================================================

def test_regression_11_noise_band_is_over_rounds_not_attempts():
    """回归 #11 —— 这是最能区分新旧实现的一个用例。

    构造：
        第 1 轮：2 次全对  → 该轮准确率 100%
        第 2 轮：2 次对 1  → 该轮准确率 50%

      旧实现（对单次尝试求 min/max）：尝试值是 [T,T,T,F] → 噪声带 [0, 1]
      新实现（先按轮聚合再求极差）：轮值是 [1.0, 0.5] → 噪声带 [0.5, 1.0]

    **两者的结果不同**，所以这条用例能真的区分实现 ——
    如果只是断言 `band == [0,1]`，新旧实现都会通过，那就测不出任何东西。
    """
    attempts = [
        _attempt(round_no=1, correct=True),
        _attempt(round_no=1, correct=True),
        _attempt(round_no=2, correct=True),
        _attempt(round_no=2, correct=False),
    ]
    agg = _report(attempts).agg()

    assert agg["accuracy_per_round"] == [1.0, 0.5], "必须给出每轮的聚合准确率"
    assert agg["accuracy_band"] == [0.5, 1.0], (
        f"噪声带必须算在轮上（[1.0, 0.5] → [0.5, 1.0]），"
        f"实际得到 {agg['accuracy_band']} —— 若为 [0,1] 说明又退回成对单次尝试求极差了"
    )


def test_regression_11_band_is_not_always_zero_to_one():
    """回归 #11 配套 —— 噪声带**不允许**恒为 [0,1]。

    单次尝试的真假值只有两种取值，所以对它们求极差**必然**是 [0,1]（只要有对有错）。
    换句话说：一个恒为 [0,1] 的"噪声带"是**零信息量**的。

    这条用例把上一条的判据抽象成一句更本质的话。
    """
    for rounds_pattern in (
        [(True, True), (True, False)],       # 100% 与 50%
        [(True, True), (False, False)],      # 100% 与 0%
        [(True,), (True,), (True,)],         # 全对
    ):
        attempts = [
            _attempt(round_no=i + 1, correct=c)
            for i, row in enumerate(rounds_pattern)
            for c in row
        ]
        band = _report(attempts).agg()["accuracy_band"]
        if len({sum(row) / len(row) for row in rounds_pattern}) == 1:
            # 各轮准确率相同 → 噪声带应当是"零宽"
            assert band[0] == band[1], f"各轮同分时噪声带应当为零宽，实际 {band}"
        else:
            # 各轮不同 → 带宽应当反映**真实差异**，而不是恒定的 [0,1]
            assert band[1] - band[0] < 1.0 or rounds_pattern == [(True, True), (False, False)], (
                f"噪声带 {band} 看不出各轮的差异"
            )


def test_regression_11_band_functions_exist_for_cost_and_steps_too():
    """噪声带不只是准确率的事 —— 成本与步数同样要按轮聚合。

    否则读者会看到"成本噪声带 ¥0.0093–¥0.0206"这种**单次极差**，
    误以为成本波动很大，而它实际反映的是"不同场景的成本本来就不同"。
    """
    attempts = [
        _attempt(round_no=1, cost=0.010),
        _attempt(round_no=1, cost=0.030),
        _attempt(round_no=2, cost=0.012),
        _attempt(round_no=2, cost=0.032),
    ]
    agg = _report(attempts).agg()

    # 两轮的均值分别是 0.020 与 0.022 → 噪声带很窄
    assert agg["cost_band_yuan"] == [0.02, 0.022], (
        f"成本噪声带必须按轮聚合，实际 {agg['cost_band_yuan']}"
    )
    # 而单次极差是 0.010–0.032 —— 那个数字大得多，且不反映"轮间波动"
    assert agg["cost_band_yuan"][1] - agg["cost_band_yuan"][0] < 0.02


# ================================================================
# regression_#13：准确率必须与收敛率一起出现
# ================================================================

def test_regression_13_aggregate_exposes_convergence_and_parse_rates():
    """回归 #13 —— 聚合结果里**必须**同时有收敛率与 JSON 解析成功率。

    历史：`max_steps=8` 恰好卡在 F3 需要的最少步数之下，于是
    「没跑完就停了」被记成了「答错了」，准确率从 83.3% 掉到 77.8%。
    数字本身没有报错 —— 它只是**测错了东西**。

    所以：只要报告里同时给出收敛率，读者就能自己发现这个陷阱
    （收敛率不到 100% ⇒ 准确率不可用于比较）。
    """
    attempts = [
        _attempt(round_no=1, correct=True, finished=True, parse_ok=True),
        _attempt(round_no=1, correct=False, finished=False, parse_ok=False),
    ]
    agg = _report(attempts).agg()

    assert "fully_converged_rate" in agg, "必须给出收敛率"
    assert "json_parse_rate" in agg, "必须给出 JSON 解析成功率"
    assert agg["fully_converged_rate"] == 0.5
    assert agg["json_parse_rate"] == 0.5


def test_regression_13_report_warns_when_accuracy_is_not_comparable(capsys):
    """回归 #13 的核心 —— **收敛率不足时必须显式告警**。

    没有这条，读者会把一个"被配置污染"的准确率当成模型的真实能力，
    然后拿它去和多 Agent 比较 —— 而那个比较毫无意义。

    所以报告不只是"给出"收敛率，还必须在它不足时**主动指出问题**。
    """
    from eval.runner import print_report

    attempts = [
        _attempt(fault_id="F3", round_no=1, correct=False, finished=False, parse_ok=False),
        _attempt(fault_id="F3", round_no=2, correct=True, finished=True, parse_ok=True),
        _attempt(fault_id="F3", round_no=3, correct=True, finished=True, parse_ok=True),
    ]
    print_report(_report(attempts))
    out = capsys.readouterr().out

    assert "收敛率" in out, "报告里必须出现收敛率"
    assert "不可用于比较" in out or "⚠️" in out, (
        f"收敛率不足时必须告警，否则读者会把配置问题当成能力问题。实际输出：\n{out}"
    )


def test_regression_13_no_warning_when_fully_converged(capsys):
    """正向对照 —— 全部收敛时**不该**出现"不可用于比较"的告警。

    没有这条，"有告警"的断言可能只是"永远有告警"造成的假绿。
    """
    from eval.runner import print_report

    attempts = [
        _attempt(fault_id="F1", round_no=r, correct=True, finished=True, parse_ok=True)
        for r in (1, 2, 3)
    ]
    print_report(_report(attempts))
    out = capsys.readouterr().out

    assert "不可用于比较" not in out, f"全部收敛时不该告警，实际：\n{out}"


def test_regression_13_warns_on_too_few_rounds(capsys):
    """配套 —— 轮数少于 3 时必须提示噪声带不可信。

    单轮跑出来的"噪声带"必然零宽 —— 那是**没测出波动**，不是**波动为零**。
    两者完全不同，必须说清楚。
    """
    from eval.runner import print_report

    attempts = [_attempt(round_no=1, correct=True)]
    print_report(_report(attempts, rounds=1))
    out = capsys.readouterr().out

    assert "噪声带不可信" in out, f"轮数不足时必须提示，实际：\n{out}"
