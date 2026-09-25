"""把护栏代理挂到 **MCP** 上 —— 外部 Agent 的接入形态（D21 / M3）。

   外部 Agent  →  MCP client  →  **本模块的 MCP server**  →  护栏代理  →  真实工具

============================ 为什么必须经过 MCP ============================

ADR-0007 决定 3：三条路（读事件流 / 注册钩子 / 代理）里，只有**代理**既能拦、
又**不要求对方改代码**。对第三方的要求只剩一句"把工具接到这个代理上"，
而 MCP 是当下最通用的工具协议。

它也把"护栏在看什么"变成**可核对的性质**：对方的每一次工具调用与返回都从这条会话过，
没有第二条路（自家那套 `src/rca/mcp_server.py` 用的是同一个理由与同一种写法）。

============================ 两面 ============================

    工具面：上游工具箱里的每个工具，原样注册成转发（名字不变，对方无需改代码）
    结论面：额外注册一个 `submit_conclusion(text)` —— **这是 MCP 流量里看不到的东西**
            （工具调用有，结论没有），所以必须由对方显式交进来。见 ADR-0007 冻结的契约。

⚠️ 与 `src/rca/mcp_server.py` 同一纪律：**本模块里不许 `print`**
   —— stdio 传输把 stdout 当协议通道，一句打印就污染协议。
   （本模块只用内存流，不涉及 stdio；但纪律保持一致，以免将来被人接上 stdio。）
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

import anyio
from mcp.client.session import ClientSession
from mcp.server import MCPServer
from mcp.shared.memory import create_client_server_memory_streams

from .proxy import GuardedToolFace

SERVER_NAME = "rca-guard"
SERVER_INSTRUCTIONS = (
    "被护栏看着的工具面。工具调用会被记录用于判定；"
    "结束时必须调用 submit_conclusion 交回结论，否则视为未完成（不是默认放行）。"
    "护栏只判**可自证的形式缺陷**（如结论点名了工具从未返回过的指标名），不判对错。"
)


def build_guarded_server(
    face: GuardedToolFace,
    *,
    name: str = SERVER_NAME,
    instructions: str = SERVER_INSTRUCTIONS,
) -> MCPServer:
    """构造护栏的 MCP server：**上游工具 + submit_conclusion**。"""
    server: MCPServer = MCPServer(name=name, instructions=instructions)

    for tool_name in face.names():
        _register_forwarder(server, face, tool_name)

    @server.tool(
        name="submit_conclusion",
        description=(
            "交回最终结论（纯文本）。护栏会拿它与本次工具返回对照，"
            "返回 {verdict, findings, submission_no}："
            "allow = 没发现形式缺陷；warn/block = 有问题，可以按 findings 修正后**再交一次**。"
        ),
    )
    def submit_conclusion(text: str) -> dict:
        return face.submit_conclusion(text)

    return server


#: 上游只读工具的入参形状（名字 → 参数名 → Python 注解）。
#:
#: ⚠️ 这里**必须**给出具体签名：MCP 用**函数的签名**生成入参 schema，
#:    写成 `def h(**kwargs)` 会被 SDK 直接拒掉（实测：`Tool 'query_metrics' rejected
#:    arguments: ['kwargs']`）。而 schema 保真正是"经过 MCP"这件事的意义之一 ——
#:    对方拿到的参数名与上游一致，不需要改代码。
UPSTREAM_SIGNATURES: dict[str, dict[str, type]] = {
    "query_logs": {"level": str, "limit": int},
    "query_metrics": {"service": str},
    "get_changes": {"keyword": str},
}


def _register_forwarder(server: MCPServer, face: GuardedToolFace, tool_name: str) -> None:
    """给一个上游工具注册转发 handler（保留上游的参数名）。"""
    params = UPSTREAM_SIGNATURES.get(tool_name, {"arguments": dict})
    #: 不在签名表里的工具走"对象透传"——此时**必须拆包**，把 `arguments` 的内容交给上游，
    #: 而不是把 `{"arguments": {...}}` 原样塞进去（实测被自己的用例抓到）。
    passthrough = tool_name not in UPSTREAM_SIGNATURES

    def _handler(**kwargs: Any) -> dict:
        if passthrough:
            return face.call(tool_name, dict(kwargs.get("arguments") or {}))
        return face.call(tool_name, {k: v for k, v in kwargs.items() if v is not None})

    _handler.__name__ = f"forward_{tool_name}"
    # 用显式签名覆盖 `**kwargs`，让 SDK 生成与上游一致的入参 schema
    _handler.__signature__ = inspect.Signature(       # type: ignore[attr-defined]
        parameters=[
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY,
                              default=None, annotation=annotation)
            for name, annotation in params.items()
        ]
    )
    _handler.__annotations__ = dict(params)
    server.tool(
        name=tool_name,
        description=f"[经护栏代理转发] {tool_name}：调用与返回都会进入判定事件流。",
    )(_handler)


@asynccontextmanager
async def open_face_session(server: MCPServer) -> AsyncIterator[ClientSession]:
    """在一个**真实的 MCP 会话**里连上这个 server（内存流传输）。

    内存流 = 协议层是真的，只是不跨进程 —— 与 `tests/test_mcp_ops.py` 同一个理由：
    **直调 handler 验证不了"工具面真的经由 MCP 暴露"**（名字、schema、序列化、往返）。

    ⚠️ `_lowlevel_server` 是 SDK 的私有属性；仓库里有一条守卫用例盯着它还在不在。
    """
    low = server._lowlevel_server
    async with create_client_server_memory_streams() as (cstreams, sstreams):
        c_read, c_write = cstreams
        s_read, s_write = sstreams
        async with ClientSession(c_read, c_write) as session:
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: low.run(s_read, s_write, low.create_initialization_options())
                )
                await session.initialize()
                try:
                    yield session
                finally:
                    tg.cancel_scope.cancel()


def payload(result: Any) -> dict:
    """把 MCP 的 `CallToolResult` 变成普通 dict（结构化字段优先，其次首个文本块）。"""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in (getattr(result, "content", None) or []):
        text = getattr(block, "text", None)
        if text:
            try:
                import json

                loaded = json.loads(text)
                if isinstance(loaded, dict):
                    return loaded
            except Exception:      # noqa: BLE001 —— 非 JSON 就当纯文本
                return {"text": text}
    return {}


ToolFaceBuilder = Callable[[], MCPServer]

__all__ = [
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "build_guarded_server",
    "open_face_session",
    "payload",
]
