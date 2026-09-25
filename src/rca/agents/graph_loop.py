"""用 LangGraph 的 `StateGraph` 跑 baseline 那条循环 —— 带**循环级检查点**，可断点续跑（AC-10）。

================================================================================
为什么现在才接 LangGraph（按 ADR-0001 里预先写好的触发条件）
================================================================================

`docs/adr/0001` 写着：**"什么时候应该把 LangGraph 接进来？—— 需要断点续跑（AC-10），
这是最直接的理由，checkpointer 是现成能力。"** 现在就是那个时候。

同时它**不替换**评测路径：`docs/00` 与 `docs/06` 里的数字出自手写的 `baseline.py`
（那是冻结的"分母"），而**两侧必须同驱动才可比**。所以：

    · 评测（`eval/runner.py`）      → 仍然走 `baseline.py`（手写循环），数字不动
    · 断点续跑（本模块 + scripts/demo_resume.py）→ 走 LangGraph + 官方 SQLite checkpointer

这是刻意的、写明的选择：**不为技术栈好看去换驱动**，只在"需要它"的那条路上用它。

================================================================================
它长什么样
================================================================================

    START → call_model ──(有工具调用)──→ execute_tools ──→ call_model → …
                    │
                    └──(没有工具调用 / 撞预算)──→ finalize → END

每个**节点边界**都是一个检查点（用 `durability="sync"` 落盘），
thread_id = `baseline-<run_id>-<fault>-r<round>` ⇒ 进程被杀之后，
**同一个 thread_id + 输入 None** 就能从最后一个完成的节点继续。

⚠️ 复用的是 `baseline.py` 的 prompt、`extract_json` 与 `_parse_root_causes`
   —— **不复制、不改动**那个文件（它有人工内容指纹冻结，见 tests/test_agents.py）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from ..llm.provider import DeepSeekClient
from ..tools import RunContext, ToolBox
from .baseline import SYSTEM_PROMPT, TASK_PROMPT, Diagnosis, _parse_root_causes, extract_json

# 检查点数据库的默认位置（运行时状态，随 runs/ 一起被 gitignore）
DEFAULT_CHECKPOINT_DB = Path(__file__).resolve().parent.parent.parent.parent / "runs" / "_checkpoints" / "graph.sqlite"


class LoopState(TypedDict, total=False):
    """图的状态。

    ⚠️ 计费与步数**必须**放进状态里：断点续跑之后，累计量要从检查点里恢复，
       否则"续跑"出来的成本/步数会从零开始 —— 那等于把测量也重启了。
    """

    messages: list[dict]
    step: int
    nudged: bool
    finished: bool
    raw_text: str
    root_causes: list[str]
    parse_ok: bool
    stop_reason: str
    trace: list[dict]
    tool_calls: int
    replay_hits: int
    cost_yuan: float
    input_tokens: int
    output_tokens: int
    elapsed_s: float


def build_graph(
    client: DeepSeekClient,
    ctx: RunContext,
    *,
    model: str | None = None,
    max_steps: int = 8,
    checkpointer: Any = None,
):
    """构造（并编译）这张图。`checkpointer` 传 None 就是无检查点的纯内存运行。"""

    def call_model(state: LoopState) -> LoopState:
        from .contract import repair_messages  # 延迟导入：contract 反向依赖 baseline

        box = ToolBox(ctx)
        messages = list(state.get("messages") or [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": TASK_PROMPT},
        ])
        step = int(state.get("step") or 0) + 1

        result = client.chat(messages=messages, model=model, tools=box.specs(),
                             tag=f"graph/{ctx.run_id}")
        started = time.perf_counter()

        out: LoopState = dict(state)  # type: ignore[assignment]
        out["step"] = step
        out["cost_yuan"] = float(state.get("cost_yuan") or 0.0) + float(result.cost_yuan)
        # ⚠️ token 要从 `usage` 里取（`LlmResult` 没有 input_tokens 字段）——
        #    与 `baseline._accumulate` 用的是同一组键，别自己发明。
        out["input_tokens"] = int(state.get("input_tokens") or 0) + int(
            result.usage.get("prompt_tokens", 0) or 0)
        out["output_tokens"] = int(state.get("output_tokens") or 0) + int(
            result.usage.get("completion_tokens", 0) or 0)
        out["elapsed_s"] = float(state.get("elapsed_s") or 0.0) + (time.perf_counter() - started)
        if result.usage.get("__replayed__"):
            out["replay_hits"] = int(state.get("replay_hits") or 0) + 1

        # 有工具调用 → 把 assistant 消息原样塞回去（保留协议字段），交给 execute_tools
        if result.tool_calls:
            messages.append(result.raw["choices"][0]["message"])
            out["messages"] = messages
            out["finished"] = False
            out["stop_reason"] = ""
            return out

        # 没有工具调用 → 这是最终结论（可能是合法 JSON，也可能是散文）
        parsed = extract_json(result.text)
        if parsed is None and not state.get("nudged"):
            messages = repair_messages(messages, result.text)
            out["messages"] = messages
            out["nudged"] = True
            out["finished"] = False
            out["stop_reason"] = ""
            return out

        out["messages"] = messages
        out["raw_text"] = result.text
        if parsed:
            out["root_causes"] = _parse_root_causes(parsed)
            out["parse_ok"] = True
        else:
            out["root_causes"] = []
            out["parse_ok"] = False
        out["finished"] = True
        out["stop_reason"] = "answered"
        return out

    def execute_tools(state: LoopState) -> LoopState:
        box = ToolBox(ctx)
        messages = list(state.get("messages") or [])
        last = messages[-1] if messages else {}
        trace = list(state.get("trace") or [])
        n_tools = int(state.get("tool_calls") or 0)

        for tc in _tool_calls_of(last):
            # ⚠️ `ToolBox.call(name, arguments_json)` 要的是**原始 JSON 字符串**
            #    （它自己解析）—— 传 dict 会炸在 `arguments_json.strip()` 上。
            text = box.call(tc["name"], tc["arguments_json"])
            n_tools += 1
            trace.append({"step": state.get("step"), "tool": tc["name"],
                          "args": tc.get("arguments") or {}, "result": text})
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": text})

        out: LoopState = dict(state)  # type: ignore[assignment]
        out["messages"] = messages
        out["trace"] = trace
        out["tool_calls"] = n_tools
        return out

    def finalize(state: LoopState) -> LoopState:
        out: LoopState = dict(state)  # type: ignore[assignment]
        if not out.get("finished"):
            # 撞上步数预算：**不是能力问题，是配置问题**（#13/#22 的教训）
            out["finished"] = False
            out["stop_reason"] = "budget"
        return out

    def route_after_model(state: LoopState) -> str:
        step = int(state.get("step") or 0)
        messages = state.get("messages") or []
        last = messages[-1] if messages else {}
        if _tool_calls_of(last):
            # ⚠️ 预算检查必须放在**这里**。第一版写在"没有工具调用"那条分支上，
            #    于是模型**一直调工具**时图会**无限循环** —— 而步数预算存在的意义
            #    正是拦住这种情况。这个 bug 是新驱动的用例当场抓到的
            #    （`test_the_graph_stops_at_the_budget_and_says_so` 挂死 10 分钟）。
            return "finalize" if step >= max_steps else "execute_tools"
        if state.get("finished"):
            return "finalize"
        if step >= max_steps:
            return "finalize"
        return "call_model"      # JSON 催促之后再来一轮

    g = StateGraph(LoopState)
    g.add_node("call_model", call_model)
    g.add_node("execute_tools", execute_tools)
    g.add_node("finalize", finalize)
    g.add_edge(START, "call_model")
    g.add_conditional_edges("call_model", route_after_model,
                            {"execute_tools": "execute_tools", "call_model": "call_model",
                             "finalize": "finalize"})
    g.add_edge("execute_tools", "call_model")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def _tool_calls_of(message: dict) -> list[dict]:
    """从 assistant 消息里取出工具调用。

    返回的每一项同时带**原始 JSON 字符串**（`ToolBox.call` 要它）与
    **解析后的 dict**（写进 trace 便于人看）。兼容对象与 dict 两种形态。
    """
    tcs = (message or {}).get("tool_calls") or []
    out: list[dict] = []
    for tc in tcs:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            raw = fn.get("arguments")
            out.append({
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "arguments_json": raw if isinstance(raw, str) else json.dumps(raw or {}),
                "arguments": _as_dict(raw),
            })
        else:  # 对象形态（provider 返回的 dataclass）
            raw = getattr(tc, "arguments", "") or ""
            out.append({
                "id": getattr(tc, "id", ""),
                "name": getattr(tc, "name", ""),
                "arguments_json": raw if isinstance(raw, str) else json.dumps(raw),
                "arguments": _as_dict(raw),
            })
    return out


def _as_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    return {}


def thread_id_for(ctx: RunContext, *, fault_id: str, round_no: int) -> str:
    """线程 id —— 断点续跑就是靠"同一个 id + 输入 None"。"""
    return f"baseline-{ctx.run_id}-{fault_id}-r{round_no}"


def open_checkpointer(db_path: Path | None = None):
    """打开 SQLite 检查点（官方 `langgraph-checkpoint-sqlite`）。

    ⚠️ 返回的是**上下文管理器**，调用方要 `with` 它 —— 连接不能提前关。
    """
    path = Path(db_path or DEFAULT_CHECKPOINT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver.from_conn_string(str(path))


def state_to_diagnosis(state: LoopState) -> Diagnosis:
    """把图状态变回 `Diagnosis`（与手写循环**同一个类型**，便于并排比较）。"""
    causes = list(state.get("root_causes") or [])
    return Diagnosis(
        root_cause="；".join(causes),
        root_causes=causes,
        parse_ok=bool(state.get("parse_ok")),
        steps=int(state.get("step") or 0),
        tool_calls=int(state.get("tool_calls") or 0),
        cost_yuan=float(state.get("cost_yuan") or 0.0),
        input_tokens=int(state.get("input_tokens") or 0),
        output_tokens=int(state.get("output_tokens") or 0),
        elapsed_s=float(state.get("elapsed_s") or 0.0),
        finished=bool(state.get("finished")),
        raw_text=str(state.get("raw_text") or ""),
        trace=list(state.get("trace") or []),
    )
