"""把 ops 工具面**收口到 MCP** —— 这是 FR-2.1「无旁路保证」的落点。

================================================================================
背景：为什么"接线"还不够
================================================================================

D15 把 ops 动作接上了策略执行点（`src/rca/tools_ops.py` + `scripts/ops.py`），
但那时它是**进程内直调** —— README 里"工具面**没有经过 MCP 收口**"这一条仍然是未接线。
本模块把那条线接上：

    Agent / 人  →  **MCP client**  →  **MCP server（本模块）**  →  OpsToolBox  →  PolicyEngine

为什么"经过 MCP"不是形式主义，而是**可核对的性质**：

    工具面收口之后，执行 ops 动作的**唯一**通道就是 MCP server 里的这三个 handler；
    而它们在构造时就绑定了同一个 `PolicyEngine`。
    配套的用例会证明"没有旁路"：**除本模块外，没有任何地方 import `OpsToolBox`**。
    （如果谁绕过 MCP 直接 new 一个 OpsToolBox 去调，那条用例会变红。）

================================================================================
两条硬约束
================================================================================

1. **本模块与它的入口脚本里绝对不能 `print`。**
   MCP 的 stdio 传输把 **stdout 当作协议通道** —— 一句调试打印就会污染协议、
   让 client 端收到垃圾而报一个与真正原因无关的错。
   所以这里**没有任何输出**；诊断信息走 `OpsResult.detail`（结构化返回）或审计日志。
   有一条用例守着"这两个文件里不许出现 print("。

2. **不提供 grant 工具**（与 `tools_ops.py` 一致）。
   授权只能由人通过 CLI 发 —— MCP 工具面里没有开门的钥匙。

================================================================================
仍然未做的部分（诚实标注）
================================================================================

"工具面完全收口"指的是**ops 工具**这一族；三个**诊断**专员用的仍然是只读工具，
它们本来就不可逆、也没有经过 MCP。要不要把它们也搬进 MCP 是另一件事
（见 `docs/adr/0001`：优先做"需要的能力"，不做"技术栈好看"）。
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from rca.ops_runtime import build_engine
from rca.tools_ops import WORLD_SERVICES, OpsToolBox, ops_tool_names

SERVER_NAME = "rca-ops"
SERVER_INSTRUCTIONS = (
    "被诊断世界的运维动作（ops）。三个工具都会经过策略执行点："
    "set_knobs 可逆、delete_artifact 不可逆（默认拒绝，需人授权）、restore_artifact 用于还原。"
    "**授权只能由人签发** —— 本服务不提供任何申请/签发授权的工具。"
)


def build_ops_server(
    *,
    world: dict[str, str] | None = None,
    actor: str = "mcp-client",
    trace_id: str = "",
    toolbox: OpsToolBox | None = None,
) -> MCPServer:
    """构造 ops 的 MCP server。

    参数 `toolbox` 只为测试注入（可以直接塞一个用假世界/假引擎的箱子）；
    生产路径不传它，走 `build_engine()` + 真实世界地址。
    """
    server: MCPServer = MCPServer(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    ops = toolbox or OpsToolBox(
        build_engine(actor=actor),
        world=world or WORLD_SERVICES,
        actor=actor,
        trace_id=trace_id,
    )

    @server.tool(
        name="set_knobs",
        description=(
            "改变被诊断世界的旋钮（例如调高某个服务的延迟），用于做受控实验。"
            "该动作可逆，会经过策略执行点并写入审计日志。"
        ),
    )
    def set_knobs(service: str, knobs: dict[str, Any]) -> dict:
        return ops.set_knobs(service, knobs).to_dict()

    @server.tool(
        name="delete_artifact",
        description=(
            "删除一个诊断产物。**这是不可逆操作：默认会被拒绝**，"
            "必须有覆盖它的显式授权（由人签发）；即使获准，也只是移入隔离区（可还原）。"
        ),
    )
    def delete_artifact(path: str, grant_id: str | None = None, note: str = "") -> dict:
        return ops.delete_artifact(path, grant_id=grant_id, note=note).to_dict()

    @server.tool(
        name="restore_artifact",
        description="把之前移入隔离区的产物还原回来（不需要授权）。",
    )
    def restore_artifact(quarantine_id: str, to: str | None = None) -> dict:
        return ops.restore_artifact(quarantine_id, to=to).to_dict()

    return server


async def registered_tool_names(server: MCPServer) -> set[str]:
    """这个 server 上注册了哪些工具（给用例核对"面"是否与 ops 工具一致）。

    ⚠️ `MCPServer.list_tools()` 是 **async** 的 —— 这一点看签名看不出来
       （`inspect.signature` 不显示 async），我第一次探针写出了
       `'coroutine' object is not iterable`。**API 必须调用过才算摸清。**
    """
    return {t.name for t in await server.list_tools()}


__all__ = [
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "build_ops_server",
    "ops_tool_names",
    "registered_tool_names",
]
