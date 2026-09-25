"""M3 验收演示：**外部 Agent** 通过 MCP 被护栏看着（D21 / M3）。

============================ 这个演示想证明什么 ============================

    一个**别的框架写的** Agent（这里用 LangGraph），工具面走**我们的 MCP 护栏代理**，
    它在结论里犯了一个**我们预先注入的**形式缺陷 —— 护栏必须**当场抓住**，
    Agent 按 findings 修正后再交，护栏放行。

为什么用"注入已知错误"而不是"找个真外部 Agent"（ADR-0007 决定 3）：

    真外部 Agent 的失败原因不属于我们，**没有 ground truth ⇒ 护栏对不对无法判定**。
    注入之后，"该抓住的"是我们自己造的，验收才成立。

============================ 为什么结论要单独交 ============================

MCP 流量只覆盖**行动面**（工具调用与返回）。Agent 最后那句话**不在流量里**，
而"这个指标名是编的""这个数字没依据"恰恰要用结论去对照证据
⇒ 对方必须在结束时调 `submit_conclusion`（ADR-0007 里冻结的契约）。

============================ 用法 ============================

    # 零成本：结论由模板拼出来（不调模型），只验管道与判定
    python scripts/external_agent_demo.py --no-llm

    # 真实一点：用模型写结论（约 2 次调用，¥0.003 量级）
    python scripts/external_agent_demo.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, TypedDict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from langgraph.graph import END, START, StateGraph                 # noqa: E402

from rca.guard.mcp_face import build_guarded_server, open_face_session, payload  # noqa: E402
from rca.guard.proxy import GuardedToolFace                       # noqa: E402
from rca.tools import RunContext, ToolBox                         # noqa: E402

TOOLS = ("query_logs", "query_metrics", "get_changes")

#: 注入的**已知错误**：一个不存在的指标名 + 一个查无此数的数值。
#: 这就是本次验收的 ground truth —— 护栏必须抓住它。
INJECTED_NAME = "fabricated_latency_budget_ms"
INJECTED_NUMBER = "987654"


class State(TypedDict, total=False):
    session: Any
    evidence: str
    conclusion: str
    first_verdict: dict
    final_verdict: dict
    round_no: int


def _latest_run() -> Path:
    """找一个归档运行（只用来标注报告与核对场景号）。

    ⚠️ **不能**要求它带 traces：入库的两臂只提交了 `results.json`（traces 太大、
       核对数字用不到），要求 traces 会让**干净克隆里这个演示直接跑不起来**
       —— 而"陌生人能复现"正是这条验收的意义。场景数据由 `--scenario` 给
       （可以用随仓库提交的 `replay_pack/`）。
    """
    cands = sorted(
        (d for d in (ROOT / "runs" / "_eval").glob("multi-*")
         if (d / "results.json").exists()),
        key=lambda p: p.stat().st_mtime,
    )
    if not cands:
        raise SystemExit("找不到任何归档运行（runs/_eval/multi-*/results.json）")
    return cands[-1]


def _scenario_dirs() -> list[Path]:
    return sorted((ROOT / "runs").glob("r-2*"))


def build_agent(face: GuardedToolFace, *, use_llm: bool, model: str | None):
    """外部 Agent：一个最小的 LangGraph 图（**不是**仓库里那套手写循环）。"""

    async def probe(state: State) -> State:
        session = state["session"]
        got: list[str] = []
        for name, args in (("query_metrics", {"service": "payment"}),
                           ("query_logs", {"level": "WARNING", "limit": 20})):
            res = payload(await session.call_tool(name, args))
            got.append(f"[{name}] {str(res)[:400]}")
        return {"evidence": "\n".join(got)}

    async def conclude(state: State) -> State:
        text = ""
        if use_llm:
            from rca.llm.provider import DeepSeekClient, LlmConfig

            client = DeepSeekClient(LlmConfig.from_env())
            prompt = (
                "下面是工具返回的证据，请用两句话给出根因结论。\n"
                "**为了演示护栏，请务必在结论里提到指标 "
                f"{INJECTED_NAME} 与数值 {INJECTED_NUMBER}**（这是刻意的注入，不是真实观测）。\n\n"
                + state["evidence"][:2000]
            )
            text = client.chat(messages=[{"role": "user", "content": prompt}],
                               model=model, tag="external-agent").text
        else:
            text = (f"根因是外部风控变慢；{INJECTED_NAME} 达到 {INJECTED_NUMBER}，"
                    "这是本次异常的主因。（注：指标名与数值是**刻意注入**的错误）")
        return {"conclusion": text}

    async def submit(state: State) -> State:
        session = state["session"]
        res = payload(await session.call_tool("submit_conclusion", {"text": state["conclusion"]}))
        out: State = {"round_no": state.get("round_no", 0) + 1}
        if not state.get("first_verdict"):
            out["first_verdict"] = res
        out["final_verdict"] = res
        return out

    async def fix(state: State) -> State:
        """按 findings 修正：把注入的两处换成**证据里真实存在**的名字与数值。

        ⚠️ 不能写死替换值：F4 的证据里未必有 `risk_control_latency_ms = 800.5`
           （那是 F8 的观测）。写死的替换会再次被判定为无据 —— 实测踩过一次。
           真实 Agent 也会这么做：从证据里挑一个能溯源的。
        """
        import re

        text = state["conclusion"].replace(INJECTED_NAME, "").replace(INJECTED_NUMBER, "")
        pairs = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\d+(?:\.\d+)?)", state["evidence"])
        if pairs:
            name, value = pairs[0]
            text += f"（已按 findings 修正：改为引用证据中可溯源的 {name}={value}）"
        return {"conclusion": text + " 以上主张均取自本次工具返回。"}

    def after_submit(state: State) -> str:
        v = (state.get("final_verdict") or {}).get("verdict")
        if v == "allow" or state.get("round_no", 0) >= 2:
            return END
        return "fix"

    graph = StateGraph(State)
    graph.add_node("probe", probe)
    graph.add_node("conclude", conclude)
    graph.add_node("submit", submit)
    graph.add_node("fix", fix)
    graph.add_edge(START, "probe")
    graph.add_edge("probe", "conclude")
    graph.add_edge("conclude", "submit")
    graph.add_conditional_edges("submit", after_submit, {"fix": "fix", END: END})
    graph.add_edge("fix", "submit")
    return graph.compile()


async def run(*, use_llm: bool, model: str | None, fault: str, round_no: int,
              scenario_override: str = "") -> dict:
    run_dir = _latest_run()
    data = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    attempt = next((a for a in data["attempts"]
                    if a["fault_id"] == fault and a["round_no"] == round_no), None)
    if attempt is None:
        # 只是标注用 —— 场景数据由 --scenario 决定，缺这次归档不该让演示挂掉
        print(f"  ⚠️ {run_dir.name} 里没有 {fault}:{round_no}，只用它做场景数据来源的标注")

    # 工具面：只读，直接架在归档场景数据上（外部 Agent 不知道也不关心数据从哪来）
    #
    # ⚠️ 场景目录**不能**从 trace 路径推：trace 在 `runs/_eval/<run>/traces/` 下，
    #    从它推出来的是**运行目录**而不是场景目录 —— 那样工具读不到指标快照，
    #    会静默回退到 live /metrics（累计值），让外部 Agent 在错误的证据上下结论。
    #    这里用 runner 的同一个映射（`discover_runs`），并留一个显式覆盖口。
    from eval.runner import discover_runs

    scenario = Path(scenario_override) if scenario_override else discover_runs([fault]).get(fault)
    if scenario is None:
        raise SystemExit(f"找不到 {fault} 的场景目录 —— 先跑一次场景或用 --scenario 指定")
    # 相对路径要归一化：否则后面 `relative_to(ROOT)` 会抛 ValueError（实测踩过）
    scenario = scenario if scenario.is_absolute() else (ROOT / scenario)
    scenario = scenario.resolve()
    ctx = RunContext.from_run_dir(scenario)
    box = ToolBox(ctx)
    face = GuardedToolFace(toolbox=box, label=f"external-agent@{run_dir.name}",
                           tool_names=lambda: list(TOOLS))
    server = build_guarded_server(face)

    agent = build_agent(face, use_llm=use_llm, model=model)
    async with open_face_session(server) as session:
        # ⚠️ 取 `.tools` 而不是直接迭代结果：这个 SDK 版本里 `ListToolsResult` 是**具名元组**，
        #    直接迭代拿到的是字段值（踩过：`'tuple' object has no attribute 'name'`）。
        tools_result = await session.list_tools()
        listed = sorted(t.name for t in tools_result.tools)
        final: State = await agent.ainvoke({"session": session})

    out = {
        "run": run_dir.name,
        "scenario_dir": str(scenario.relative_to(ROOT)),
        "mcp_tools_listed": listed,
        "injected_error": {"name": INJECTED_NAME, "number": INJECTED_NUMBER},
        "conclusion_v1": final.get("conclusion", "")[:400],
        "first_verdict": final.get("first_verdict"),
        "final_verdict": final.get("final_verdict"),
        "face": face.to_dict(),
    }
    (ROOT / "runs" / "_evidence" / "m3-external-agent.json").parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "runs" / "_evidence" / "m3-external-agent.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="M3：外部 Agent 经 MCP 被护栏看着")
    ap.add_argument("--no-llm", action="store_true", help="结论用模板拼（零成本，只验管道）")
    ap.add_argument("--model", default=None)
    ap.add_argument("--fault", default="F4")
    ap.add_argument("--round", dest="round_no", type=int, default=1)
    ap.add_argument("--scenario", default="", help="显式指定场景数据目录（默认用 discover_runs 的映射）")
    args = ap.parse_args()

    out = asyncio.run(run(use_llm=not args.no_llm, model=args.model,
                          fault=args.fault, round_no=args.round_no,
                          scenario_override=args.scenario))

    print("=" * 96)
    print("  M3 验收：外部 Agent（LangGraph）→ 我们的 MCP 护栏代理 → 只读工具")
    print("=" * 96)
    print(f"\n  场景数据：{out['scenario_dir']}")
    print(f"  MCP 上列出的工具：{', '.join(out['mcp_tools_listed'])}")
    print(f"  注入的已知错误：{out['injected_error']['name']} = {out['injected_error']['number']}")

    v1 = out["first_verdict"] or {}
    print(f"\n  ── 第一次交卷：判定 = {v1.get('verdict')} ──")
    for f in v1.get("findings", []):
        print(f"     [{f['severity'].upper()}] {f['rule']}：{f['subject']}")
        print(f"         {f['detail'][:110]}")
        print(f"         证据：{'、'.join(f['evidence']) or '(无)'}")

    v2 = out["final_verdict"] or {}
    print(f"\n  ── 修正后再交：判定 = {v2.get('verdict')} ──")
    for f in v2.get("findings", []):
        print(f"     [{f['severity'].upper()}] {f['rule']}：{f['subject']}")

    caught = v1.get("verdict") == "block" and v2.get("verdict") == "allow"
    print(f"\n  {'✅ 通过' if caught else '❌ 不通过'} —— 注入的形式缺陷"
          f"{'被当场抓住、修正后放行' if caught else '没有被正确处理'}")
    print("  详情已写：runs/_evidence/m3-external-agent.json")
    return 0 if caught else 1


if __name__ == "__main__":
    raise SystemExit(main())
