"""M4 事前闸门的用例 —— "block 真的能拦"必须被钉住。

M1–M3 的护栏只**判定**：`block` 是一句结论，动作照样发生。
M4 把它变成动作面的一道闸门。这里守四条：

  1. **判为拦就不转发**：上游工具箱**一次都不能被调用**（这条最容易假实现 ——
     "报了拦但照样转发"看起来完全正常）；
  2. **拦住也要留痕**：事件流里能看到"它要调什么"和"被拦了"，
     所以 M1 的规则仍然看得见这条路径上发生过什么；
  3. **写类工具交给策略执行点**（不是护栏自己发明一套授权）；
  4. **拦住不等于崩掉**：Agent 拿到的是结构化拒绝，可以改道继续。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rca.guard.proxy import Gate, GuardedToolFace  # noqa: E402


class _Box:
    """上游工具箱替身：**记录它到底被调用了几次**（拦截的判据就在这上面）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, name: str, args: dict):
        self.calls.append((name, args))
        return SimpleNamespace(text=f"[{name}] risk_control_latency_ms = 800.5", ok=True)


def _face(**gate_kw) -> tuple[GuardedToolFace, _Box]:
    box = _Box()
    return GuardedToolFace(toolbox=box, label="t", tool_names=lambda: ["query_metrics"],
                           gate=Gate(**gate_kw)), box


# ---------------------------------------------------------------- 1) 真的拦住

def test_repeat_calls_are_blocked_and_never_reach_the_upstream() -> None:
    """同一「工具 + 参数」到阈值就拦 —— 而且**上游一次都没多调**。"""
    face, box = _face(repeat_threshold=2)
    a = face.call("query_metrics", {"service": "payment"})
    b = face.call("query_metrics", {"service": "payment"})
    c = face.call("query_metrics", {"service": "payment"})      # 第 3 次：该拦

    assert a["ok"] is True and b["ok"] is True
    assert c["ok"] is False and c.get("blocked") is True
    assert [f["rule"] for f in c["findings"]] == ["repeated_tool_call"]
    assert len(box.calls) == 2, f"上游被调了 {len(box.calls)} 次 —— 拦下的那次不该转发"
    assert face.blocked == 1


def test_different_arguments_are_not_treated_as_a_repeat() -> None:
    """换了参数就是新的一次调查（打转判据靠**排序后的参数哈希**，不靠工具名）。"""
    face, box = _face(repeat_threshold=1)
    face.call("query_metrics", {"service": "payment"})
    face.call("query_metrics", {"service": "inventory"})       # 不同参数 ⇒ 放行
    assert len(box.calls) == 2
    assert face.blocked == 0


def test_tool_budget_is_enforced() -> None:
    face, box = _face(max_tool_calls=2)
    face.call("query_metrics", {"service": "a"})
    face.call("query_metrics", {"service": "b"})
    third = face.call("query_metrics", {"service": "c"})
    assert third.get("blocked") is True
    assert [f["rule"] for f in third["findings"]] == ["tool_budget_exceeded"]
    assert len(box.calls) == 2


# ---------------------------------------------------------------- 2) 留痕

def test_a_blocked_call_still_leaves_a_trace() -> None:
    """拦住也要留痕：事件流里既看得见"它要调"，也看得见"被拦了"。"""
    face, _ = _face(repeat_threshold=1)
    face.call("query_metrics", {"service": "payment"})
    blocked = face.call("query_metrics", {"service": "payment"})

    kinds = [type(e).__name__ for e in face.trace.events]
    assert kinds == ["ToolCall", "ToolResult", "ToolCall", "ToolResult"]
    last = face.trace.results[-1]
    assert last.ok is False and "护栏拦截" in last.text
    assert blocked["text"] == last.text, "返回给 Agent 的与记进事件流的必须是同一句"


# ---------------------------------------------------------------- 3) 与策略点合流

def test_write_tools_go_through_the_policy_check() -> None:
    """写类工具的授权由**策略执行点**说话，护栏不自己发明一套。"""
    seen: list[tuple[str, dict]] = []

    def deny(name: str, args: dict):
        seen.append((name, args))
        return False, "不可逆动作需要人签发的授权"

    face, box = _face(write_tools=("delete_artifact",), policy_check=deny)
    out = face.call("delete_artifact", {"path": "runs/x"})

    assert out.get("blocked") is True
    assert [f["rule"] for f in out["findings"]] == ["policy_denied"]
    assert "人签发" in out["findings"][0]["detail"]
    assert seen == [("delete_artifact", {"path": "runs/x"})], "策略点必须被问到"
    assert box.calls == [], "被策略点拒绝的动作绝不能到上游"


def test_policy_allows_a_write_that_is_permitted() -> None:
    face, box = _face(write_tools=("set_knobs",), policy_check=lambda n, a: (True, "可逆"))
    out = face.call("set_knobs", {"service": "payment", "knobs": {"latency_ms": 900}})
    assert out["ok"] is True and face.blocked == 0
    assert len(box.calls) == 1


# ---------------------------------------------------------------- 4) 拦住不等于崩掉

def test_agent_can_continue_after_being_blocked() -> None:
    """被拦之后 Agent 还能改道、还能交卷 —— 护栏不该把流程打断成异常。"""
    face, _ = _face(repeat_threshold=1)
    face.call("query_metrics", {"service": "payment"})
    assert face.call("query_metrics", {"service": "payment"}).get("blocked") is True

    ok = face.call("query_logs", {"level": "ERROR"})            # 换一条路 ⇒ 放行
    assert ok["ok"] is True
    verdict = face.submit_conclusion("根因是外部风控变慢（risk_control_latency_ms=800.5ms）。")
    assert verdict["verdict"] == "allow", verdict["findings"]
    assert face.to_dict()["n_blocked"] == 1
