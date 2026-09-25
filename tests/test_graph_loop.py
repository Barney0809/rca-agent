"""LangGraph 驱动的用例：**循环级断点续跑**（AC-10）。

============================ 这个文件在验什么 ============================

AC-10 的原话是「循环级 checkpoint；**进程被杀后**可断点续跑」。
所以证据不能只是"我 import 了 langgraph"，而必须回答：

  1. 这张图跑起来和手写循环**同一个形状**（同样的 prompt、同样的解析、
     同样的计数）——否则它就不是"同一个任务"，只是另一个程序；
  2. **中途断掉之后，从检查点继续，前面做完的步骤不会重做** ——
     这一条是"断点续跑"的真正含义（重跑一遍也能出结果，但那不叫续跑）；
  3. **累计量（步数/成本/token）跨检查点恢复** —— 否则"续跑"出来的成本从零算起，
     等于把测量也重启了（而本项目最在意的就是这个）。

⚠️ 全部用**假客户端**（不花钱、不需要世界、不需要 API key）：
   真刀真枪的"强杀进程再续跑"由 `scripts/demo_resume.py` 跑一次做端到端演示，
   但那种演示不可靠（要网络、要世界、要钱），**不该进日常用例**。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from rca.agents.graph_loop import (
    build_graph,
    open_checkpointer,
    state_to_diagnosis,
    thread_id_for,
)
from rca.llm.provider import LlmResult, ToolCall
from rca.telemetry.reduce import ReducedView
from rca.tools import RunContext

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# 假客户端：按剧本返回，并记录**每一次调用**
# --------------------------------------------------------------------------- #
@dataclass
class _Scripted:
    """按剧本走的假 LLM。`calls` 是全局计数（跨"进程"共享，用来证明没重做）。"""

    script: list[dict]
    calls: list[dict]
    crash_on_call: int | None = None

    def chat(self, *, messages, model=None, tools=None, tag="", **kw) -> LlmResult:
        n = len(self.calls)
        self.calls.append({"n": n, "tag": tag})
        if self.crash_on_call is not None and n == self.crash_on_call:
            raise RuntimeError("模拟进程被杀（用例里用异常代替 os._exit）")

        step = self.script[min(n, len(self.script) - 1)]
        if step.get("tool"):
            call = ToolCall(id=f"call_{n}", name=step["tool"], arguments=json.dumps(step["args"]))
            raw = {"choices": [{"message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": call.id, "type": "function",
                                "function": {"name": call.name,
                                             "arguments": call.arguments}}],
            }}]}
            return LlmResult(text="", tool_calls=[call], finish_reason="tool_calls",
                             usage={"total_tokens": 10}, raw=raw,
                             cost_yuan=0.001, model_used="stub")
        return LlmResult(text=step["answer"],
                         finish_reason="stop", usage={"total_tokens": 10},
                         raw={"choices": [{"message": {"role": "assistant",
                                                       "content": step["answer"]}}]},
                         cost_yuan=0.002, model_used="stub")


@pytest.fixture()
def ctx() -> RunContext:
    # 与 tests/test_loop_defense.py 的 fixture 同形：最小可用的诊断上下文
    # （log_view 空、metrics 空、changes 空 —— 工具能调，但返回"没有匹配"）
    return RunContext(
        run_id=f"t-{uuid.uuid4().hex[:6]}",
        log_view=ReducedView(window_start=None, window_end=None, total_lines=0),
        metrics={},
        changes=[],
        scenario={},
    )


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return tmp_path / "checkpoints.sqlite"


def _final_answer() -> str:
    return json.dumps({
        "root_causes": ["inventory 的下游重试次数被从 1 改为 5，形成重试风暴"],
        "evidence": ["[inventory] ERROR ×127 调用下游 payment 重试 5 次全部失败"],
        "confidence": 0.8,
    }, ensure_ascii=False)


def _script_two_tools_then_answer() -> list[dict]:
    return [
        {"tool": "query_logs", "args": {"keyword": "重试"}},
        {"tool": "query_metrics", "args": {"service": "inventory"}},
        {"answer": _final_answer()},
    ]


# --------------------------------------------------------------------------- #
# 1. 跑完：形状与手写循环一致
# --------------------------------------------------------------------------- #
def test_the_graph_completes_and_parses_like_the_hand_written_loop(ctx: RunContext, db: Path) -> None:
    client = _Scripted(script=_script_two_tools_then_answer(), calls=[])
    with open_checkpointer(db) as saver:
        graph = build_graph(client, ctx, max_steps=8, checkpointer=saver)
        final = graph.invoke({}, config={"configurable": {"thread_id": thread_id_for(
            ctx, fault_id="F4", round_no=1)}}, durability="sync")

    diag = state_to_diagnosis(final)

    assert diag.parse_ok is True
    assert diag.root_causes and "重试" in diag.root_causes[0], diag.root_causes
    assert diag.steps == 3, f"预期 3 轮（2 轮工具 + 1 轮结论），实际 {diag.steps}"
    assert diag.tool_calls == 2, f"预期 2 次工具调用，实际 {diag.tool_calls}"
    assert diag.finished is True
    assert len(diag.trace) == 2, "trace 应当留下每一步工具调用与返回（#29）"
    assert db.exists() and db.stat().st_size > 0, "检查点数据库应当真的写出来了"


# --------------------------------------------------------------------------- #
# 2. ★ AC-10 的核心：断掉之后续跑，**不重做**已完成的步骤
# --------------------------------------------------------------------------- #
def test_resume_after_a_crash_does_not_redo_completed_steps(ctx: RunContext, db: Path) -> None:
    """第 3 次调用时"进程被杀"（用例里用异常模拟），然后用**新的图 + 同一个
    thread_id**、输入 `None` 续跑：

      · 它必须跑到结论（证明检查点可用）；
      · 而且**总调用次数只比原来多一次**（证明前两步没有重做）。
    """
    tid = thread_id_for(ctx, fault_id="F4", round_no=1)
    cfg = {"configurable": {"thread_id": tid}}
    client = _Scripted(script=_script_two_tools_then_answer(), calls=[], crash_on_call=2)

    # ---- 第一次：跑到一半被"杀" ----
    with open_checkpointer(db) as saver:
        graph = build_graph(client, ctx, max_steps=8, checkpointer=saver)
        with pytest.raises(RuntimeError, match="模拟进程被杀"):
            graph.invoke({}, config=cfg, durability="sync")

    calls_before = len(client.calls)
    assert calls_before == 3, f"被杀前应当调用过 3 次（含被杀那次），实际 {calls_before}"

    # ---- 第二次：**新的图、新的连接**，输入 None = 从检查点继续 ----
    client.crash_on_call = None
    with open_checkpointer(db) as saver:
        graph = build_graph(client, ctx, max_steps=8, checkpointer=saver)
        state_now = graph.get_state(cfg)
        assert state_now.values, "检查点里应当是空的 —— 那第一次就没落盘"
        resumed = graph.invoke(None, config=cfg, durability="sync")

    diag = state_to_diagnosis(resumed)

    assert diag.finished is True, f"续跑没有跑完：{diag.stop_reason}"
    assert diag.parse_ok is True
    # ★ 关键：**总调用次数**只比原来多一次（第 3 步），不是从头再来一遍
    assert len(client.calls) == 4, (
        f"续跑把已完成的两步又跑了一遍（总调用 {len(client.calls)} 次，应为 4）——"
        f"那就不是「续跑」，只是「重跑」"
    )
    # ★ 累计量跨检查点恢复（否则续跑出来的成本从零算起）
    assert diag.cost_yuan > 0.003, f"成本没有累计起来：{diag.cost_yuan}"
    assert diag.steps == 3, f"步数应当接着数：{diag.steps}"
    assert diag.tool_calls == 2


def test_a_fresh_thread_id_starts_from_scratch(ctx: RunContext, db: Path) -> None:
    """反向对照：**换个 thread_id** 就是一次全新的运行（不能从别人的检查点里接着跑）。

    没有这条，"能续跑"和"永远续跑"就分不清了 —— 而后者会把两次运行混成一次。
    """
    client = _Scripted(script=_script_two_tools_then_answer(), calls=[])
    # ⚠️ 线程 id 必须**经由 `thread_id_for`** 生成 —— 用字面量的话，
    #    那个函数改坏了也测不出来（第一版就是字面量，变异体当场没变红）。
    tid_a = thread_id_for(ctx, fault_id="F4", round_no=1)
    tid_b = thread_id_for(ctx, fault_id="F8", round_no=2)
    assert tid_a != tid_b, "不同故障/轮次必须是不同的线程 id（否则第二次会续到第一次身上）"

    with open_checkpointer(db) as saver:
        graph = build_graph(client, ctx, max_steps=8, checkpointer=saver)
        graph.invoke({}, config={"configurable": {"thread_id": tid_a}}, durability="sync")
        calls_after_a = len(client.calls)
        final_b = graph.invoke({}, config={"configurable": {"thread_id": tid_b}},
                               durability="sync")

    assert calls_after_a == 3, f"thread-A 应当跑 3 次（2 工具 + 1 结论），实际 {calls_after_a}"
    # ⚠️ 这里**不写死** thread-B 用几次调用：假客户端的剧本是按"全局调用次数"索引的，
    #    所以 thread-B 第一轮就拿到的可能已经是结论那一条（1 次就完事）。
    #    要验的是**换 thread 会真的重新执行**（而不是从别人的检查点里接着跑）——
    #    而"重新执行过"的证据就是调用次数**确实增加了**，且拿到了一个**完成**的状态。
    assert len(client.calls) > calls_after_a, (
        "换了 thread_id 却一次都没重跑 —— 那说明它复用了别的线程的检查点"
    )
    assert state_to_diagnosis(final_b).finished is True


def test_the_graph_stops_at_the_budget_and_says_so(ctx: RunContext, db: Path) -> None:
    """撞预算必须**说出来**（`stop_reason="budget"`）—— 那是配置问题，不是能力问题（#13/#22）。

    没有这条，一次"没跑完"会被当成"答错了"（这正是 #13 的教训）。
    """
    client = _Scripted(script=[{"tool": "query_logs", "args": {}}], calls=[])  # 一直调工具
    with open_checkpointer(db) as saver:
        graph = build_graph(client, ctx, max_steps=2, checkpointer=saver)
        final = graph.invoke({}, config={"configurable": {"thread_id": "t-budget"}},
                             durability="sync")

    assert final.get("stop_reason") == "budget"
    assert state_to_diagnosis(final).finished is False
