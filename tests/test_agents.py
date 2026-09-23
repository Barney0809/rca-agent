"""
D6 验收：三个专职 Agent 的职责边界与信息隔离。

需求 FR-1.3：**三个专职 Agent，每个只能访问自己的数据源。**

============================ 为什么隔离是"必须"而不是"最好" ============================

隔离有一个反面：如果每个 Agent 都能看到全部数据，那"多 Agent"就只是
**同一份数据问三遍** —— 成本三倍、信息零增益。

只有让每个 Agent **客观上存在盲区**，协作才产生真实价值：
    每个 Agent 的结论都是**不完整的**，所以必须互相质证。

所以本文件的用例盯住的是"**边界真的存在**"，而不只是"prompt 里写了"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rca.agents.roles import ALL_ROLES, CHANGE_AGENT, LOGS_AGENT, METRICS_AGENT
from rca.telemetry.models import ReducedView
from rca.tools import RestrictedToolBox, RunContext, ToolBox


@pytest.fixture
def empty_ctx() -> RunContext:
    """一个不需要真实数据的上下文（本文件只测工具边界，不测内容）。"""
    return RunContext(
        run_id="t-test",
        log_view=ReducedView(window_start=None, window_end=None, total_lines=0),
        metrics={},
        changes=[],
        scenario={},
    )


# ================================================================
# 角色定义本身的正确性
# ================================================================

def test_each_role_has_exactly_one_tool():
    """每个角色只能有一个工具 —— 这是"专职"的定义。"""
    for role in ALL_ROLES:
        assert role.tool, f"{role.key} 没有声明工具"
        assert role.duty, f"{role.key} 没有声明职责"
        assert len(role.system_prompt) > 200, f"{role.key} 的 prompt 太短，可能没写清楚职责"


def test_roles_cover_the_three_data_sources_exactly():
    """三个角色的工具合起来，恰好覆盖三路数据源 —— 不多也不少。

    **多了**：说明有 Agent 越界（那隔离就失效了）
    **少了**：说明有一路数据没人看（那结论必然片面）
    """
    tools = sorted(r.tool for r in ALL_ROLES)
    assert tools == ["get_changes", "query_logs", "query_metrics"], (
        f"三个角色的工具应当恰好是三路数据源各一个，实际：{tools}"
    )


def test_all_roles_require_needs_from_others():
    """每个角色的输出契约里都必须有 needs_from_others。

    隔离会让每个 Agent 证据不全；**如果不强制它说出自己缺什么**，
    隔离就只是把能力削掉了，而不是制造协作。
    """
    for role in ALL_ROLES:
        assert "needs_from_others" in role.system_prompt, (
            f"{role.key} 的输出契约里缺 needs_from_others —— "
            f"那它只会给一个不完整的结论，而不会说「我需要什么」"
        )


# ================================================================
# 隔离在代码层真的生效
# ================================================================

def test_specs_only_expose_the_allowed_tool(empty_ctx: RunContext):
    """**模型根本看不到别的工具** —— 这是第一道防线。"""
    box = RestrictedToolBox(empty_ctx, frozenset({"query_metrics"}), role="metrics")
    names = [s["function"]["name"] for s in box.specs()]
    assert names == ["query_metrics"], f"暴露了不该暴露的工具：{names}"


@pytest.mark.parametrize(
    "role,forbidden",
    [
        (METRICS_AGENT, ["query_logs", "get_changes"]),
        (LOGS_AGENT, ["query_metrics", "get_changes"]),
        (CHANGE_AGENT, ["query_logs", "query_metrics"]),
    ],
)
def test_cross_role_calls_are_denied(empty_ctx: RunContext, role, forbidden):
    """**第二道防线**：即便模型硬要调别的工具，也会被明确拒绝。

    为什么必须有第二道：prompt 是**建议**，不是约束。
    模型完全可能"顺手"调一个它看到的工具名 —— 而它不该能调通。

    这与项目的核心命题一致：**不靠自觉，靠机制。**
    """
    box = RestrictedToolBox(empty_ctx, frozenset({role.tool}), role=role.key)
    for bad in forbidden:
        out = box.call(bad, "{}")
        assert "拒绝" in out, f"{role.key} 调 {bad} 竟然没被拒绝：{out[:80]}"
        assert role.tool in out, "拒绝信息里应当告诉它可以用什么"
    assert len(box.denials) == len(forbidden)


def test_denial_is_recorded_not_silent(empty_ctx: RunContext):
    """越权尝试必须被记录 —— 不许静默失败。

    这是从 harness-log #4（4xx 被吞）和 #9（授权静默失效）学到的：
    **被拒绝这件事本身是信息，不能丢。**
    """
    box = RestrictedToolBox(empty_ctx, frozenset({"query_metrics"}), role="metrics")
    box.call("query_logs", '{"level": "ERROR"}')

    assert box.denials == ["query_logs"]
    assert empty_ctx.tool_calls == 1, "越权尝试也应当计入工具调用统计"
    assert empty_ctx.tool_log[-1]["ok"] is False, "越权尝试应当标记为失败"


def test_allowed_call_goes_through(empty_ctx: RunContext):
    """正向对照：允许的工具必须真的能调通。

    没有这条，上面的"拒绝"断言可能是"所有调用都被拒"造成的假绿。
    """
    box = RestrictedToolBox(empty_ctx, frozenset({"query_metrics"}), role="metrics")
    out = box.call("query_metrics", "{}")
    assert "拒绝" not in out
    assert empty_ctx.tool_log[-1]["ok"] is True
    assert box.denials == []


def test_base_toolbox_has_no_restriction(empty_ctx: RunContext):
    """对照：基类 ToolBox（baseline 用的）**不做任何限制**。

    这保证了对照实验的公平性：baseline 拿到的是完整工具面，
    多 Agent 拿到的是被切分的工具面。**变量只有协作结构。**
    """
    box = ToolBox(empty_ctx)
    names = sorted(s["function"]["name"] for s in box.specs())
    assert names == ["get_changes", "query_logs", "query_metrics"]


def test_fork_gives_independent_counters(empty_ctx: RunContext):
    """fork() 必须给出独立计数器，否则并发跑的三个 Agent 统计会互相污染。"""
    a = empty_ctx.fork()
    b = empty_ctx.fork()

    a.tool_calls = 5
    a.tool_log.append({"tool": "x"})

    assert b.tool_calls == 0, "分叉后的计数器应当互不影响"
    assert b.tool_log == []
    assert empty_ctx.tool_calls == 0, "分叉也不应影响原上下文"

    # 底层数据是共享的（读引用）
    assert a.run_id == b.run_id == empty_ctx.run_id


def test_baseline_module_is_frozen():
    """⚠️ 元测试：`baseline.py` 不允许被改动。

    D5 的数字（准确率 61.1%）已经写进文档，是对照实验的分母。
    如果有人为了"统一代码风格"把 baseline 重构成通用类，
    它的行为可能变化，**D5 的数字就失效了**。

    这条用例用内容指纹守住它：文件一旦改动就会变红，
    迫使改动者显式更新指纹**并重新跑一遍 baseline**。
    """
    import hashlib

    path = Path(__file__).resolve().parent.parent / "src" / "rca" / "agents" / "baseline.py"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    expected = "c5cad8da8af3d838"

    assert digest == expected, (
        f"baseline.py 被改动了！\n"
        f"  期望指纹 {expected}\n"
        f"  实际指纹 {digest}\n"
        f"baseline 是对照实验的分母，改动它会让 docs/05 的数字失效。\n"
        f"若确实需要改，请：① 更新本用例里的指纹；② 重新跑一遍 baseline 并更新 docs/05。"
    )
