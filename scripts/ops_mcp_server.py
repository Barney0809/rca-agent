"""MCP server 入口（**stdio** 传输）—— ops 工具面的唯一执行通道。

用法（这正是 MCP host 会做的方式）：

    .\\.venv\\Scripts\\python.exe scripts\\ops_mcp_server.py

⚠️⚠️ **本文件绝对不能 `print`。**
    MCP 的 stdio 传输把 **stdout 当作协议通道**：一句调试打印就会污染协议，
    client 端会收到垃圾、报一个与真正原因无关的错（而且极难查 —— 这正是本项目
    一路上在防的"静默失败"）。诊断信息请走工具返回值或审计日志。
    有一条用例守着这一点（`tests/test_mcp_ops.py`）。

⚠️ 它是**被 host 启动**的进程，不是给人直接跑的 CLI —— 人用的入口是 `scripts/ops.py`。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from rca.mcp_server import build_ops_server  # noqa: E402


def main() -> None:
    server = build_ops_server(actor="mcp-client")
    # stdio：stdin 收请求、stdout 回响应。这之后本进程的 stdout 归协议所有。
    server.run("stdio")


if __name__ == "__main__":
    main()
