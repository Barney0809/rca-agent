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
