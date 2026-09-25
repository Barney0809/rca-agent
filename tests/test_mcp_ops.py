"""MCP 收口的用例：**工具面真的经由 MCP 暴露，而且没有旁路**（FR-2.1）。

============================ 这个文件在验什么 ============================

D15 把 ops 动作接上了策略执行点，但那时是**进程内直调**：
README 里"工具面没有经过 MCP 收口"这一条仍未接线。收口之后要能回答两个问题：

  1. **真的经由 MCP 吗？** —— 不是"我 import 了 mcp 这个包"，
     而是"**一次真实的会话往返**里能列出工具、能调用、能拿到结构化结果"。
     所以这里跑的是 `mcp.shared.memory` 的内存流会话（协议层是真的，只是不跨进程）。
  2. **有没有旁路？** —— 除了 MCP server 模块，**没有任何地方** import `OpsToolBox`。
     有别的路直接拿到那个类去调，收口就是形式主义。这条用**源码扫描**守（离线、可变异）。

⚠️ 为什么不用 stdio 子进程做测试：那要起进程 + 接管道，在受限沙箱里可能直接不可用
   （见项目环境说明）。内存流把**协议往返**留下来、把**进程边界**去掉，
   对一个单元测试来说是更合适的取舍；真正的 stdio 入口由 `scripts/ops_mcp_server.py` 提供。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from rca.mcp_server import build_ops_server, registered_tool_names
from rca.policy import PolicyEngine, Verb
from rca.tools_ops import OpsToolBox, ops_tool_names

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def workspace() -> Path:
    """唯一工作区。**不清理**（与其它 policy 用例一致：证据留痕优先）。"""
    ws = ROOT / "runs" / "_mcp_tests" / f"t-{uuid.uuid4().hex[:8]}"
    (ws / "allowed").mkdir(parents=True)
    (ws / "quarantine").mkdir(parents=True)
    return ws


@pytest.fixture()
def engine(workspace: Path) -> PolicyEngine:
    return PolicyEngine(
        allowed_roots=[workspace / "allowed"],
        quarantine_root=workspace / "quarantine",
        audit_path=workspace / "audit.ndjson",
        grant_store=workspace / "grants.json",   # 授权库放在授权根**之外**
        quarantine_ttl_s=600,
    )


def _server(engine: PolicyEngine):
    """造一个绑到测试工作区的 ops MCP server（世界用假地址，不真连）。"""
    box = OpsToolBox(engine, world={"order": "http://fake-order"}, actor="mcp-test")
    return build_ops_server(toolbox=box)


async def _with_session(server, fn):
    """在一个**真实的 MCP 会话**里执行 `fn(session)`（内存流传输）。"""
    low = server._lowlevel_server          # ⚠️ 私有属性：见下方那条守卫用例
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
                    return await fn(session)
                finally:
                    tg.cancel_scope.cancel()


def _payload(result) -> dict:
    """把 `CallToolResult` 变成 dict（结构化字段优先，其次首个文本块里的 JSON）。"""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except Exception:  # noqa: BLE001
                return {"text": text}
    return {}


# --------------------------------------------------------------------------- #
# 1. 工具面：MCP 上暴露的**就是** ops 那三个（不多不少）
# --------------------------------------------------------------------------- #
def test_the_mcp_server_exposes_exactly_the_ops_tools(engine: PolicyEngine) -> None:
    server = _server(engine)

    names = asyncio.run(registered_tool_names(server))

    assert names == ops_tool_names(), f"MCP 工具面与 ops 工具集不一致：{names}"
    assert "grant" not in names, "MCP 工具面里不许有签发授权的能力"


def test_the_sdk_still_exposes_the_lowlevel_server_our_tests_drive() -> None:
    """守卫我们依赖的**私有**属性：`MCPServer._lowlevel_server`。

    内存在会话要靠它把协议层跑起来。它是私有 API —— 一旦 SDK 改了名字，
    那些会话用例会变成"莫名其妙地报 AttributeError"。这条守卫让失败**说得清楚**：
    要么换 API、要么改用 stdio/HTTP 传输，而不是让人以为业务逻辑坏了。
    """
    from mcp.server import MCPServer

    assert hasattr(MCPServer(name="x"), "_lowlevel_server"), (
        "mcp SDK 换了内部结构：内存流会话用例需要改用公开传输（stdio/HTTP）"
    )


# --------------------------------------------------------------------------- #
# 2. deny-first **穿过 MCP 依然成立**（且判定是客户端看得见的结构化结果）
# --------------------------------------------------------------------------- #
def test_deny_first_holds_through_a_real_mcp_session(engine: PolicyEngine, workspace: Path) -> None:
    server = _server(engine)
    target = workspace / "allowed" / "evidence.json"
    target.write_text("关键证据", encoding="utf-8")

    async def call(session: ClientSession):
        return await session.call_tool("delete_artifact", {"path": str(target)})

    result = asyncio.run(_with_session(server, call))
    payload = _payload(result)

    assert payload.get("allowed") is False, f"经 MCP 调用居然放行了：{payload}"
    assert "默认拒绝" in payload.get("reason", ""), payload
    assert payload.get("suggestion"), "拒绝必须带建议（否则模型只能瞎猜）"
    assert target.exists(), "★ 判定说拒绝了，但文件没了 —— 这才是事故"


def test_delete_through_mcp_with_a_grant_becomes_reversible(
    engine: PolicyEngine, workspace: Path
) -> None:
    server = _server(engine)
    target = workspace / "allowed" / "keep.json"
    payload_text = '{"答案": 42}'
    target.write_text(payload_text, encoding="utf-8")
    grant = engine.grant(verb=Verb.DELETE, path_prefix=workspace / "allowed", ttl_s=60)

    async def delete_then_restore(session: ClientSession):
        deleted = _payload(await session.call_tool(
            "delete_artifact", {"path": str(target), "grant_id": grant.grant_id}))
        restored = _payload(await session.call_tool(
            "restore_artifact", {"quarantine_id": deleted.get("quarantine_id", "")}))
        return deleted, restored

    deleted, restored = asyncio.run(_with_session(server, delete_then_restore))

    assert deleted.get("allowed") is True, deleted
    assert deleted.get("quarantine_id"), "必须返回隔离区 id（否则没法还原）"
    assert restored.get("allowed") is True, restored
    assert target.exists() and target.read_text(encoding="utf-8") == payload_text


# --------------------------------------------------------------------------- #
# 3. 没有旁路：除 MCP server 之外，谁都不许直接碰 OpsToolBox
# --------------------------------------------------------------------------- #
# 允许直接使用 `OpsToolBox` 的**全部**位置（白名单，逐条给理由）。
# 不在表里的地方想执行 ops 动作 —— 只能走 MCP。
_OPS_TOOLBOX_ALLOWED = {
    "src/rca/tools_ops.py": "它自己的定义",
    "src/rca/mcp_server.py": "唯一被允许把它暴露成 MCP 工具的地方（Agent 侧通道）",
    "scripts/ops.py": "**人**用的 CLI（授权只能由人发，所以人必须有一条自己的门）",
}


def test_only_the_two_sanctioned_doors_reach_the_ops_toolbox() -> None:
    """**"无旁路"的精确含义**：执行 ops 动作只剩两条**被点名的**门。

        · `scripts/ops_mcp_server.py` + `src/rca/mcp_server.py` —— **Agent 侧的唯一通道**（MCP）
        · `scripts/ops.py`                                     —— **人**的 CLI

    为什么人那条门要留着：授权只能由人签发（`PolicyEngine.grant` 的注释），
    要是连 CLI 都封掉，人就**没法**做任何不可逆操作了 —— 那不是安全，是瘫痪。
    但它必须**被点名**：白名单写在用例里，多出第三条就会变红。

    这条是"收口"在代码层面的落点：**不是承诺，是扫出来的事实。**
    （`tests/` 里当然允许 import —— 用例要构造测试用的箱子。）
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in _OPS_TOOLBOX_ALLOWED:
            continue
        if "OpsToolBox" in path.read_text(encoding="utf-8", errors="replace"):
            offenders.append(rel)

    assert not offenders, (
        f"这些文件绕过两条门直接使用 OpsToolBox：{offenders}\n"
        f"（要新增一条门，请连同理由一起加进 _OPS_TOOLBOX_ALLOWED —— 白名单必须是显式的）"
    )


def _print_calls(path: Path) -> list[int]:
    """文件里**真的调用了 print** 的行号（用 AST，不被文档字符串里的字面量骗到）。

    ⚠️ 第一版是纯文本 `"print(" in text` —— 结果被我**自己 docstring 里**那句
    "这里绝对不能 print(" 触发了。**文本匹配 ≠ 代码事实**（同族：#48 的词形匹配）。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]


def test_the_mcp_entry_points_never_print() -> None:
    """stdout 是 MCP 的**协议通道** —— 入口里一句 print 就能毁掉整个会话。

    这类失败最讨厌的地方是**报错与原因无关**（客户端收到垃圾 → 报解析错），
    所以宁可写成一条会变红的静态检查（而且用 AST，不用文本匹配）。
    """
    for rel in ("scripts/ops_mcp_server.py", "src/rca/mcp_server.py", "src/rca/mcp_client.py"):
        lines = _print_calls(ROOT / rel)
        assert not lines, (
            f"{rel} 第 {lines} 行调用了 print —— stdout 是 MCP 的协议通道。\n"
            f"诊断信息请走工具返回值（OpsResult.detail）或审计日志。"
        )
