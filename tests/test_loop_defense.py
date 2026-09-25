"""
D9 验收：打转（loop）防御。

============================ 为什么需要它 ============================

实测证据（2026-09-25，任务改成"列出所有异常"之后）：

    [F1 第1轮] ❌ 步数=14 工具=87 成本=¥0.0405 31.9s
          结论：（空）

**14 步里调了 87 次工具，最后一句话都没得出。**
而报告里只能看到一个"步数 14、结论为空" ——
**分不清它是在深挖，还是在原地打转。**

这两件事的修法**完全相反**：
    在深挖  → 该加预算
    在打转  → 加预算只会让它更贵地打转，得改提示词或工具返回的内容

============================ 做法 ============================

工具调用只有 `ToolBox.call` 一个出口（baseline 与三个专职 Agent 共用），
所以把检测放在那里，**所有 Agent 自动具备**，不需要各自实现一遍。

三层：
    1. **统计**：参数完全相同的调用计为 repeat，不同（工具+参数）组合计为 distinct
    2. **提醒模型**：第 3 次重复起在工具返回里追加警告；
       第 5 次起明确要求它收手（"结果不会有任何变化"）
    3. **分类**：报告里把"撞预算"与"疑似打转"分开，指向不同的修法

⚠️ 签名用**解析后的参数**而不是原始字符串：
   模型经常把同一个查询用不同的空格/键序再发一遍
   （`{"level":"ERROR"}` vs `{ "level" : "ERROR" }`）。
   按原始文本比会漏掉这些 —— 而它们正是"打转"的典型样子。
"""

from __future__ import annotations

import pytest

from eval.runner import Attempt, stop_reason
from rca.telemetry.models import ReducedView
from rca.tools import RunContext, ToolBox


@pytest.fixture
def ctx() -> RunContext:
    return RunContext(
        run_id="t-loop",
        log_view=ReducedView(window_start=None, window_end=None, total_lines=0),
        metrics={},
        changes=[],
        scenario={},
    )


def test_repeating_the_same_call_is_counted(ctx: RunContext) -> None:
    """第 1 次不算重复，之后每一次都算。"""
    box = ToolBox(ctx)

    box.call("query_logs", '{"level": "ERROR"}')
    assert ctx.repeat_calls == 0, "第一次调用不该算重复"

    box.call("query_logs", '{"level": "ERROR"}')
    box.call("query_logs", '{"level": "ERROR"}')

    assert ctx.repeat_calls == 2, f"应记 2 次重复，实际 {ctx.repeat_calls}"
    assert ctx.distinct_calls == 1, "三次调用是同一个签名，distinct 应为 1"


def test_different_arguments_are_not_repeats(ctx: RunContext) -> None:
    """换条件就是换证据，不算打转。"""
    box = ToolBox(ctx)

    box.call("query_logs", '{"level": "ERROR"}')
    box.call("query_logs", '{"level": "WARNING"}')
    box.call("query_metrics", '{"service": "order"}')

    assert ctx.repeat_calls == 0
    assert ctx.distinct_calls == 3


def test_signature_ignores_cosmetic_differences(ctx: RunContext) -> None:
    """空格 / 键序不同，但语义相同的调用必须算**同一次**。

    这不是吹毛求疵：模型真的会这么干（把同一个查询换个写法再问一遍），
    而按原始文本比对会把它当成"新证据"，于是打转被记成"在扩证据面"。
    """
    box = ToolBox(ctx)

    box.call("query_logs", '{"level": "ERROR", "service": "order"}')
    box.call("query_logs", '{"service":"order","level":"ERROR"}')
    box.call("query_logs", '{ "level" : "ERROR" , "service" : "order" }')

    assert ctx.distinct_calls == 1, "三种写法是同一个查询"
    assert ctx.repeat_calls == 2


def test_model_gets_a_warning_on_the_third_repeat(ctx: RunContext) -> None:
    """第 3 次起，工具返回里要**明确告诉模型**它在重复。

    只统计不提醒是没用的 —— 模型看不到统计，只会继续转。
    """
    box = ToolBox(ctx)

    out1 = box.call("query_logs", '{"level": "ERROR"}')
    out3 = ""
    for _ in range(2):
        out3 = box.call("query_logs", '{"level": "ERROR"}')

    assert "重复" not in out1, "第一次不该打扰模型"
    assert "重复" in out3, f"第 3 次该提醒，实际返回：{out3[-200:]}"
    assert "换一个查询条件" in out3, "提醒必须给出可执行的下一步，而不只是抱怨"


def test_warning_escalates_and_tells_it_to_stop(ctx: RunContext) -> None:
    """第 5 次起必须要求它收手，而不是继续礼貌提醒。"""
    box = ToolBox(ctx)

    out = ""
    for _ in range(5):
        out = box.call("query_logs", '{"level": "ERROR"}')

    assert "立刻" in out, f"第 5 次应该要求立刻收手，实际：{out[-200:]}"
    assert "不会" in out or "不变" in out, "要说明继续调用是纯浪费"


