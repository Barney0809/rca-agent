"""M3 线路层的离线用例 —— 全部走**真实 MCP 会话**，不花钱、不调模型。

为什么不能直调 handler（`server.call_tool(...)`）：那只能证明 handler 的返回值，
**证明不了"工具面真的经由 MCP 暴露"** —— 名字、入参 schema、序列化、协议往返
（`tests/test_mcp_ops.py` 用的是同一个理由）。本项目对"接线"的标准是**端到端跑通一次**。

这里钉住四件事：
  1. 面**长什么样**：上游工具 + `submit_conclusion` 都在，且**参数名与上游一致**；
  2. 转发真的把调用与返回送进那条会话，并落进判定事件流；
  3. `submit_conclusion` 经协议往返能拿到判定（block → 修正 → allow）；
  4. 模块里**不许 print**（stdio 传输把 stdout 当协议通道 —— 与 ops MCP server 同一纪律）。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rca.guard.mcp_face import (  # noqa: E402
    build_guarded_server,
    open_face_session,
    payload,
)
from rca.guard.proxy import GuardedToolFace  # noqa: E402

TOOLS = ("query_logs", "query_metrics", "get_changes")
EVIDENCE = "[query_metrics] risk_control_latency_ms = 800.5\n[query_metrics] pool_in_flight = 0"


class _Box:
    """上游只读工具箱替身：返回一段**含可溯源指标**的文本。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, name: str, args: dict):
        self.calls.append((name, args))
        return SimpleNamespace(text=EVIDENCE, ok=True)


def _face() -> tuple[GuardedToolFace, _Box]:
    box = _Box()
    return GuardedToolFace(toolbox=box, label="test", tool_names=lambda: list(TOOLS)), box


def _tools_and_call(server, tool: str, args: dict, *, then_submit: str | None = None):
    """在一个真实会话里：列工具 → 调一个工具 →（可选）交一次卷。"""

    async def _inner():
        async with open_face_session(server) as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
            called = payload(await session.call_tool(tool, args))
            submitted = None
            if then_submit is not None:
                submitted = payload(
                    await session.call_tool("submit_conclusion", {"text": then_submit})
                )
            return tools, called, submitted

    return asyncio.run(_inner())


# ---------------------------------------------------------------- 1) 面的形状

def test_face_exposes_upstream_tools_plus_submit_conclusion() -> None:
    face, _ = _face()
    tools, _, _ = _tools_and_call(build_guarded_server(face), "query_metrics", {"service": "payment"})
    assert set(tools) == set(TOOLS) | {"submit_conclusion"}


def test_forwarded_tools_keep_the_upstream_parameter_names() -> None:
    """**参数名必须与上游一致** —— 否则"不需要对方改代码"这句话就不成立。

    ⚠️ 这条是踩出来的：第一版用 `def h(**kwargs)` 转发，MCP 按**函数签名**生成
       schema，于是 SDK 直接拒（`Tool 'query_metrics' rejected arguments: ['kwargs']`）。
       schema 保真正是"经过 MCP"这件事的意义之一。
    """
    face, _ = _face()
    tools, _, _ = _tools_and_call(build_guarded_server(face), "query_metrics", {"service": "payment"})

    def props(tool) -> set[str]:
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
        return set(schema.get("properties", {}))

    assert props(tools["query_metrics"]) == {"service"}
    assert props(tools["query_logs"]) == {"level", "limit"}
    assert props(tools["get_changes"]) == {"keyword"}


# ---------------------------------------------------------------- 2) 转发与留痕

def test_forwarding_reaches_the_upstream_and_lands_in_the_event_stream() -> None:
    face, box = _face()
    server = build_guarded_server(face)
    _, called, _ = _tools_and_call(server, "query_metrics", {"service": "payment"})

    assert box.calls == [("query_metrics", {"service": "payment"})], "参数要原样到上游"
    assert called.get("ok") is True and "800.5" in str(called.get("text"))
    kinds = [type(e).__name__ for e in face.trace.events]
    assert kinds == ["ToolCall", "ToolResult"], "调用与返回都要进事件流（护栏靠它判证据）"


# ---------------------------------------------------------------- 3) 结论面

def test_submit_conclusion_round_trip_blocks_then_allows() -> None:
    """经协议往返：注入的编造指标名被 block，修正后 allow。"""
    face, _ = _face()
    server = build_guarded_server(face)

    async def _two_submissions():
        async with open_face_session(server) as session:
            await session.call_tool("query_metrics", {"service": "payment"})
            first = payload(await session.call_tool(
                "submit_conclusion", {"text": "根因是 fabricated_metric_total 异常，987654。"}
            ))
            second = payload(await session.call_tool(
                "submit_conclusion", {"text": "根因是外部风控变慢（risk_control_latency_ms=800.5ms）。"}
            ))
            return first, second

    first, second = asyncio.run(_two_submissions())
    assert first["verdict"] == "block"
    assert {f["rule"] for f in first["findings"]} == {"unsupported_claim", "unverifiable_number"}
    assert second["verdict"] == "allow", second["findings"]
    assert [s["submission_no"] for s in face.to_dict()["submissions"]] == [1, 2]


def test_unknown_tool_signature_falls_back_to_a_passthrough_object() -> None:
    """不在签名表里的上游工具 ⇒ 退化成 `arguments` 对象透传（如实、不假装保真）。"""
    box = _Box()
    face = GuardedToolFace(toolbox=box, label="t", tool_names=lambda: ["mystery_tool"])
    tools, called, _ = _tools_and_call(build_guarded_server(face), "mystery_tool",
                                       {"arguments": {"a": 1}})
    assert "arguments" in (
        getattr(tools["mystery_tool"], "inputSchema", None)
        or getattr(tools["mystery_tool"], "input_schema", None) or {}
    ).get("properties", {})
    assert called.get("ok") is True
    assert box.calls == [("mystery_tool", {"a": 1})], "透传的参数要能到上游"


# ---------------------------------------------------------------- 4) 协议纪律

def test_the_face_module_never_prints() -> None:
    """stdio 传输把 stdout 当协议通道 —— 一句 print 就会污染协议。

    （本模块现在只用内存流，但纪律保持一致：将来有人把它接上 stdio 时不会踩雷。）
    """
    src = (Path(__file__).resolve().parent.parent / "src" / "rca" / "guard" / "mcp_face.py")
    assert not re.search(r"^\s*print\(", src.read_text(encoding="utf-8"), re.M), \
        "mcp_face.py 里出现了 print( —— 它会污染 MCP 协议通道"
