"""
采集层：从三个数据源把原始数据取回来。

============================ 三个数据源 ============================

按需求 FR-1.3，Agent 有三个**互相隔离**的信息源：

    1) 日志    每个服务 stdout 的文本日志       → LogsAgent 只能看这个
    2) 指标    每个服务的 /metrics              → MetricsAgent 只能看这个
    3) 变更    故障注入器写的 changes.ndjson    → ChangeAgent 只能看这个

**隔离是刻意的**：每一路都有盲区，谁都单独定不了根因。
这正是不需要"多 Agent"的证明的反面 —— 也正是多 Agent 存在的理由。

============================ 两种日志来源 ============================

优先级：
    1) runs/<run_id>/logs/<service>.log   —— 场景运行时抓下来的（**首选**）
    2) docker compose logs                —— 兜底（比如手工排查时）

之所以首选抓下来的文件：`docker compose logs` 返回的是**自容器启动以来的累计日志**，
每跑一个场景就会越来越慢，而且需要按时间窗口再过滤一次。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx

from .models import LogRecord
from .parse import parse_log_line, parse_prometheus

DEFAULT_SERVICES = ("order", "inventory", "payment")
SERVICE_URLS = {
    "order": os.environ.get("WORLD_ORDER_URL", "http://127.0.0.1:8080"),
    "inventory": os.environ.get("WORLD_INVENTORY_URL", "http://127.0.0.1:8081"),
    "payment": os.environ.get("WORLD_PAYMENT_URL", "http://127.0.0.1:8082"),
}


def project_root() -> Path:
    """从本文件位置往上找到项目根（含 pyproject.toml 的那一层）。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parents[3]


# ---------------------------------------------------------------- 日志

def collect_logs(
    run_dir: Path | None = None,
    services: tuple[str, ...] = DEFAULT_SERVICES,
) -> tuple[list[LogRecord], int]:
    """采集日志，返回 (结构化记录, 未解析行数)。

    未解析行数必须**显式返回**，不能静默丢弃 ——
    否则"降维后没看到某个信号"就无法区分是"信号不存在"还是"解析漏了"。
    """
    records: list[LogRecord] = []
    unparsed = 0

    for svc in services:
        text = _read_log_text(run_dir, svc)
        for line in text.splitlines():
            rec = parse_log_line(line, default_service=svc)
            if rec is None:
                unparsed += 1
            else:
                records.append(rec)

    records.sort(key=lambda r: (r.ts, r.service))
    return records, unparsed


