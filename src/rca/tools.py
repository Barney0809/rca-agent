"""
工具层：Agent 能看到什么、能做什么。

============================ 三个信息源，严格隔离 ============================

按需求 FR-1.3，Agent 的证据只有三路：

    日志  query_logs     被诊断系统三个服务的日志（已降维）
    指标  query_metrics  三个服务的 /metrics（已筛选）
    变更  get_changes    故障注入器写的变更事件

**没有第四路。** 特别是：

    ❌ 不暴露 scenario.json（那是标准答案）
    ❌ 不暴露 /_knobs（那是被改了哪些参数 —— 等于答案的一半）
    ❌ 不给任何文件系统工具（那是策略执行点的职责，见 src/rca/policy/）

============================ 为什么 baseline 与多 Agent 用同一套工具 ============================

D5 要跑一个**单 Agent baseline**，之后 D6/D7 做多 Agent。
两者**必须用完全相同的工具面**，否则对照实验就不成立 ——

    如果 baseline 工具少（信息不足），赢的是"信息量"而不是"协作结构"
    如果 baseline 工具多（能看答案），那它赢是理所当然的

所以这里刻意做成"中性"的：同一套工具，谁都能用。
**唯一的变量是协作结构本身。**
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .telemetry.models import ReducedView
from .telemetry.reduce import reduce_logs


@dataclass
class RunContext:
    """一次诊断运行的全部可用数据。

    ⚠️ `scenario` 字段是**给评测用的**（含标准答案），
       任何工具都不得把它暴露出去。见 ToolBox 的实现。
    """

    run_id: str
    log_view: ReducedView
    metrics: dict[str, dict[str, float]]
    changes: list[dict]
    scenario: dict = field(default_factory=dict, repr=False)

    # 工具调用统计（成本之外的另一维：步数）
    tool_calls: int = 0
    tool_log: list[dict] = field(default_factory=list)

    @classmethod
    def from_run_dir(
        cls,
        run_dir: Path,
        *,
        services: tuple[str, ...] = ("order", "inventory", "payment"),
    ) -> RunContext:
        """从场景目录装载（走 D3 的采集层）。"""
        from .telemetry.collect import (
            collect_changes,
            collect_logs,
            collect_metrics,
            load_scenario,
        )

        records, unparsed = collect_logs(run_dir, services)
        view = reduce_logs(records)
        view.unparsed_lines = unparsed
        return cls(
            run_id=run_dir.name,
            log_view=view,
            metrics=collect_metrics(services),
            changes=collect_changes(run_dir),
            scenario=load_scenario(run_dir),
        )


# ================================================================
# 工具实现
# ================================================================

def _fmt_templates(view: ReducedView, *, level=None, service=None,
                   keyword=None, limit=40, with_samples=True) -> str:
    picked = []
    for t in view.templates:
        if level and t.level != level.upper():
            continue
        if service and t.service != service:
            continue
        if keyword and keyword not in t.template:
            continue
        picked.append(t)

    # 按严重级别 + 次数排序（ERROR 优先）——与降维层同一套优先级
    order = {"ERROR": 0, "CRITICAL": 0, "WARNING": 1}
    picked.sort(key=lambda t: (order.get(t.level, 2), -t.count))

    lines: list[str] = []
    for t in picked[:limit]:
        lines.append(
            f"[{t.service}] {t.level} ×{t.count} "
            f"{t.first_ts:%H:%M:%S}→{t.last_ts:%H:%M:%S}  {t.template}"
        )
        if with_samples:
            for s in t.samples[:1]:
                lines.append(f"    例：{s}")
    if len(picked) > limit:
        lines.append(f"…还有 {len(picked) - limit} 个模板被省略（可用 keyword/service 收窄）")
    if not picked:
        lines.append("（没有匹配的日志模板）")
    return "\n".join(lines)


def query_logs(
    ctx: RunContext,
    *,
    level: str | None = None,
    service: str | None = None,
    keyword: str | None = None,
    limit: int = 40,
) -> str:
    """查日志。返回降维后的模板视图（不是原始行）。"""
    view = ctx.log_view
    head = [
        f"# 日志概览  窗口 {view.window_start:%H:%M:%S}–{view.window_end:%H:%M:%S}"
        if view.window_start and view.window_end
        else "# 日志概览",
        "按服务：" + "  ".join(f"{k}={v}" for k, v in sorted(view.by_service.items())),
        "按级别：" + "  ".join(f"{k}={v}" for k, v in sorted(view.by_level.items())),
    ]
    if view.dropped_info_lines:
        head.append(f"（已按频次裁剪 {view.dropped_info_lines} 行 INFO；ERROR/WARNING 一条未裁）")

    hot = [b for b in view.timeline if b.errors or b.warnings]
    if hot:
        head.append("")
        head.append("## 异常时间线")
        for b in hot:
            parts = []
            if b.errors:
                parts.append(f"E{b.errors}")
            if b.warnings:
                parts.append(f"W{b.warnings}")
            head.append(f"  {b.minute}  " + " ".join(parts))

    head.append("")
    head.append("## 消息模板")
    head.append(_fmt_templates(view, level=level, service=service,
                               keyword=keyword, limit=limit))
    return "\n".join(head)


# 指标白名单：只有可能指示故障的序列才暴露。
# 全量指标有几十条，绝大多数在一次故障里没有意义，白白占上下文。
_INTERESTING_METRICS = (
    "requests_total",
    "handler_duration_ms",
    "downstream_duration_ms",
    "downstream_calls_total",
    "downstream_exhausted_total",
    "pool_in_flight",
    "pool_limit",
    "risk_control_duration_ms",
    "stock_level",
    "leak_bytes",
)


def query_metrics(
    ctx: RunContext,
    *,
    service: str | None = None,
    keyword: str | None = None,
) -> str:
    """查指标。只返回白名单内、且有诊断价值的序列。"""
    lines: list[str] = []
    for svc, series in sorted(ctx.metrics.items()):
        if service and svc != service:
            continue
        picked = []
        for name, value in sorted(series.items()):
            base = name.split("{")[0]
            if base not in _INTERESTING_METRICS:
                continue
            if keyword and keyword not in name:
                continue
            picked.append((name, value))
        if not picked:
            continue
        lines.append(f"[{svc}]")
        for name, value in picked:
            lines.append(f"  {name} = {value:g}")
    return "\n".join(lines) if lines else "（没有匹配的指标）"


def get_changes(ctx: RunContext, *, keyword: str | None = None) -> str:
    """查变更事件。**这是唯一可能"看起来像答案其实是陷阱"的一路。**"""
    if not ctx.changes:
        return "（本时段没有任何配置变更记录）"
    lines = ["# 变更事件"]
    for c in ctx.changes:
        if keyword and keyword not in json.dumps(c, ensure_ascii=False):
            continue
        lines.append(
            f"  {c.get('ts')}  {c.get('target')}.{c.get('key')}: "
            f"{c.get('from')} → {c.get('to')}  by={c.get('by')}"
        )
    return "\n".join(lines)


# ================================================================
# 工具箱：给 LLM 的工具声明 + 统一调用入口
# ================================================================

TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "query_logs",
            "description": (
                "查询被诊断系统的日志（已降维为消息模板）。"
                "可按严重级别、服务名、关键词过滤。"
                "注意：这是模板级视图，同一个模板的多次出现已合并计数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "enum": ["ERROR", "WARNING", "INFO"],
                              "description": "只保留该级别"},
                    "service": {"type": "string", "enum": ["order", "inventory", "payment"],
                                "description": "只保留该服务"},
                    "keyword": {"type": "string", "description": "模板内容包含该关键词"},
                    "limit": {"type": "integer", "description": "最多返回多少个模板，默认 40"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_metrics",
            "description": (
                "查询被诊断系统三个服务的运行指标"
                "（请求数、处理耗时、下游调用耗时、连接池占用、库存、泄漏字节等）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "enum": ["order", "inventory", "payment"]},
                    "keyword": {"type": "string", "description": "指标名包含该关键词"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_changes",
            "description": (
                "查询本时段的配置变更记录（谁在什么时候把哪个参数从什么改成了什么）。"
                "变更记录只是线索之一，**有变更不等于它就是根因**。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"keyword": {"type": "string"}},
                "required": [],
            },
        },
    },
]


class ToolBox:
    """统一的工具调用入口，并记录每一次调用（用于统计"步数"）。"""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    @staticmethod
    def specs() -> list[dict]:
        return TOOL_SPECS

    def call(self, name: str, arguments_json: str) -> str:
        """执行一个工具调用，返回给模型看的文本。

        ⚠️ 参数来自模型，**可能是坏 JSON** —— 必须兜住并返回可读的错误，
           让模型自己纠正。直接把异常抛出去会让整轮失败。
        """
        try:
            args = json.loads(arguments_json) if arguments_json.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("参数必须是一个 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            self._record(name, arguments_json, ok=False)
            return (
                f"参数解析失败：{exc}\n"
                f'请重新调用，arguments 必须是合法 JSON 对象，例如 {{"level": "ERROR"}}'
            )

        handlers = {
            "query_logs": query_logs,
            "query_metrics": query_metrics,
            "get_changes": get_changes,
        }
        fn = handlers.get(name)
        if fn is None:
            self._record(name, arguments_json, ok=False)
            return f"未知工具：{name}。可用工具：{', '.join(handlers)}"

        try:
            out = fn(self.ctx, **args)
        except TypeError as exc:
            self._record(name, arguments_json, ok=False)
            return f"参数不合适：{exc}"
        except Exception as exc:  # noqa: BLE001
            self._record(name, arguments_json, ok=False)
            return f"工具执行出错：{type(exc).__name__}: {exc}"

        self._record(name, arguments_json, ok=True)
        return out

    def _record(self, name: str, arguments_json: str, *, ok: bool) -> None:
        self.ctx.tool_calls += 1
        self.ctx.tool_log.append({"tool": name, "args": arguments_json, "ok": ok})
