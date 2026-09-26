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

import pytest

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
    root_cause: str = "",
) -> Attempt:
    return Attempt(
        fault_id=fault_id,
        round_no=round_no,
        correct=correct,
        explanation="",
        root_cause=root_cause,
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
    # ⚠️ 这里**只能**断言"那一条"具体告警。
    #    第一版写的是 `"不可用于比较" in out or "⚠️" in out` —— 那个 `or` 分支
    #    让这条用例在**任何**别处出现 ⚠️ 时都成立：D15 加了"小样本告警"之后，
    #    3 轮的报告里**永远**有 ⚠️，于是变异体 `hist-convergence-warning-always-off`
    #    （把收敛告警关掉）再也变不红 —— 全量对账当场抓到（harness-log #47）。
    #    ⇒ 存在性断言不许留"或"的后门。
    assert "不可用于比较" in out, (
        f"收敛率不足时必须给出**那一条**告警。实际输出：\n{out}"
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


# ================================================================
# #16 评分器把"被否掉的提及"算作命中
# ================================================================
#
# 真实事故（D7，F2 第一次跑通多 Agent）：
#
#   协调者的裁决原文是：
#     「…把 order 入口连接池容量砍到 2… 下游 payment 风控变慢只是并发的放大因素，
#       不是触发者。」
#
#   它**采纳了红鲱鱼（配置变更）**，把真根因（外部风控变慢）当作被驳回的干扰项 ——
#   但这句话里同时含"风控"和"变慢"，于是旧的纯关键词判定给出 **100%，判对**。
#
#   **多 Agent 实际上掉进了红鲱鱼陷阱，而分数说它答对了。**
#
# 这是 harness-log #11 #12 #13 的同一家族：数字算出来了，但测的不是那个东西。
# 而且它比前几个更危险 —— 前面几个是"数字没意义"，这个是
# **数字恰好把失败报成了成功**，直接指向错误的结论。

F2_DISMISSED = (
    "05:44:18 的配置变更 order.W_ORDER_POOL_SIZE 64→2 把 order 入口连接池容量砍到 2，"
    "使 order 在正常并发下立即饱和（in_flight=2），97% 的 create_order 请求在 400ms 内"
    "拿不到连接而失败；下游 payment 风控变慢只是并发的放大因素，不是触发者。"
)

F2_ASSERTED = (
    "根因是外部风控响应变慢（单次约 801ms），它把 order 的每个请求都拖长，"
    "导致池容量为 2 的 order 连接池被长时间占满；order 的池耗尽是被放大的后果，不是根因。"
)


def test_regression_16_dismissed_cause_is_not_a_hit():
    """核心：把真根因当干扰项排除掉的答案，必须判错。"""
    from eval.scenarios import SCENARIOS

    sc = SCENARIOS["F2"]
    ok, _ = sc.judge(F2_DISMISSED)

    assert ok is False, (
        "这段裁决**采纳了红鲱鱼**、把真根因（外部风控变慢）明确降级成了干扰项，"
        "必须是判错。旧的纯关键词判定会判对 —— 那正是这个缺陷。"
    )
    # 说明文字要指出"词出现了但被否掉了"，而不是含糊地说"缺少关键概念"
    why = sc.explain(F2_DISMISSED)
    assert "被否掉" in why or "降级" in why, f"说明没有指出真正的问题：{why}"


def test_regression_16_asserted_cause_is_still_a_hit():
    """正向对照：正常主张真根因的答案，必须判对。

    没有这一条，"判错"可能只是"永远判错"造成的假绿。
    """
    from eval.scenarios import SCENARIOS

    ok, _ = SCENARIOS["F2"].judge(F2_ASSERTED)
    assert ok is True, "这是标准答案的写法，不该判错"


def test_regression_16_negation_inside_a_clause_is_handled():
    """反向保护：**否定/让步和真答案在同一句、但在不同小句**时不能误伤。

    这是很自然的正确措辞：
        「order 的连接池只是被放大的脆弱点，真正的根因是外部风控变慢。」

    这一句里**同时**有让步标记（"只是"）和真关键词（"风控"+"变慢"）。
    如果只按句号切、把整句当一个整体看，它会因为含"只是"被判定成
    "在否掉风控" —— **把对的答案判成错的**。

    误伤比漏判更糟：它会让本来正确的 Agent 显得无能，而且很难被发现
    （分数变低总是容易被归因成"模型不行"）。

    所以判定必须切到**小句**级别（逗号也切）：
        前半句被让步标记否掉、后半句正常主张 ⇒ 判对。

    ⚠️ 这条用例的文本是**特意挑的**：
       必须让"只按句号切"和"按逗号也切"得出**不同结果**，否则它什么都测不出来。
       （第一版用的是「根因不是 A，而是 B」，那种写法里没有任何让步标记，
         两种切法结论相同 —— 变异测试当场证明那条用例是装饰品。）
    """
    from eval.scenarios import SCENARIOS

    text = "order 的连接池只是被放大的脆弱点，真正的根因是外部风控变慢。"
    ok, _ = SCENARIOS["F2"].judge(text)
    assert ok is True, (
        "「A 只是症状，真正的根因是 B」这种写法不能被误伤 —— "
        "注意这里的让步标记和真关键词在**同一句**里"
    )

    # 同时保留"否定 + 转折"的写法（另一种常见的正确措辞）
    text2 = "根因不是 order 的连接池，而是外部风控变慢。"
    ok2, _ = SCENARIOS["F2"].judge(text2)
    assert ok2 is True, "「不是 A，而是 B」这种否定+转折的写法也不能被误伤"


def test_regression_16_old_behaviour_is_still_reachable():
    """`dismissal_aware=False` 必须能退回旧行为 —— 用来量化"改了多少结论"。

    历史数字不能因为改了规则就悄悄变化：得能同时算出新旧两版，
    才能说清"哪几条结论变了、为什么变"（工具是 `eval/rescore.py`）。
    """
    from eval.scenarios import SCENARIOS

    sc = SCENARIOS["F2"]
    old_ok, _ = sc.judge(F2_DISMISSED, dismissal_aware=False)
    new_ok, _ = sc.judge(F2_DISMISSED, dismissal_aware=True)

    assert old_ok is True, "旧行为应当判对（这正是要被修正的假阳性）"
    assert new_ok is False, "新行为应当判错"
    assert old_ok != new_ok, "这条用例的价值就在于两种行为**确实不同**"


# ================================================================
# #17 交叉质证的步数预算没有接线到 --max-steps
# ================================================================
#
# 真实事故：`cross_examine(..., max_steps=4)` 是写死的默认值，
#   `--max-steps` 根本到不了它。实测中指标 Agent 的交叉质证**正好用满 4 步**
#   却没产出结构化结论（parse_ok=False）→ 整体收敛率被记成 0%。
#
# 更糟的是报告据此建议"提高 --max-steps 重测" —— 而那个参数改不动这个预算，
# **照着建议做会白花一遍钱**。这是 #13 在另一个地方复发：
# 一个没人量过的预算数字，会悄悄变成结论的一部分。


def test_regression_17_cross_examine_has_no_default_step_budget():
    """结构面：`cross_examine` 不许有默认步数预算。

    有默认值 = 调用方可以什么都不想就用上它，而这正是事故的成因。
    要求显式传参，等于强制调用方**做一次决定**。
    """
    import inspect

    from rca.agents.coordinator import cross_examine

    params = inspect.signature(cross_examine).parameters
    assert "max_steps" in params, "cross_examine 应当接受 max_steps"
    assert params["max_steps"].default is inspect.Parameter.empty, (
        "cross_examine 的 max_steps 不许有默认值 —— 写死 4 的那个坑（harness-log #17）"
        "就是默认值造成的。调用方必须显式决定步数预算。"
    )


def test_regression_17_runner_passes_cross_exam_steps_downstream():
    """行为面：runner 的 `cross_exam_steps` 必须真的传到 `diagnose_multi`。

    只改 CLI 参数名、不接线，是这个缺陷最可能的复发方式 ——
    参数看起来存在、能设置、却没有任何效果。所以这里拦住 `diagnose_multi`
    并把**实际收到的参数**记下来核对。
    """
    from unittest.mock import patch

    from eval.runner import _run_multi_slice
    from eval.scenarios import SCENARIOS
    from rca.agents.coordinator import MultiAgentResult

    captured: dict = {}

    def fake_diagnose(client, ctx, **kwargs):
        captured.update(kwargs)
        return MultiAgentResult()

    with patch("rca.agents.coordinator.diagnose_multi", fake_diagnose):
        _run_multi_slice(
            client=None, ctx=None, fid="F2", rnd=1, score=SCENARIOS["F2"],
            model=None, max_steps=7, cross_exam_steps=11,
        )

    assert captured.get("cross_exam_steps") == 11, (
        f"cross_exam_steps 没有传到 diagnose_multi（实收 {captured}）。"
        "参数存在但没有效果，与这个缺陷本身等价。"
    )
    assert captured.get("max_steps") == 7, f"max_steps 也应原样传下去（实收 {captured}）"


def test_regression_17_warning_points_at_the_right_knob(capsys):
    """报告在多 Agent 未收敛时，必须指出**两个**预算旋钮，而不是只提 --max-steps。

    原来只说"提高 --max-steps"，而实测没收敛的是交叉质证 ——
    照着建议做会白花钱。**给出无法生效的建议，比不给建议更坏。**
    """
    from eval.runner import print_report

    attempt = _attempt(round_no=1, correct=False, finished=False)
    attempt.detail = {
        "hypotheses": [{"role": "logs", "finished": True, "steps": 6}],
        "cross_exams": [{"role": "metrics", "finished": False, "steps": 4}],
        "verdict": {"parse_ok": True},
    }
    report = _report([attempt], rounds=3)
    report.agent = "multi"

    print_report(report)
    out = capsys.readouterr().out

    assert "cross-exam-steps" in out, f"必须指出交叉质证的旋钮，实际：\n{out}"
    assert "metrics" in out, f"必须指出是哪个环节没收敛，实际：\n{out}"


# ================================================================
# #18 存档漏字段 → 报告说"没收敛"却查不出是谁
# ================================================================
#
# 真实事故：`CrossExam.to_dict()` 没保存 `finished`，
#   于是 results.json 里交叉质证只有 parse_ok，
#   报告说"收敛率 0%"时**根本查不出是哪个 Agent 没收敛**。
#   存档的价值就在于事后定位问题；少一个字段就少一条线索。


def test_regression_18_cross_exam_snapshot_records_finished():
    from rca.agents.coordinator import CrossExam

    x = CrossExam(role="metrics", original_claim="x", finished=False, parse_ok=False, steps=4)
    d = x.to_dict()

    assert "finished" in d, (
        "CrossExam 的存档必须包含 finished —— 否则报告说'没收敛'时无法定位是哪个角色"
    )
    assert d["finished"] is False
    assert d["steps"] == 4, "同时要能看到它用了几步（判断是不是撞了预算上限）"


# ================================================================
# #19 存档把结论截断，导致判定无法复核（违反 FR-C 可复现）
# ================================================================
#
# 真实事故：`root_cause=diag.root_cause[:400]` —— 存的是**截断文本**，
#   而 `correct` 是按**全文**算的。后果：
#   18 条里正好有 2 条（F4 第2轮、F5 第2轮，文本恰好 400 字符整），
#   用存档文本重新判一遍会得到**不同的结论**。
#
#   也就是说：**存档的证据无法复核它自己记录的判定。**
#   面试官拿到 results.json，本该能自己重算每个数字，而不是只能相信里面的布尔值。
#
#   发现方式：`eval/rescore.py` 逐条比对"存档里的 correct"与"用存档文本重算的判定"。


def test_regression_19_saved_report_can_reproduce_its_own_verdict(tmp_path, monkeypatch):
    """核心不变量：**用存档文本重算，必须得到存档里那个判定。**

    这条不变量比"某个字段没被截断"更本质 —— 它直接表达"存档可以复核自己"，
    因此任何形式的证据丢失（截断、编码、丢字段）都会被它抓到。
    """
    import json

    from eval.runner import load_report, save_report
    from eval.scenarios import SCENARIOS

    sc = SCENARIOS["F1"]
    # 造一段**远超 400 字符**的结论，且关键词只出现在**后面** ——
    # 只要存档截断，重算就会判错。
    long_text = "先是一大段与判定无关的铺垫。" * 40 + "根因是外部依赖风控变慢。"
    assert len(long_text) > 500, "构造的文本必须超过旧的 400 字符截断线"

    attempt = _attempt(fault_id="F1", round_no=1, correct=sc.judge(long_text)[0])
    attempt.root_cause = long_text
    report = _report([attempt])

    monkeypatch.setattr("eval.runner.RUNS_DIR", tmp_path)
    path = save_report(report)
    reloaded = load_report(path)

    stored = reloaded.attempts[0]
    assert stored.root_cause == long_text, (
        f"存档把结论截断了（{len(stored.root_cause)} 字符，原文 {len(long_text)}）—— "
        "存档的证据必须能复核它自己的判定（FR-C 可复现）"
    )
    recomputed, _ = sc.judge(stored.root_cause)
    assert recomputed == stored.correct, (
        "用存档文本重算得到的判定与存档里记录的不一致 —— 存档无法自证"
    )
    assert recomputed is True, "这段文本是正确答案，重算应当是判对"


def test_regression_19_baseline_slice_stores_the_whole_verdict():
    """行为面：**截断发生的那一行**必须被直接覆盖。

    ⚠️ 这条用例是补写的。第一版只测了 `save_report` / `load_report` 的往返，
       结果变异测试当场证明它抓不到截断 —— 因为截断发生在
       `_run_baseline_slice` 里，而那条用例根本没走到那里。
       **存档层是好的，坏的是构造存档的那一层。**

       这正是"变异测试"的价值：它不问"用例绿不绿"，只问"缺陷回填后它还绿不绿"。
    """
    from types import SimpleNamespace

    from eval.runner import _run_baseline_slice
    from eval.scenarios import SCENARIOS

    sc = SCENARIOS["F1"]
    full_text = "先是一大段与判定无关的铺垫。" * 40 + "根因是外部依赖风控变慢。"
    assert len(full_text) > 500, "构造的文本必须超过旧截断线（400 字符）"

    class FakeBaseline:
        def diagnose(self, ctx):
            return SimpleNamespace(
                root_cause=full_text,
                steps=3, tool_calls=5, cost_yuan=0.01,
                input_tokens=1, output_tokens=1, elapsed_s=1.0,
                finished=True, parse_ok=True,
            )

    attempt = _run_baseline_slice(FakeBaseline(), ctx=None, fid="F1", rnd=1, score=sc)

    assert attempt.root_cause == full_text, (
        f"存档只留下 {len(attempt.root_cause)} 字符（原文 {len(full_text)}）—— "
        "结论被截断了，而 correct 是按全文算的，于是存档无法复核自己的判定"
    )
    recomputed, _ = sc.judge(attempt.root_cause)
    assert recomputed == attempt.correct, "用存档文本重算必须得到存档里记录的判定"


# ================================================================
# #20 作废的场景不得参与聚合
# ================================================================
#
# F2 的答案被对照实验证明是反的（harness-log #20）：
# 它会把答对的扣分、把答错的加分。
#
# 但它**保留在目录里作为反例留档**（"我们出错过一道题"比正确答案更有教学价值）。
# 于是产生一个新的风险：**坏题目继续参与准确率聚合，污染每一次比较**。
#
# 而且这种污染**看不出来** —— 你只会觉得"准确率怎么这么低"，
# 不会想到是某道题的答案定错了。
#
# 所以：作废必须是一个**机制**（字段 + 聚合层过滤 + 报告显式说明），
# 而不是"记得别把它算进去"。


def test_regression_20_invalidated_scenario_is_excluded_from_aggregate():
    """核心：作废场景的尝试必须被排除在所有数字之外。"""
    from eval.runner import Report

    # F1 全对、F2 全错。若 F2 参与聚合，准确率会被拉到 50%；排除后应为 100%。
    attempts = [
        _attempt(fault_id="F1", round_no=r, correct=True) for r in (1, 2, 3)
    ] + [
        _attempt(fault_id="F2", round_no=r, correct=False) for r in (1, 2, 3)
    ]
    agg = Report(
        model="m", mode="live", rounds=3, started_at="t", attempts=attempts
    ).agg()

    assert agg["accuracy"] == 1.0, (
        f"F2 已作废，不该影响准确率；实际 {agg['accuracy']:.0%}。"
        "坏题目参与聚合会污染每一次比较，而且看不出来。"
    )
    assert "F2" not in agg["per_fault"], "作废场景不该出现在逐场景表里"
    assert "F1" in agg["per_fault"], "有效场景必须还在"


def test_regression_20_exclusion_is_disclosed_not_silent():
    """排除必须**说出来**，不能悄悄不显示。

    否则读报告的人会以为"所有场景都算进去了" —— 那又是一种
    "数字看起来没问题，但它测的不是你以为的东西"。
    """
    from eval.runner import Report

    attempts = [_attempt(fault_id="F1", round_no=r, correct=True) for r in (1, 2, 3)]
    attempts += [_attempt(fault_id="F2", round_no=1, correct=False)]
    agg = Report(
        model="m", mode="live", rounds=3, started_at="t", attempts=attempts
    ).agg()

    assert "F2" in agg["invalidated"], "聚合结果里必须带上作废场景及理由"
    assert agg["invalidated"]["F2"], "理由不能是空字符串"
    assert agg["n_excluded_attempts"] == 1
    assert agg["n_attempts_total"] == 4, "总数要保留，才能看出「排除了多少」"


def test_regression_20_the_exclusion_check_is_not_vacuous():
    """元测试：确认 F2 真的被标成作废了，否则上面两条测的是空气。"""
    from eval.scenarios import SCENARIOS

    assert SCENARIOS["F2"].invalidated_reason, (
        "F2 必须带 invalidated_reason —— 它的答案已被对照实验证明是反的（#20）"
    )
    # 其他场景不该被误标（否则会把有效数据也排除掉）
    live = [fid for fid, s in SCENARIOS.items() if not s.invalidated_reason]
    assert "F7" in live, "F7 是合格的红鲱鱼场景，不能被标成作废"
    assert len(live) >= 6, f"被标作废的场景太多了：{sorted(SCENARIOS)}"


# ================================================================
# #21 否定语境判定再次被措辞绕过
# ================================================================
#
# 真实经过（F8 第一次跑多 Agent）：报告给出 100%，看着就是"多 Agent 第一次被证明有用"。
# 但读了裁决原文才发现它在讲：
#
#     「…order 的 4.22e+09 字节内存泄漏在本窗口内无功能/资源影响，
#        是被放大的脆弱点而非触发者。」
#
# **它把内存泄漏明确降级了** —— 最终结论只给了一个根因，和 baseline 一样不完整。
# 那个 100% 是假的：`DISMISSAL_MARKERS` 里没有"而非""脆弱点"这类措辞。
#
# ⚠️ 这条比 #16 更值得记住的地方：
#     #16 之后我做了变异测试、写了 4 条用例、全部 SEALED ——
#     但那 4 条用的都是**同一种措辞**（"只是…并非根因"）。
#     变异测试只能证明"能抓住作者想象过的那种错误"，
#     **证明不了能抓住没见过的措辞** —— 因为变异体也是作者写的。
#
# 所以这一组用例刻意用**多种不同的降级措辞**，而不是同款的第四遍。


# 每一种都是真实出现过的"把关键词降级掉"的写法
DISMISSAL_PHRASINGS = [
    # #16 那一版（旧场景里出现过的）
    "下游 payment 风控变慢只是并发的放大因素，不是触发者。",
    # #21 那一版（F8 裁决的原文形状）
    "order 的 4.22e+09 字节内存泄漏在本窗口内无功能/资源影响，是被放大的脆弱点而非触发者。",
    "内存泄漏只是伴随现象，无法解释观测到的延迟。",
    "该泄漏不足以解释本次异常。",
    "order 的内存泄漏与本次故障无关。",
    "内存泄漏这一说法站不住。",
    "泄漏不能解释延迟，应予排除。",
]


@pytest.mark.parametrize("text", DISMISSAL_PHRASINGS)
def test_regression_21_many_dismissal_phrasings_are_all_caught(text: str) -> None:
    """多种"降级措辞"都必须被识别为"没有主张"。

    刻意用 `parametrize` 把 7 个措辞样本展开成 7 条独立用例 ——
    这样失败信息能直接指出**是哪种措辞漏了**，而不是笼统地说"这一组挂了"。

    ⚠️ 第一版把 parametrize 写在了一个**嵌套函数**上，然后手动循环调用。
       那是装饰：pytest 根本不收集嵌套函数，真正生效的是下面那个 for 循环。
       "看起来参数化了、其实没有" —— 这正是本项目一直在清掉的那类东西。
    """
    from eval.scenarios import SCENARIOS

    sc = SCENARIOS["F6"]   # F6 的判据就是「内存/泄漏」+「order」，用它来测最直接
    ok, _ = sc.judge(text)
    assert not ok, (
        f"这段话把关键词降级/否定了，不该算作主张：\n  {text}\n"
        "（关键词出现 ≠ 主张它 —— 见 harness-log #16 #21）"
    )


def test_regression_21_asserting_still_counts():
    """正向对照：正常主张的写法不能被误伤。

    没有这一条，"能识别降级"可能只是"永远判错"造成的假绿。
    """
    from eval.scenarios import SCENARIOS

    ok, _ = SCENARIOS["F6"].judge("order 自身存在内存泄漏，每次请求约泄漏 0.5MB。")
    assert ok, "这是标准答案的写法，不该判错"


def test_regression_21_the_phrasing_samples_are_actually_diverse():
    """元测试：确认样本真的是**不同的措辞**，而不是同一句话抄了 7 遍。

    这正是 #21 的教训：同款重复的用例集看起来很多，但覆盖面是一个点。
    """
    tails = {p[-6:] for p in DISMISSAL_PHRASINGS}
    assert len(tails) >= 5, (
        f"措辞样本的尾部只有 {len(tails)} 种 —— 太同质了，"
        "覆盖不到真实世界里措辞的多样性（#21 就是这么漏的）"
    )


# ================================================================
# F8 两轴判定：把"找到了但没当成根因"表达出来
# ================================================================
#
# F8 上真实发生的是第三种结局：
#     metrics 专员**找到了**内存泄漏 → 交叉质证说服它改口 → 裁决把它降级成"伴随现象"
#
# 单一的关键词组判定表达不了它：看到词就算对（假阳性，见 #21），
# 被否掉就算错（那就和 baseline 的"从没找到"混为一谈）。**两种都不对。**
#
# 所以拆成两轴：
#     结论轴 = 最终答案有没有主张它（**同口径**，可以拿来比高低）
#     召回轴 = 这次尝试里任何环节有没有找到它（**不同口径**，回答"分工有没有覆盖到"）


# F8 裁决的真实措辞（来自 multi-20260925-072846）
F8_VERDICT_DISMISSES_LEAK = (
    "payment 所依赖的外部风控服务响应变慢（约 800ms/次），导致 payment 扣款耗时被拉长；"
    "order 的 4.22e+09 字节内存泄漏在本窗口内无功能/资源影响，是被放大的脆弱点而非触发者。"
)


def test_f8_verdict_axis_separates_the_two_causes():
    """结论轴：风控变慢被主张，内存泄漏被降级 —— 必须一真一假。"""
    from eval.scenarios import SCENARIOS, asserted_causes

    sc = SCENARIOS["F8"]
    axes = asserted_causes(F8_VERDICT_DISMISSES_LEAK, sc.required_causes)

    assert axes["外部风控变慢"] is True
    assert axes["内存泄漏"] is False, (
        "这段裁决把泄漏明确降级成了「伴随现象」，结论轴必须是 False"
    )


def test_f8_recall_axis_sees_the_claim_a_specialist_raised():
    """召回轴的核心：**专员提过就算找到**，哪怕最终裁决把它否掉了。

    这正是 F8 上 multi 相对 baseline 的**唯一**差别。
    """
    from eval.runner import Attempt, attempt_cause_axes

    attempt = Attempt(
        fault_id="F8", round_no=1, correct=False, explanation="",
        root_cause=F8_VERDICT_DISMISSES_LEAK,
        steps=28, tool_calls=89, cost_yuan=0.06,
        input_tokens=0, output_tokens=0, elapsed_s=27.0,
        finished=True, parse_ok=True,
        detail={
            "hypotheses": [
                {"role": "logs", "claim": "payment 调外部风控响应缓慢，约 800ms"},
                {"role": "metrics", "claim": "order 自身存在约 4.22e+09 字节的内存泄漏"},
                {"role": "change", "claim": "本时段没有任何配置变更"},
            ],
            "cross_exams": [],
            "verdict": {"parse_ok": True},
        },
    )

    axes = attempt_cause_axes(attempt)

    assert axes["verdict"]["内存泄漏"] is False, "结论轴：裁决否掉了它"
    assert axes["recall"]["内存泄漏"] is True, (
        "召回轴：metrics 专员**找到过**它 —— 这一格正是 multi 相对 baseline 的唯一差别"
    )
    assert axes["recall"]["外部风控变慢"] is True


def test_f8_recall_equals_verdict_for_single_answer_agents():
    """baseline（没有 detail）的召回必须**退化成**它的最终答案。

    否则会凭空给 baseline 记上一笔"找到了"——那是对比里的作弊。
    """
    from eval.runner import Attempt, attempt_cause_axes

    attempt = Attempt(
        fault_id="F8", round_no=1, correct=False, explanation="",
        root_cause="外部风控服务响应缓慢（约800ms），导致全链路延迟升高。",
        steps=6, tool_calls=24, cost_yuan=0.012,
        input_tokens=0, output_tokens=0, elapsed_s=9.4,
        finished=True, parse_ok=True, detail=None,
    )

    axes = attempt_cause_axes(attempt)

    assert axes["recall"] == axes["verdict"], (
        "单答案的 Agent 只有一个环节（它的最终答案），两轴必须一致；"
        "若不一致，说明召回轴凭空多算了什么"
    )
    assert axes["recall"]["内存泄漏"] is False, "baseline 从没提过泄漏"


def test_f8_axes_are_empty_for_single_fault_scenarios():
    """单故障场景不该打两轴（它只有一个原因，两轴必然相同，属于噪音）。"""
    from eval.runner import Attempt, attempt_cause_axes

    attempt = Attempt(
        fault_id="F1", round_no=1, correct=True, explanation="",
        root_cause="外部风控变慢", steps=5, tool_calls=20, cost_yuan=0.01,
        input_tokens=0, output_tokens=0, elapsed_s=8.0,
        finished=True, parse_ok=True,
    )
    assert attempt_cause_axes(attempt) == {}


def test_f8_declares_both_causes_so_the_axes_are_not_vacuous():
    """元测试：F8 必须真的声明了两个原因，否则上面几条测的是空气。"""
    from eval.scenarios import SCENARIOS

    f8 = SCENARIOS["F8"]
    names = [c.name for c in f8.required_causes]
    assert names == ["外部风控变慢", "内存泄漏"], f"F8 的原因清单不对：{names}"

    # 其他场景不该有原因清单（它们只有一个原因）
    others = [fid for fid, s in SCENARIOS.items() if s.required_causes and fid != "F8"]
    assert not others, f"这些场景不该声明 required_causes：{others}"

# ================================================================
# #28 关键词侧的三分类：判据是**合取**，就不能用**析取**判断「提没提过」
# ================================================================
#
# 真实 bug（D10 第二步，被审计脚本当场抓出来）：
#
#   `keyword_verdict` 用「**任意一组**出现关键词」判断「提到过」，
#   而一个原因的判据是**所有组**的合取。后果：
#
#     F4 的判据 =（重试/retry）+（inventory/库存）
#     一段**从没提过重试配置**的答案，因为传播链写了
#     「payment→inventory→order」，就含了 `inventory` ——
#     于是被判成「提到了但否掉了」(dismissed)，而真相是 **absent**。
#
#   ⇒ 一句话：**判据是合取，就不能用析取去判断「它提没提过」。**
#
# 为什么值得单独立用例：这个错误**不会报错、不会崩**，
# 它只是让"一致率"这类对比数字**悄悄失真** ——
# 当时审计显示 6/8 一致，看着像"裁判与词表有分歧"，
# 其实是我的对比函数自己错了。


def _f4_cause():
    from eval.scenarios import SCENARIOS, Cause

    f4 = SCENARIOS["F4"]
    return Cause(name="重试配置漂移", keyword_groups=f4.keyword_groups)


def test_regression_28_absent_needs_every_group_missing_not_just_one():
    """核心：**只命中了判据里的一组**，结论必须是 absent，不是 dismissed。

    这段原文来自真实存档（baseline F4 第1轮），它压根没提重试配置 ——
    但传播链里写了 `inventory`。按析取判断就会误判成 dismissed。
    """
    from eval.scenarios import keyword_verdict

    text = (
        "payment 服务调用的外部风控不可用（risk control unavailable），"
        "导致扣款失败，故障沿 payment→inventory→order 逐层向上传播。"
    )
    assert keyword_verdict(text, _f4_cause()) == "absent", (
        "这段提到了 inventory（判据里的一组），但**从没提过重试**（另一组）；"
        "判据是合取，所以它属于「根本没提」，不是「提到了但否掉了」"
    )


def test_regression_28_dismissed_when_all_groups_present_but_denied():
    """正向对照：两组都出现过、且被否掉 → dismissed。"""
    from eval.scenarios import keyword_verdict

    text = "外部风控不可用导致失败；inventory 的重试次数变更只是放大了错误量，并非根因。"
    assert keyword_verdict(text, _f4_cause()) == "dismissed"


def test_regression_28_asserted_path_still_works():
    """正向对照：正常主张 → asserted（防止「一律判 absent」的假绿）。"""
    from eval.scenarios import keyword_verdict

    assert keyword_verdict("根因是 inventory 的重试次数被从 1 改为 5。", _f4_cause()) == "asserted"


def test_regression_28_the_three_way_split_is_actually_reachable():
    """元测试：三种结局都必须能被真实产生。

    否则这个三分类可能退化成"永远只返回两种"，而用例仍然全绿 ——
    那正是 #28 这类"数字悄悄失真"的温床。
    """
    from eval.scenarios import keyword_verdict

    cause = _f4_cause()
    got = {
        keyword_verdict("根因是 inventory 的重试次数被从 1 改为 5。", cause),
        keyword_verdict("外部风控不可用；inventory 的重试次数变更只是放大了错误量，并非根因。", cause),
        keyword_verdict("payment 调用的外部风控不可用，故障沿 payment→inventory→order 传播。", cause),
    }
    assert got == {"asserted", "dismissed", "absent"}, f"三种结局没被全覆盖：{got}"

# ================================================================
# D10：两把尺子接进评分路径
# ================================================================
#
# 设计由数据决定（harness-log #28）：
#   关键词判定 = 主判据（免费、确定、已知措辞上全对）
#   LLM 裁判   = **独立交叉校验**，**不参与 correct 的计算**
#   两者分歧 → 单列出来（#16/#21 正是这样被发现的）
#
# 裁判**不参与** correct 的理由：它的成本与抖动都还没在规模上量清楚
# （目前自一致 8/8，但样本很小）。直接拿它当主判据，
# 等于把一个没量清的误差源引进所有数字里 —— 那是本项目一直在防的事。


def _sc(fid: str = "F1"):
    from eval.scenarios import SCENARIOS

    return SCENARIOS[fid]


def _attempt_with(fid: str, correct: bool, detail: dict | None = None):
    from eval.runner import Attempt

    return Attempt(
        fault_id=fid, round_no=1, correct=correct, explanation="",
        root_cause="外部风控响应变慢（约800ms）", steps=5, tool_calls=20,
        cost_yuan=0.01, input_tokens=0, output_tokens=0, elapsed_s=8.0,
        finished=True, parse_ok=True, detail=detail or {},
    )


def test_judge_detail_records_a_verdict_per_required_cause():
    from eval.runner import judge_detail

    seen: list[str] = []

    def fake(text, cause):
        seen.append(cause)
        return "asserted"

    d = judge_detail(_sc("F1"), "外部风控变慢", fake)
    assert d["judge"] == {"外部风控变慢": "asserted"}
    assert seen == ["外部风控变慢"], "对每个必需原因各判一次"

    # 多故障场景（F8）要判**两条**
    d8 = judge_detail(_sc("F8"), "风控变慢 + 内存泄漏", fake)
    assert set(d8["judge"]) == {"外部风控变慢", "内存泄漏"}, d8


def test_judge_detail_is_empty_when_no_judge_is_configured():
    """没传 judge_fn（默认）→ 什么也不记，也**不该多花一分钱**。"""
    from eval.runner import judge_detail

    assert judge_detail(_sc("F1"), "任意文本", None) == {}


def test_judge_failure_is_never_counted_as_agreement():
    """⚠️ 核心：裁判**自己失败**时，绝不能被当成"两把尺子一致"。

    这是本项目反复在防的一类静默失真：
    把"没测出来"伪装成"测出来了一致"。
    """
    from eval.runner import judge_asserts_all, judge_detail

    for bad in ("unknown", "error:TimeoutError"):
        detail = judge_detail(_sc("F1"), "外部风控变慢", lambda t, c, b=bad: b)
        a = _attempt_with("F1", correct=True, detail=detail)
        assert judge_asserts_all(a) is None, (
            f"裁判给出 {bad!r} 时必须返回 None（不可用），而不是 True/False"
        )

    # 逐项混合：一条对一条失败 → 整个不可用
    mixed = {"judge": {"A": "asserted", "B": "unknown"}}
    assert judge_asserts_all(_attempt_with("F8", correct=True, detail=mixed)) is None


def test_disagreement_is_surfaced_not_swallowed():
    """两把尺子分歧时必须**列出来**，而且要带原文供人复核。"""
    from eval.runner import Report, _judge_agreement

    # 关键词判定说"对"，裁判说"根本没提" → 分歧
    a1 = _attempt_with("F1", correct=True, detail={"judge": {"外部风控变慢": "absent"}})
    # 两把尺子都说"对" → 一致
    a2 = _attempt_with("F1", correct=True, detail={"judge": {"外部风控变慢": "asserted"}})

    agg = _judge_agreement([a1, a2])
    assert agg["n_judged"] == 2
    assert agg["judge_agree"] == 1
    ds = agg["judge_disagreements"]
    assert len(ds) == 1
    assert ds[0]["keyword_correct"] is True
    assert ds[0]["judge_asserts_all"] is False
    assert ds[0]["text"], "分歧条目必须带原文，否则人没法复核"


def test_judge_agreement_is_empty_without_a_judge_run():
    """没跑裁判 → 聚合结果里**不含**任何裁判字段，报告也不会打那一节。"""
    from eval.runner import Report, _judge_agreement

    assert _judge_agreement([_attempt_with("F1", correct=True)]) == {}
    agg = Report(model="m", mode="live", rounds=3, started_at="t",
                 attempts=[_attempt_with("F1", correct=True)]).agg()
    assert "n_judged" not in agg or not agg.get("n_judged")

def test_judge_cost_is_folded_back_into_the_attempt_cost():
    """裁判的成本**必须**加进 Attempt.cost_yuan。

    否则报告的"总计"会低估真实花费 —— 而"跑一次评测花了多少钱"
    正是本项目要如实给出的三个数字之一（#27 之后尤其不能糊）。
    """
    from eval.runner import judge_detail

    def fake_with_cost(text, cause):
        return "asserted", 0.0025        # 元组形式：结论 + 成本

    d = judge_detail(_sc("F8"), "任意文本", fake_with_cost)
    assert d["judge_cost_yuan"] == 0.005, (
        f"F8 要判两条，成本应累加为 0.005，实际 {d.get('judge_cost_yuan')}"
    )
    assert set(d["judge"]) == {"外部风控变慢", "内存泄漏"}


def test_judge_fn_may_return_a_bare_verdict_string():
    """两种返回形式都要认：只给结论（测试里常用）也照样工作。"""
    from eval.runner import judge_detail

    d = judge_detail(_sc("F1"), "外部风控变慢", lambda t, c: "asserted")
    assert d == {"judge": {"外部风控变慢": "asserted"}}, "没有成本时不该凭空造一个 0.0 出来"


def test_judge_cost_is_not_invented_when_there_is_no_judge():
    from eval.runner import judge_detail

    assert judge_detail(_sc("F1"), "文本", None) == {}

# ================================================================
# P8：**标准答案本身也要被检查**
# ================================================================
#
# 真实经历（D10 扩对抗样本时）：
#
#   我给对抗样本写了一条「外部风控变慢解释了延迟。这两件事应当分开看，
#   前者推不出后者。」，标注 expected=dismissed（想表达"把泄漏否掉了"）。
#   跑出来裁判判 absent —— 我第一反应是"**裁判错了一条**"。
#
#   查下去才发现：**那段话压根没点名内存泄漏**，裁判判 absent 是对的，
#   错的是我的标注。**差一点把一个假失败记进结论。**
#
# ⇒ 规矩：凡是"工具错了一条"的直觉，先怀疑**自己的标准答案**。
#   而这一条可以自动化：expected=dismissed 的样本，文本里**必须真的出现过**
#   那个原因的关键词 —— 否则它根本不构成"提到了但否掉了"。


def test_every_dismissed_fixture_actually_mentions_the_cause():
    """`expected=DISMISSED` 的样本，必须**真的提到过**那个原因。

    否则它测的不是"能不能识别降级"，而是"能不能看出这段话没提这件事"——
    那是另一回事，而且这个标注是错的（P8 就栽在这里）。
    """
    from eval.judge import ADVERSARIAL, DISMISSED, FIXTURES
    from eval.scenarios import Cause

    leak = Cause(name="内存泄漏", keyword_groups=(("内存", "memory", "泄漏", "leak"),))

    problems = []
    for fx in ADVERSARIAL:
        if fx["expected"] != DISMISSED:
            continue
        text = fx["text"]
        mentioned = any(kw.lower() in text.lower() for kw in leak.keyword_groups[0])
        if not mentioned:
            problems.append(f"{fx['id']}：标注 dismissed，但文本里没有出现任何"
                            f"「内存/泄漏/leak」字样 → 它其实是 absent，标注错了")

    assert not problems, (
        "以下对抗样本的标注与文本不符（**标准答案本身也要被检查**）：\n  "
        + "\n  ".join(problems)
    )


def test_the_dismissed_fixture_check_is_not_vacuous():
    """元测试：确认确实有 expected=dismissed 的样本被检查到，否则上面那条是空转。"""
    from eval.judge import ADVERSARIAL, DISMISSED

    n = sum(1 for fx in ADVERSARIAL if fx["expected"] == DISMISSED)
    assert n >= 10, f"只有 {n} 条 dismissed 样本 —— 太少，那条检查意义有限"

# ================================================================
# #29 trace 没被存档 —— 项目最强调可复现，却答不出「Agent 到底做了什么」
# ================================================================
#
# 真实缺口（D11 第一步查出来的）：
#   `Diagnosis.trace` / `Hypothesis.trace` 一直在收集（每一步调了什么工具、
#   参数是什么、返回了什么），但 **`results.json` 里没有它**。
#
# ⇒ 后果：拿着存档，你能看到"结论"和"几个数字"，
#   **却看不到任何一条原始证据**。而本项目的 FR-C 是"可复现"——
#   面试官拿到存档本该能自己追溯，而不是只能相信里面写好的布尔值。
#   （与 #18「存档漏 finished」、#19「存档截断结论」是同一个家族。）
#
# 设计取舍：trace 里是几 MB 的工具原始返回，
# **不能塞进 results.json**（那个文件是给人读的汇总）。
# 所以单独落到 `traces/` 下，汇总里只记路径。


def test_trace_is_saved_beside_the_report_not_inside_it(tmp_path, monkeypatch):
    """核心：轨迹要**单独落盘**，汇总里只留路径。

    两个断言缺一不可：
      · traces/ 下真的写出了文件（证据没丢）
      · results.json 里**没有**塞进原始 trace（汇总仍然可读）
    """
    import json as _json

    from eval.runner import Attempt, Report, load_report, save_report

    a = Attempt(
        fault_id="F1", round_no=1, correct=True, explanation="", root_cause="x",
        steps=3, tool_calls=2, cost_yuan=0.01, input_tokens=1, output_tokens=1,
        elapsed_s=1.0, finished=True, parse_ok=True,
        trace=[
            {"step": 1, "tool": "query_logs", "args": "{}", "result": "…" * 500},
            {"step": 2, "tool": "query_metrics", "args": "{}", "result": "…"},
        ],
    )
    report = Report(model="m", mode="live", rounds=1, started_at="t", attempts=[a])

    monkeypatch.setattr("eval.runner.RUNS_DIR", tmp_path)
    monkeypatch.setattr("eval.runner.ROOT", tmp_path)
    path = save_report(report)

    # ① 轨迹文件真的写出来了
    traces = list((path.parent / "traces").glob("*.json"))
    assert len(traces) == 1, f"轨迹没有落盘：{traces}"
    saved = _json.loads(traces[0].read_text(encoding="utf-8"))
    assert len(saved) == 2 and saved[0]["tool"] == "query_logs"

    # ② 汇总里只有路径，没有原始轨迹
    raw = _json.loads(path.read_text(encoding="utf-8"))
    att = raw["attempts"][0]
    assert "trace" not in att, "原始轨迹不许塞进 results.json（会把汇总撑爆）"
    assert att["trace_path"], "汇总里必须留下轨迹文件的路径"

    # ③ 路径能反查回文件
    reloaded = load_report(path)
    assert reloaded.attempts[0].trace_path == att["trace_path"]


def test_accounts_without_a_trace_still_load(tmp_path, monkeypatch):
    """老存档（没有 trace / trace_path 字段）必须仍然能被读回。"""
    from eval.runner import Attempt, Report, load_report, save_report

    a = Attempt(
        fault_id="F1", round_no=1, correct=True, explanation="", root_cause="x",
        steps=1, tool_calls=0, cost_yuan=0.0, input_tokens=0, output_tokens=0,
        elapsed_s=0.0, finished=True, parse_ok=True,
    )
    report = Report(model="m", mode="live", rounds=1, started_at="t", attempts=[a])
    monkeypatch.setattr("eval.runner.RUNS_DIR", tmp_path)
    monkeypatch.setattr("eval.runner.ROOT", tmp_path)
    path = save_report(report)

    assert not (path.parent / "traces").exists(), "没有轨迹时不该造一个空目录"
    assert load_report(path).attempts[0].trace_path == ""

def test_multi_attempt_carries_every_components_trace():
    """#29 补完：多 Agent 的**六个环节**（3 调查 + 3 质证）轨迹都要进存档。

    之前只有 baseline 那一路有轨迹 —— 而多 Agent 恰恰是最需要事后追溯的那个
    （#16 / #21 两次假阳性都是在**读原文**时发现的）。
    """
    from unittest.mock import patch

    from eval.runner import _run_multi_slice
    from eval.scenarios import SCENARIOS
    from rca.agents.coordinator import CrossExam, MultiAgentResult
    from rca.agents.specialist import Hypothesis

    class FakeRes(MultiAgentResult):
        pass

    fake = FakeRes()
    fake.hypotheses = [
        Hypothesis(role=r, name=r, claim="c", trace=[{"step": 1, "tool": "query_logs"}])
        for r in ("logs", "metrics", "change")
    ]
    fake.cross_exams = [
        CrossExam(role=r, original_claim="c", trace=[{"step": 1, "tool": "query_metrics"}])
        for r in ("logs", "metrics", "change")
    ]

    with patch("rca.agents.coordinator.diagnose_multi", lambda *a, **k: fake):
        attempt = _run_multi_slice(
            client=None, ctx=None, fid="F8", rnd=1, score=SCENARIOS["F8"],
            model=None, max_steps=22, cross_exam_steps=22,
        )

    phases = [t["phase"] for t in attempt.trace]
    assert phases.count("investigate") == 3, f"三个调查环节的轨迹都要在：{phases}"
    assert phases.count("cross_exam") == 3, f"三个质证环节的轨迹都要在：{phases}"
    roles = {t["role"] for t in attempt.trace}
    assert roles == {"logs", "metrics", "change"}
    assert all(t["steps"] for t in attempt.trace), "每个环节都要带自己的工具调用"


def test_multi_attempt_records_the_tokens_the_cost_was_computed_from():
    """**#71**：归档里"花了多少钱"必须伴随"钱是怎么花出来的"。

    现场：多 Agent 那一路把 `input_tokens` / `output_tokens` **写死成 0**
    （注释还写着"token 汇总见 detail"，而 `detail` 里**从来没有过 token**）。
    后果不是"少了个字段"，而是：**成本无法事后核对**。

    这一条正是 #70 卡住的地方 —— 余额少了 ¥6.76，而"我记录的 ¥3.41"
    到底是模型算错了、还是别的东西花了钱，**谁都查不下去**：
    token 是成本的原料，原料没留，账就永远对不上。

    ⇒ 这里钉两件事：① 存档里的 token 非零；② 它**恰好等于**各环节之和
      （不是拍了个数、也不是只统计了一部分环节）。
    """
    from unittest.mock import patch

    from eval.runner import _run_multi_slice
    from eval.scenarios import SCENARIOS
    from rca.agents.coordinator import CrossExam, MultiAgentResult, Verdict
    from rca.agents.specialist import Hypothesis

    fake = MultiAgentResult()
    fake.hypotheses = [
        Hypothesis(role=r, name=r, claim="c", input_tokens=1000, output_tokens=100)
        for r in ("logs", "metrics", "change")
    ]
    fake.cross_exams = [
        CrossExam(role=r, original_claim="c", input_tokens=2000, output_tokens=200)
        for r in ("logs", "metrics", "change")
    ]
    fake.verdict = Verdict(root_cause="c", input_tokens=3000, output_tokens=300)
    fake.guard_input_tokens = 500
    fake.guard_output_tokens = 50

    with patch("rca.agents.coordinator.diagnose_multi", lambda *a, **k: fake):
        attempt = _run_multi_slice(
            client=None, ctx=None, fid="F8", rnd=1, score=SCENARIOS["F8"],
            model=None, max_steps=22, cross_exam_steps=22,
        )

    # 3×1000（调查）+ 3×2000（质证）+ 3000（裁决）+ 500（护栏审查）= 12500
    assert attempt.input_tokens == 12_500, (
        f"存档里的输入 token 是 {attempt.input_tokens}，应当是各环节之和 12500 —— "
        "写死 0 或漏统计某一环节都会对不上（#71）"
    )
    assert attempt.output_tokens == 3 * 100 + 3 * 200 + 300 + 50, (
        f"输出 token 对不上：{attempt.output_tokens}"
    )
    assert attempt.input_tokens > 0 and attempt.output_tokens > 0, "token 必须是真实数字，不许再写 0"

# ================================================================
# #30 跨计价时段比较成本 → 假的"成本翻倍"
# ================================================================
#
# 真实错误（**我自己犯的**）：
#   multi 同配置重跑，成本从 ¥0.0674 涨到 ¥0.1414（2.10×），
#   我把它当成"代码改动导致变贵"写进了文档。
#
#   查下去才发现：**两次运行的计价时段不同** ——
#     08:00 那次是**谷时**（0.02/1.0/4.0），09:42 那次是**峰时**（0.04/2.0/8.0），
#     峰时单价整整是谷时的 **2 倍**。0.1414 / 0.0674 = 2.10 ≈ 2。
#
#   ⇒ 那个"暴涨"几乎完全是**时段**造成的，跟代码无关。
#     正确的同口径比较是 08:00 的 multi（¥0.0674）对 08:35 的 baseline（¥0.0172）
#     = **3.9×**，而不是我写下的 8.2×。
#
# 这属于本项目一直在抓的那一类：**数字算出来了，但两个数字的口径不同。**
# 机制上的封堵：**每次运行把计价时段记进存档并打出来**。


def test_report_records_the_pricing_tier():
    """运行必须把计价时段记进 results.json 并打印出来。

    不记的话，"跨时段比较成本"这个错误**无法被发现** ——
    因为两个数字看起来只是"不一样"，看不出是单价不同。
    """
    from eval.runner import Report

    r = Report(model="m", mode="live", rounds=1, started_at="t", pricing_tier="peak")
    assert r.to_dict()["pricing_tier"] == "peak"


def test_report_records_the_run_configuration(tmp_path):
    """#66：归档必须**自解释** —— 决定"准确率"的运行参数要写进 results.json。

    历史：#13 证明 `max_steps` 会改变"准确率"（8 → 0%、14 → 100%），
    而 2026-09-27 发现**归档里根本没有这个字段**（也没有 `cross_exam_steps` / `guard`）⇒
    拿到一份 results.json 无法自证"它是在什么配置下跑的"，
    也就无法判断两次运行**能不能比**（这正是 D12/D15 那些数字的前提）。

    两个方向都要守：**新归档必须记**、**老归档必须仍能读回**（不许因为缺字段整份作废）。
    """
    import json

    from eval.runner import Report, load_report

    r = Report(model="m", mode="live", rounds=1, started_at="t",
               max_steps=22, cross_exam_steps=22, guard=True)
    d = r.to_dict()
    assert d["max_steps"] == 22 and d["cross_exam_steps"] == 22 and d["guard"] is True, \
        f"运行参数必须进归档，实际：{ {k: d.get(k) for k in ('max_steps', 'cross_exam_steps', 'guard')} }"

    # 老归档（D12/D15 那些）没有这三个字段 ⇒ 必须仍能读回，且默认值明确是"没记录"
    old = tmp_path / "results.json"
    old.write_text(json.dumps({
        "model": "m", "mode": "live", "rounds": 1, "started_at": "t",
        "pricing_tier": "off_peak", "aggregate": {}, "attempts": [],
    }, ensure_ascii=False), encoding="utf-8")
    back = load_report(old)
    assert back.max_steps == 0, "0 = 老归档没记录（不是'0 步'）"
    assert back.guard is False


def test_default_step_budget_matches_the_documented_protocol():
    """#66：`--max-steps` 的**默认值**必须等于文档里的协议值，否则陌生人拿到的是另一个配置。

    2026-09-27 实测发现同一个数有**三份**：代码默认 14、文档协议 22、
    `.env.example` 里还写着 20（而没有任何代码读它）。#13 已经证明这个数会改变准确率 ⇒
    "默认值跟协议不一致"意味着：照文档跑出来的数字与归档里的数字**不可比**。
    """
    from eval.runner import DEFAULT_MAX_STEPS

    assert DEFAULT_MAX_STEPS == 22, (
        f"默认步数预算是 {DEFAULT_MAX_STEPS}，而 docs/00 的协议是 22"
        f"（#22：枚举任务实测最慢需 14 步 ⇒ 留余量用 22）"
    )


def test_a_run_with_zero_attempts_is_not_a_success(capsys):
    """#69：**一次都没测到**不许报成功。

    实测（2026-09-27，做 D19 的负对照时撞上）：`--mode replay --batch <不存在的批次>`
    会逐条打印"失败（跳过）"、什么都不写、然后 **exit 0** ⇒
    CI / 脚本看到的是"成功"，而实际上一次都没测到。

    两条方向都要守：
      · 0 次尝试 ⇒ 非零（否则就是"没测到伪装成测到了"）；
      · 有尝试（哪怕中途放弃、哪怕全错）⇒ 0（那些尝试是花了钱的，照常存档）。
    """
    from eval.runner import Report, exit_code_for

    empty = Report(model="m", mode="replay", rounds=1, started_at="t")
    assert exit_code_for(empty) == 2, "一次尝试都没有 ⇒ 必须是非零退出码"
    assert "一次尝试都没有产出" in capsys.readouterr().out, "还要说清为什么（而不是静默非零）"

    has_attempts = Report(model="m", mode="live", rounds=1, started_at="t",
                          attempts=[_attempt_with("F1", correct=False)])
    assert exit_code_for(has_attempts) == 0, "有尝试就不该说'没跑'（哪怕答案是错的）"

    has_attempts.aborted_reason = "402 Insufficient Balance"
    assert exit_code_for(has_attempts) == 0, "中途放弃但已有尝试 ⇒ 仍然照常存档（#56 的教训）"


def test_report_prints_the_pricing_tier(capsys):
    from eval.runner import Report, print_report

    r = Report(
        model="m", mode="live", rounds=3, started_at="t",
        pricing_tier="peak",
        attempts=[_attempt_with("F1", correct=True) for _ in range(3)],
    )
    print_report(r)
    out = capsys.readouterr().out

    assert "峰时" in out, "必须打出来，否则读者无从知道单价是普通的 2 倍"
    assert "只与同时段的运行比较成本" in out, "还要明确给出比较纪律"


def test_off_peak_runs_are_labelled_too(capsys):
    """正向对照：谷时也要标明 —— 只说峰时会让人以为"没标=谷时"。"""
    from eval.runner import Report, print_report

    r = Report(
        model="m", mode="live", rounds=3, started_at="t", pricing_tier="off_peak",
        attempts=[_attempt_with("F1", correct=True) for _ in range(3)],
    )
    print_report(r)
    assert "谷时" in capsys.readouterr().out

# ================================================================
# #31 计价模型漏掉法定节假日 → 成本虚高一倍，并被我误读成"成本涨了"
# ================================================================
#
# 真实事件（**我自己犯的，而且被用户一句话问出来的**）：
#
#   multi 同配置重跑，报告成本从 ¥0.0674 涨到 ¥0.1414（**2.10×**）。
#   我把原因归给"计价时段不同"（08:00 谷时 vs 09:42 峰时），并**写进了文档**。
#
#   用户反问：「今天不是节假日吗，真的有峰时问题吗？」
#   查证（2026-09-25 是中秋，正在中秋+国庆的十天半价窗口里）：
#     DeepSeek 官方规则 —— **调休上班的周末、中国法定节假日全天均按空闲时段计费**。
#   ⇒ 09:42 的真实计费**仍是谷时**，而我的 `is_peak_hour()` 按峰时算，
#     **把成本算高了一倍**；然后我又把这个虚高的数字当成了"成本真的涨了"。
#
#   ⇒ 代码注释里原本写着「不处理节假日，误差方向是高估成本，属于保守估计，可以接受」。
#     **那个判断是错的**：高估本身无害，但**一旦拿它去比较两次运行的增减**，
#     它就会变成一个**假的成本变化**。


def test_holidays_are_billed_off_peak():
    """法定节假日**全天**按空闲时段计费（官方规则，2026-09 查证）。

    漏掉这一条不只是"算贵了一点" —— 它会让假日期间的成本变成峰时价的 2 倍，
    而那个数字一旦被用来比较增减，就会得出**假的成本变化**（#31）。
    """
    from datetime import datetime

    from rca.llm.provider import is_peak_hour

    # 中秋（2026-09-25 周五）—— 峰时段内也应是空闲
    assert is_peak_hour(datetime(2026, 9, 25, 10, 0)) is False
    assert is_peak_hour(datetime(2026, 9, 25, 15, 0)) is False
    # 国庆窗口内
    assert is_peak_hour(datetime(2026, 10, 7, 10, 0)) is False


def test_normal_workdays_are_still_peak():
    """正向对照：**普通工作日**必须仍然是峰时。

    否则"假日算谷时"可能退化成"永远算谷时"，成本会被系统性低估 —— 那是反向的错。
    """
    from datetime import datetime

    from rca.llm.provider import is_peak_hour

    assert is_peak_hour(datetime(2026, 10, 9, 10, 0)) is True
    assert is_peak_hour(datetime(2026, 10, 9, 15, 0)) is True
    # 午休与下班后仍是空闲
    assert is_peak_hour(datetime(2026, 10, 9, 13, 0)) is False
    assert is_peak_hour(datetime(2026, 10, 9, 20, 0)) is False
    # 周末
    assert is_peak_hour(datetime(2026, 10, 10, 10, 0)) is False


def test_the_holiday_table_is_not_empty_and_is_dated():
    """元测试：节假日表不能是空的，否则上一条测的是空气；
    而且它**必须能被一眼看出是给哪一年用的**（否则明年会静默失效）。"""
    from rca.llm.provider import HOLIDAYS_2026

    assert len(HOLIDAYS_2026) >= 7, f"节假日表太小：{sorted(HOLIDAYS_2026)}"
    assert all(d.startswith("2026-") for d in HOLIDAYS_2026), (
        "表名里写了年份，条目也必须是同一年 —— 否则跨年时会静默算错"
    )

# ================================================================
# #34 计价时段存了却没接上 —— `--from-json` 重载时警告会消失
# ================================================================
#
# 与 #18「CrossExam 存档漏 finished」同族：**字段存了，但没接线**。
# 具体危害：`pricing_tier` 的作用就是提醒"只与同时段比较成本"，
# 而 `--from-json`（重新聚合已有结果）**正是我做比较时走的路径** ——
# 不恢复它，那条警告在最需要的时候恰好消失。


def test_pricing_tier_survives_a_save_load_round_trip(tmp_path, monkeypatch):
    """`--from-json` 必须把计价时段也恢复回来。"""
    from eval.runner import Report, load_report, save_report

    r = Report(model="m", mode="live", rounds=1, started_at="t",
               pricing_tier="peak", attempts=[_attempt_with("F1", correct=True)])
    monkeypatch.setattr("eval.runner.RUNS_DIR", tmp_path)
    monkeypatch.setattr("eval.runner.ROOT", tmp_path)
    path = save_report(r)

    assert load_report(path).pricing_tier == "peak", (
        "重载后计价时段丢了 —— 那条「只与同时段比较成本」的警告就会消失"
    )


def test_old_archives_without_the_tier_still_load(tmp_path, monkeypatch):
    """老存档没有这个字段 → 回落为空串，**不许编一个值出来**。"""
    from eval.runner import Report, load_report, save_report

    r = Report(model="m", mode="live", rounds=1, started_at="t",
               attempts=[_attempt_with("F1", correct=True)])
    monkeypatch.setattr("eval.runner.RUNS_DIR", tmp_path)
    monkeypatch.setattr("eval.runner.ROOT", tmp_path)
    assert load_report(save_report(r)).pricing_tier == ""


# ================================================================
# #35 节假日表里混进了**我猜的**日期 → 成本会被低估
# ================================================================
#
# 第一版我按"中秋国庆十天半价窗口"这个说法，把 09-26~09-30 与 10-08 也写进了节假日表。
# **那是推的，不是核实的。** 而猜错的方向很糟：
#   · 把工作日当假日 → 成本被**低估**（本该峰时却按谷时算）
#   · 把假日当工作日 → 成本被**高估**
# "高估"至少保守；"低估"会让成本看起来比实际更好看。
# ⇒ 原则：**只列能佐证的日期；不确定的宁可高估。**


def test_holiday_table_contains_only_verified_dates():
    """只允许出现**已核实**的中秋与国庆日期。

    这条用例的写法很直白：把允许的日期全部写死。
    以后要加日期，就必须先核实、再改这里 —— **改这里时你会被迫想一遍**。
    """
    from rca.llm.provider import HOLIDAYS_2026

    verified = {"2026-09-25"} | {f"2026-10-{d:02d}" for d in range(1, 8)}
    extra = sorted(set(HOLIDAYS_2026) - verified)
    assert not extra, (
        f"节假日表里出现了未经核实的日期：{extra}\n"
        "请以官方放假通知为准；在核实之前，宁可**高估**成本（当成工作日）"
    )
    missing = sorted(verified - set(HOLIDAYS_2026))
    assert not missing, f"已核实的假日缺了：{missing}"


def test_guessed_dates_are_not_counted_as_holidays():
    """我先前猜过的那几天，现在**必须**按工作日算（宁可高估）。"""
    from datetime import datetime

    from rca.llm.provider import is_peak_hour

    # 2026-09-28 是周一：若被当成假日就会低估成本
    assert is_peak_hour(datetime(2026, 9, 28, 10, 0)) is True, (
        "09-28 未经核实，必须按工作日（峰时）算 —— 否则成本被低估"
    )


# --------------------------------------------------------------------------- #
# P7 / #43：裁判审计脚本的两条不变量
# --------------------------------------------------------------------------- #
def test_keyword_matching_survives_english_inflection():
    """#48：`retry` 必须能命中 `retries`，`leak` 能命中 `leaks`。

    现场（D15 回顾时实测）：一段**点名了参数名**的答案 ——

        配置变更放大了故障：05:45:32 inventory.W_INVENTORY_DOWNSTREAM_RETRIES 由 1 改为 5，
        使 127 个失败订单产生 2153 次 payment 调用……把局部失败放大为请求风暴。

    —— 那是**标准答案本身**，却因为在词形上不满足 `retry` 而被判成"从没提过重试"：
    `retries` **既不包含** `retry`（是 `retri` + `es`）、**也不以它开头**。
    实测它在 F4 这一格上把**两侧**各判错一次（multi 一轮、baseline 一轮）。
    """
    from eval.scenarios import SCENARIOS, Cause, keyword_verdict

    cause = Cause("F4", SCENARIOS["F4"].keyword_groups)
    answer = (
        "配置变更放大了故障：05:45:32 inventory.W_INVENTORY_DOWNSTREAM_RETRIES 由 1 改为 5，"
        "使 127 个失败订单产生 2153 次 payment 调用，把局部失败放大为请求风暴。"
    )

    assert keyword_verdict(answer, cause) == "asserted", (
        "点名了参数名的正确答案又被判成没提过重试 —— 词形归一出问题了"
    )


def test_keyword_matching_unit_cases_for_inflection_and_negation():
    """配套的单元级对照：变形要认，**否掉的不许认**，没提的仍不认。"""
    from eval.scenarios import _kw_hit

    # 英文词形
    assert _kw_hit("service retries downstream calls", "retry") is True
    assert _kw_hit("the client is retrying now", "retry") is True
    assert _kw_hit("order leaks memory", "leak") is True
    assert _kw_hit("it leaked 2mb per request", "leak") is True
    # 中文仍是子串（没有词边界）
    assert _kw_hit("重试次数从 1 改成 5", "重试") is True
    # 反向对照：完全没提就不许认（否则就是"什么都能命中"）
    assert _kw_hit("payment 返回 502，仅此而已", "retry") is False
    assert _kw_hit("延迟升高", "库存") is False


def test_a_dismissed_retry_claim_is_still_not_counted_as_asserted():
    """★ 加词形归一**不能**把"否掉"也一起认了。

    这是最容易在修匹配器时被顺手放大的方向：只要把"命中"放宽，
    就可能让"重试不是根因"这种句子重新变成"主张了重试"（#16/#21 的老病）。
    """
    from eval.scenarios import SCENARIOS, Cause, keyword_verdict

    cause = Cause("F4", SCENARIOS["F4"].keyword_groups)
    denied = "inventory 的下游重试次数变更**不是根因**，它只是放大了既有故障。"

    assert keyword_verdict(denied, cause) != "asserted", (
        "把否掉的表述算成主张 —— 这正是 #16/#21 两次假阳性的机制"
    )


def test_unsupported_cause_claims_counts_listed_but_unsupported_causes():
    """结论精度诊断：答案**自己列出**的原因里，有几条对不上场景真值。

    ⚠️ 它**不是** #44 的检测器 —— 我试过两版都失败了，理由写在
       `eval/runner.py::unsupported_cause_claims` 的 docstring 里：
       第一版太钝（multi 11 vs baseline 9，把支撑性观察也算上），
       第二版靠偶然命中（真实文本里有"水位"二字）而我的合成对照用例当场戳穿了它。
       ⇒ 按 #32 的先例，**不采用会误导的指标**，#44 保持 🟡。
    """
    from eval.runner import unsupported_cause_claims

    listed = _attempt(
        fault_id="F4",
        root_cause=(
            "inventory 的下游重试次数被从 1 改为 5（W_INVENTORY_DOWNSTREAM_RETRIES），"
            "把局部失败放大成请求风暴。；"
            "inventory 的 stock_level 三个 SKU 均处于约 99.8 万的极高水平"
            "（{SKU-001}=998092、{SKU-002}=997912），这是一个独立异常。"
        ),
    )
    claims = unsupported_cause_claims(listed)

    assert len(claims) == 1, f"应当只数出没对上真值的那一条，实际 {claims}"
    assert "stock_level" in claims[0]


def test_unsupported_cause_claims_is_quiet_on_a_single_correct_cause():
    """干净答案必须是 0 条 —— 否则它就是个恒亮的噪声源。"""
    from eval.runner import unsupported_cause_claims

    clean = _attempt(
        fault_id="F4",
        root_cause=(
            "inventory 的下游重试次数被从 1 改为 5（W_INVENTORY_DOWNSTREAM_RETRIES），"
            "把局部失败放大成请求风暴，是本次故障的根本原因。"
        ),
    )

    assert unsupported_cause_claims(clean) == []


def test_unsupported_cause_claims_is_a_precision_diagnostic_not_a_discriminator() -> None:
    """钉住它的**性质**：两侧差不多 ⇒ 它**不区分好坏**，别拿它当"谁更强"的证据。

    实测：multi 11 条 / baseline 9 条。这个数字本身有用（说明"结论里夹带了别的东西"），
    但把它当成"多 Agent 更差"的证据就是**用错工具** —— 这条用例把这件事固化下来。
    """
    import dataclasses
    import json
    from pathlib import Path

    from eval.runner import Attempt, unsupported_cause_claims

    root = Path(__file__).resolve().parent.parent
    fields = {f.name for f in dataclasses.fields(Attempt)}

    def total(name: str) -> int:
        p = root / "runs" / "_eval" / name / "results.json"
        if not p.exists():
            pytest.skip("本机没有那份存档（runs/ 被 gitignore）")
        data = json.loads(p.read_text(encoding="utf-8"))
        n = 0
        for a in data["attempts"]:
            n += len(unsupported_cause_claims(Attempt(**{k: v for k, v in a.items() if k in fields})))
        return n

    multi = total("multi-20260925-131953")
    baseline = total("baseline-20260925-085629")

    assert multi > 0 and baseline > 0, (
        f"两侧都该有（multi={multi} baseline={baseline}）—— 若一侧为 0，我先前的实测结论要更新"
    )
    assert abs(multi - baseline) <= 6, (
        f"两侧差距突然拉大（multi={multi} baseline={baseline}）—— 那就该重新审这个诊断量的含义"
    )


def test_small_sample_warning_fires_for_three_rounds():
    """#46：单场景样本太小时，**报告必须自己说出来**。

    现场：D15 主跑的"multi 低 9.5 个百分点"全部来自 F4 一个 **n=3** 的格子，
    而同配置重跑 baseline 在 F4 上自己就从 3/3 掉到 2/3。
    3 轮的单场景准确率只能取 0/33/67/100 —— 这种分辨率撑不住"9.5 个百分点"的精度。

    ⇒ 与其靠我记得加一句"样本小"，不如让工具打印出来。
    """
    from eval.runner import small_sample_warning

    w = small_sample_warning(3, 7)

    assert w is not None, "3 轮居然不告警 —— 那条结论就是靠人记着"
    assert "3 轮" in w
    assert "不能比幅度" in w, "告警必须说清「能看什么、不能看什么」"
    assert "33" in w, "要把档位间隔写出来，读者才知道分辨率有多粗"

def test_small_sample_warning_is_silent_for_a_big_enough_sample():
    from eval.runner import SMALL_SAMPLE_MIN_ROUNDS, small_sample_warning

    assert small_sample_warning(SMALL_SAMPLE_MIN_ROUNDS, 7) is None
    assert small_sample_warning(10, 7) is None
    assert small_sample_warning(1, 7) is not None, "1 轮是最极端的小样本，必须告警"


def test_judge_audit_reuses_the_single_cause_label_table():
    """#43：原因标签**只能有一份**（`eval/scenarios.py::CAUSE_LABELS`）。

    `eval/judge_audit.py` 曾经自己抄了一份，而 `scenarios.py` 那段注释恰好写着：
    "都从这里取。各自写一份的话，两边迟早走散 ——
     而'两份判定不一致'这种 bug 极难发现（本项目已经栽过一次）。"

    ⇒ 断言**是同一个对象**，不是"内容暂时相同的副本"：副本会随时间走散。
    """
    from eval import judge_audit
    from eval.scenarios import CAUSE_LABELS

    assert judge_audit.CAUSE_LABELS is CAUSE_LABELS, "抄了一份副本 —— 两边迟早走散"
    assert not hasattr(judge_audit, "CAUSE_LABEL"), "旧的副本又回来了"


def test_judge_audit_keyword_side_never_gives_up_on_a_known_scenario():
    """P7：**能算就必须算出来**，不许用 `n/a` 冒充"算过了"。"""
    from eval import judge_audit
    from eval.scenarios import CAUSE_LABELS, SCENARIOS

    text = "根因是外部风控变慢：order 侧延迟升高，而 inventory 与 payment 自身耗时正常。"

    checked = 0
    for fid in SCENARIOS:
        if fid not in CAUSE_LABELS:
            continue
        checked += 1
        got = judge_audit.keyword_side(fid, text)
        assert got != judge_audit.NA, f"{fid} 的关键词判定退化成 n/a 了"
        assert got in {"asserted", "dismissed", "absent"}
    assert checked >= 7, f"只检查了 {checked} 个场景，场景表可能变了"


def test_judge_audit_excludes_unscorable_cases_from_the_rate():
    """P7 的核心：**`n/a` 不许进分母** ——「没算出来」和「判得不一致」是两件事。

    现场：审计脚本把算不出来的行输出成 `n/a`，又把它计进「不一致」，
    于是得到一份误导性的一致率（看着像裁判与关键词分歧，其实只是没算出来）。
    """
    from eval import judge_audit

    cases = [
        {"kw": "asserted", "verdicts": ["asserted"]},          # 一致
        {"kw": "asserted", "verdicts": ["absent"]},            # 真分歧
        {"kw": judge_audit.NA, "verdicts": ["asserted"]},      # 算不出来 → 必须排除
        {"kw": judge_audit.NA, "verdicts": ["absent"]},        # 同上
    ]
    agree, comparable, na_n = judge_audit.agreement_summary(cases)

    assert (agree, comparable, na_n) == (1, 2, 2), (
        f"分母里混进了算不出来的行：agree={agree} comparable={comparable} na={na_n}"
    )