def _read_log_text(run_dir: Path | None, service: str) -> str:
    """按优先级取某个服务的日志文本。"""
    if run_dir is not None:
        captured = run_dir / "logs" / f"{service}.log"
        if captured.exists():
            return captured.read_text(encoding="utf-8", errors="replace")

    # 兜底：直接问 docker（累计日志）
    try:
        proc = subprocess.run(
            ["docker", "compose", "logs", "--no-log-prefix", service],
            cwd=str(project_root()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        return proc.stdout
    except Exception:
        return ""


# ---------------------------------------------------------------- 指标
#
# ⚠️⚠️ 指标必须被**场景窗口限定**，不能直接读 live /metrics ⚠️⚠️
#
# Prometheus 的计数器是**自进程启动以来**的累计值。容器不重启，
# 直接读 live 拿到的是**前面所有场景的叠加**。
#
# 2026-09-24 踩过的坑（docs/harness-log.md #12）：
#   F6 场景（只注入内存泄漏）的 Agent 从指标里读到 F5/F4/F2 残留的
#   "3931 次风控错误、2495 次池耗尽"，得出了完全错误的结论，
#   **D5 的全部结论因此作废**。而这一切没有任何报错。
#
# 所以现在的规则是：
#   有 before/after 快照 → 计数器取差值、仪表取结束值（**窗口视图**）
#   没有快照            → 仍然返回 live 值，但**明确标注不可信**
_COUNTER_SUFFIXES = ("_total", "_sum", "_count")


def _is_counter(series_name: str) -> bool:
    """判断是否为计数器（可累加）还是仪表（瞬时值）。

    Prometheus 的命名约定：计数器以 `_total` / `_sum` / `_count` 结尾。
    对应 Java：Micrometer 的 Counter 与 Gauge。
    """
    return series_name.split("{")[0].endswith(_COUNTER_SUFFIXES)


def _load_snapshot(run_dir: Path | None, tag: str) -> dict[str, str] | None:
    if run_dir is None:
        return None
    path = run_dir / f"metrics-{tag}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _window_view(
    before: dict[str, str], after: dict[str, str], services: tuple[str, ...]
) -> dict[str, dict[str, float]]:
    """由两份快照算出窗口视图。

        计数器 → after - before（本窗口内增加了多少）
        仪表   → after 的值（结束时的瞬时状态）

    ⚠️ 计数器若出现负差（说明进程中途重启过），钳到 0 并在 meta 里标记，
       而不是把一个负数当成"发生了负次数的事件"。
    """
    out: dict[str, dict[str, float]] = {}
    restarted: list[str] = []

    for svc in services:
        b = parse_prometheus(before.get(svc, ""))
        a = parse_prometheus(after.get(svc, ""))
        view: dict[str, float] = {}
        for name, after_val in a.items():
            if _is_counter(name):
                delta = after_val - b.get(name, 0.0)
                if delta < 0:
                    restarted.append(f"{svc}:{name}")
                    delta = 0.0
                view[name] = delta
            else:
                view[name] = after_val
        out[svc] = view

    out["__meta__"] = {
        "windowed": 1.0,
        "restarted_series": float(len(restarted)),
    }
    return out


def collect_metrics(
    run_dir: Path | None = None,
    services: tuple[str, ...] = DEFAULT_SERVICES,
    timeout_s: float = 10.0,
) -> dict[str, dict[str, float]]:
    """采集三个服务的指标。

    **优先使用场景快照并返回窗口增量**；没有快照时回退到 live 累计值，
    但会打印醒目告警并在返回里标记 `__meta__.windowed = 0`。

    这个 signature 里的 `run_dir` 不是可选的装饰 —— 少了它就等于回到
    "读脏数据"的老路上（见 harness-log #12）。
    """
    before = _load_snapshot(run_dir, "before")
    after = _load_snapshot(run_dir, "after")

    if before is not None and after is not None:
        return _window_view(before, after, services)

    # ---- 回退：live 累计值 ----
    print(
        "  ⚠️ 警告：找不到指标快照"
        f"（{run_dir}/metrics-before.json / metrics-after.json 缺失）。\n"
        "     将回退到 live /metrics —— 但那是**自容器启动以来的累计值**，\n"
        "     可能包含其他场景的残留，会让结论建立在错误的证据上。\n"
        "     重新跑一次场景即可生成快照：\n"
        "       python scripts/inject_fault.py scenario <F1..F6>"
    )
    out: dict[str, dict[str, float]] = {}
    with httpx.Client(timeout=timeout_s) as client:
        for svc in services:
            base = SERVICE_URLS.get(svc)
            if not base:
                continue
            try:
                resp = client.get(f"{base}/metrics")
                resp.raise_for_status()
                out[svc] = parse_prometheus(resp.text)
            except Exception:  # noqa: BLE001
                out[svc] = {}
    out["__meta__"] = {"windowed": 0.0, "restarted_series": 0.0}
    return out


def collect_knobs(services: tuple[str, ...] = DEFAULT_SERVICES) -> dict[str, dict]:
    """采集三个服务的当前参数。

    ⚠️ 这个**不是**给 Agent 用的数据源 —— 它是答案的一半（能直接看出被改了哪个参数）。
       Agent 绝不能读它。它只供评测与人工排查使用。
    """
    out: dict[str, dict] = {}
    with httpx.Client(timeout=10.0) as client:
        for svc in services:
            base = SERVICE_URLS.get(svc)
            if not base:
                continue
            try:
                out[svc] = client.get(f"{base}/_knobs").json()
            except Exception:
                out[svc] = {}
    return out


# ---------------------------------------------------------------- 变更

def collect_changes(run_dir: Path) -> list[dict]:
    """读取本场景的变更事件日志（Agent 的第三路数据源）。"""
    path = run_dir / "changes.ndjson"
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def load_scenario(run_dir: Path) -> dict:
    """读取场景记录（含标准答案）。**仅供评测与人工对账，Agent 不可读。**"""
    path = run_dir / "scenario.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
