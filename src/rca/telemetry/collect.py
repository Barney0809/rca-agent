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

def collect_metrics(
    services: tuple[str, ...] = DEFAULT_SERVICES,
    timeout_s: float = 10.0,
) -> dict[str, dict[str, float]]:
    """采集三个服务的 /metrics，返回 {服务名: {序列名: 值}}。"""
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
            except Exception as exc:  # noqa: BLE001
                out[svc] = {"__error__": 0.0}
                out[svc]["__error_message__"] = 0.0
                _ = exc
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
