"""MCP client：**通过 MCP 调用 ops 工具**（而不是 import 那个类直接调）。

这就是"工具面收口"在调用侧的样子：

    async with open_ops_session() as session:
        tools = await session.list_tools()
        res = await session.call_tool("delete_artifact", {"path": "..."})

⚠️ 为什么要在测试里也走真实会话（而不是 `server.call_tool(...)` 直调）：
    直调只验证了 handler 的返回值，**验证不了**"工具面真的经由 MCP 暴露出来了"
    （名字、入参 schema、序列化、协议往返）。本项目对"接线"的标准是
    **端到端跑通一次**，所以 `tests/test_mcp_ops.py` 里有一条内存流会话的往返用例。

⚠️ stdio 传输需要**起子进程并接管道**。在受限沙箱里这可能不可用 ——
    所以测试走 `mcp.shared.memory` 的内存流，只有真正给人用的入口才用 stdio。
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp.client.session import ClientSession

ROOT = Path(__file__).resolve().parent.parent.parent
SERVER_SCRIPT = ROOT / "scripts" / "ops_mcp_server.py"


@asynccontextmanager
async def open_ops_session() -> AsyncIterator[ClientSession]:
    """起一个 ops MCP server 子进程（stdio）并连上它，交出 `ClientSession`。

    ⚠️ 仅在允许起子进程的环境里用（本地开发/演示）。
       沙箱内跑测试请用 `mcp.shared.memory` 的内存流 —— 见测试文件。
    """
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=str(_python_executable()),
        args=[str(SERVER_SCRIPT)],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _python_executable() -> Path:
    """用**当前解释器**起 server（保证 .venv 里的依赖可用）。"""
    return Path(sys.executable)


async def call_ops_tool(name: str, arguments: dict[str, Any]) -> dict:
    """便捷封装：开一个会话、调一次、返回结构化结果。

    ⚠️ 若工具被策略拒绝，**不抛异常** —— 拒绝是正常返回值（见 `OpsResult`）。
    """
    async with open_ops_session() as session:
        result = await session.call_tool(name, arguments)
    return _as_dict(result)


def _as_dict(result: Any) -> dict:
    """把 MCP 的 `CallToolResult` 变成普通 dict（取 structuredContent 或首个文本块）。"""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            import json

            try:
                return json.loads(text)
            except Exception:  # noqa: BLE001
                return {"text": text}
    return {}