def test_counters_start_fresh_per_forked_context(ctx: RunContext) -> None:
    """fork 出来的上下文必须**各记各的**。

    三个专职 Agent 并发跑，共享计数器会让步数没法归因到具体角色 ——
    而且一个 Agent 的打转会被算到另一个头上。
    """
    parent = ToolBox(ctx)
    parent.call("query_logs", '{"level": "ERROR"}')
    parent.call("query_logs", '{"level": "ERROR"}')   # 第 2 次 → 1 次重复
    assert ctx.repeat_calls == 1

    child_ctx = ctx.fork()
    assert child_ctx.repeat_calls == 0, "子上下文必须从 0 开始"
    assert child_ctx.distinct_calls == 0
    assert child_ctx.seen_signatures == {}, "「已经问过什么」也不能被继承"

    ToolBox(child_ctx).call("query_logs", '{"level": "ERROR"}')
    assert child_ctx.repeat_calls == 0, "子上下文里这是第一次调用"
    assert ctx.repeat_calls == 1, "父上下文的计数不能被影响"


def test_two_boxes_over_the_same_context_still_detect_repeats(ctx: RunContext) -> None:
    """⚠️ 关键回归：**换一个 ToolBox 不能重置打转检测**。

    这条用例来自一个真实的脆弱性：如果把"已经问过什么"的状态放在 ToolBox 上，
    那么"同一个 ctx 上建了两个 box"就会让检测**静默失效** ——
    而 `repeat_calls` 仍然是 0，看起来就像"没有打转"。

    **一个看起来正常的 0 比一个明显的错误更危险。**

    这种重构很容易发生：有人为了"每步都用最新的上下文"把
    `box = ToolBox(ctx)` 挪进循环里 —— 代码照样跑，检测悄悄死掉。
    """
    first = ToolBox(ctx)
    first.call("query_logs", '{"level": "ERROR"}')

    # 另建一个 box（模拟"每步新建"的那种重构）
    second = ToolBox(ctx)
    out = second.call("query_logs", '{"level": "ERROR"}')
    out = second.call("query_logs", '{"level": "ERROR"}')

    assert ctx.repeat_calls == 2, (
        f"换了 box 之后重复仍要被记到 ctx 上，实际 {ctx.repeat_calls}"
    )
    assert "重复" in out, "第三个 box 之后的调用仍然要提醒模型"


# ================================================================
# 停止原因：把"撞预算"和"在打转"分开
# ================================================================


def _attempt(*, finished: bool, tool_calls: int, repeat_calls: int) -> Attempt:
    return Attempt(
        fault_id="F1", round_no=1, correct=False, explanation="", root_cause="",
        steps=14, tool_calls=tool_calls, cost_yuan=0.0, input_tokens=0,
        output_tokens=0, elapsed_s=0.0, finished=finished, parse_ok=False,
        repeat_calls=repeat_calls,
    )


def test_stop_reason_distinguishes_looping_from_budget() -> None:
    """核心：两种"没跑完"必须给出**不同的名字**，因为它们该修的东西不同。"""
    assert stop_reason(_attempt(finished=True, tool_calls=20, repeat_calls=0)) == "converged"

    # 工具调用不多 → 更像是在深挖，只是预算不够
    assert stop_reason(_attempt(finished=False, tool_calls=8, repeat_calls=0)) == "step_limit"

    # 重复占比过半 → 在打转
    assert (
        stop_reason(_attempt(finished=False, tool_calls=87, repeat_calls=60))
        == "step_limit_looping"
    )


def test_stop_reason_does_not_cry_loop_on_a_few_calls() -> None:
    """调用次数很少时，即使重复占比高也不该判成打转。

    防止"提前收敛"被误诊成"打转" —— 那会把注意力引向错误的修法。
    """
    assert stop_reason(_attempt(finished=False, tool_calls=4, repeat_calls=4)) == "step_limit"


def test_stop_reason_is_computed_from_an_archive_not_a_live_run() -> None:
    """必须是**纯函数**：老存档（没有 repeat_calls 字段）也要能算。

    与两轴判定同一个原则 —— 能离线重算的东西，就不要绑定在"下次重跑"上。
    """
    old = Attempt(
        fault_id="F1", round_no=1, correct=True, explanation="", root_cause="x",
        steps=6, tool_calls=23, cost_yuan=0.01, input_tokens=0, output_tokens=0,
        elapsed_s=8.0, finished=True, parse_ok=True,
    )
    assert old.repeat_calls == 0, "老存档缺字段时应回落到 0"
    assert stop_reason(old) == "converged"
