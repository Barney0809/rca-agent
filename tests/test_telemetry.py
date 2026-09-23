"""
遥测层专项用例 —— 重点是**数据源本身的可信度**。

============================ 这一层为什么需要专门的用例 ============================

harness-log #12 的教训：

    我们花了很大力气保证"日志按场景窗口抓取"，
    却**没有对指标做同样的处理** —— 因为日志的窗口化是**显式写出来**的代码，
    而指标的读取是"看起来理所当然"的一行 `GET /metrics`。

    **最危险的地方往往是那些"看起来不需要检查"的地方。**

所以本文件盯住的不是"指标能不能读到"，而是
**"读到的指标是不是本场景的"**。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from rca.telemetry.collect import collect_metrics

ROOT = Path(__file__).resolve().parent.parent


def _workspace() -> Path:
    """唯一工作区。**不清理** —— 与 policy 测试同一原则（见 tests/test_policy.py）。"""
    ws = ROOT / "runs" / "_telemetry_tests" / f"t-{uuid.uuid4().hex[:8]}"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _write_snapshot(run_dir: Path, tag: str, payload: dict[str, str]) -> None:
    (run_dir / f"metrics-{tag}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


# ================================================================
# regression_#12：指标必须是窗口增量，不是累计值
# ================================================================

def test_regression_12_counters_are_window_deltas():
    """回归 #12 —— 计数器必须取**差值**。

    历史：直接读 live /metrics，拿到的是自容器启动以来的累计值。
    F6 场景（只注入内存泄漏）的 Agent 因此读到了 F5/F4/F2 残留的
    "3931 次风控错误"，得出完全错误的结论 —— **D5 的全部结论作废**。

    这条用例构造 before=100 / after=150，断言返回 **50**（增量）而不是 150（累计）。
    一旦有人把窗口化逻辑去掉，它会立刻变红。
    """
    ws = _workspace()
    _write_snapshot(ws, "before", {
        "order": 'requests_total{endpoint="create_order"} 100\n'
                 'pool_in_flight 5\n'
                 'pool_limit 64\n',
    })
    _write_snapshot(ws, "after", {
        "order": 'requests_total{endpoint="create_order"} 150\n'
                 'pool_in_flight 2\n'
                 'pool_limit 64\n',
    })

    metrics = collect_metrics(ws, services=("order",))

    assert metrics["order"]["requests_total{endpoint=\"create_order\"}"] == 50, (
        "计数器必须是窗口增量（150-100=50），不能是累计值 150"
    )
    # 仪表取结束时的瞬时值（不是差值 —— 差值对仪表没有意义）
    assert metrics["order"]["pool_in_flight"] == 2
    assert metrics["order"]["pool_limit"] == 64
    assert metrics["__meta__"]["windowed"] == 1.0


def test_regression_12_counter_restart_is_clamped_not_negative():
    """回归 #12 配套 —— 进程中途重启会让 after < before。

    这时差值会是负数。绝不能把"负数次数"交给 Agent ——
    它会得出"发生了 -80 次请求"这种荒谬结论。
    正确处理：钳到 0，并记进 meta 供排查。
    """
    ws = _workspace()
    _write_snapshot(ws, "before", {"order": "requests_total 500\n"})
    _write_snapshot(ws, "after", {"order": "requests_total 20\n"})

    metrics = collect_metrics(ws, services=("order",))

    assert metrics["order"]["requests_total"] == 0, "负差必须钳到 0"
    assert metrics["__meta__"]["restarted_series"] == 1, "重启事件应当被记下来"


def test_regression_12_missing_snapshot_is_marked_not_silent():
    """回归 #12 最核心的一条 —— **缺快照时必须标注，不许静默给累计值**。

    历史缺陷的本质不是"值算错了"，而是**它没有任何提示**。
    一个静默的错误数据源，比一个报错的数据源危险得多。
    """
    ws = _workspace()          # 故意不写任何快照

    metrics = collect_metrics(ws, services=("order",))

    assert "__meta__" in metrics, "必须返回 meta 让调用方能判断数据是否可信"
    assert metrics["__meta__"]["windowed"] == 0.0, (
        "没有快照时必须明确标注 windowed=0，而不是假装数据是干净的"
    )


def test_regression_12_query_metrics_warns_when_not_windowed():
    """配套：Agent 看到的文本里也要有警告。

    只在内部数据结构里标记是不够的 —— **真正读数据的是模型**，
    所以警告必须出现在它读到的文本里。
    """
    from rca.telemetry.models import ReducedView
    from rca.tools import RunContext, query_metrics

    ctx = RunContext(
        run_id="t",
        log_view=ReducedView(window_start=None, window_end=None, total_lines=0),
        metrics={
            "order": {"requests_total": 999.0},
            "__meta__": {"windowed": 0.0, "restarted_series": 0.0},
        },
        changes=[],
    )
    text = query_metrics(ctx)

    assert "不是按场景窗口统计" in text, f"Agent 读到的文本里必须有警告，实际：{text[:120]}"
    assert "999" in text, "警告之外仍要给出数据（只是标注它可能不干净）"


def test_windowed_metrics_do_not_warn():
    """正向对照：窗口化的数据**不该**出现警告。

    没有这条，"有警告"的断言可能只是"永远有警告"造成的假绿。
    """
    from rca.telemetry.models import ReducedView
    from rca.tools import RunContext, query_metrics

    ctx = RunContext(
        run_id="t",
        log_view=ReducedView(window_start=None, window_end=None, total_lines=0),
        metrics={
            "order": {"requests_total": 42.0},
            "__meta__": {"windowed": 1.0, "restarted_series": 0.0},
        },
        changes=[],
    )
    text = query_metrics(ctx)

    assert "不是按场景窗口统计" not in text
    assert "本时段窗口内的增量" in text
    assert "42" in text


def test_meta_key_is_not_exposed_as_a_metric():
    """`__meta__` 是内部标记，不能被当成一个"服务"暴露给 Agent。"""
    from rca.telemetry.models import ReducedView
    from rca.tools import RunContext, query_metrics

    ctx = RunContext(
        run_id="t",
        log_view=ReducedView(window_start=None, window_end=None, total_lines=0),
        metrics={
            "order": {"requests_total": 1.0},
            "__meta__": {"windowed": 1.0, "restarted_series": 0.0},
        },
        changes=[],
    )
    text = query_metrics(ctx)
    assert "__meta__" not in text
    assert "windowed" not in text
