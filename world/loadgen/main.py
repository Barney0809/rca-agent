"""
流量生成器 —— 给被诊断系统施加持续负载，产生足够多的日志。

============================ 为什么需要它 ============================

需求假设 A1 说：单次故障的原始遥测必须**远超单个上下文容量**。

为什么这是生死线：
    如果一次故障只有 200 行日志，单 Agent 一次就能读完 —— 那就没有
    "多 Agent"的必要了。只有数据量足够大、单 Agent 必须做摘要时，
    摘要漏掉关键信号（比如"14 分钟前有一次配置变更"）才会真实发生。

所以本脚本的任务不是"压测性能"，而是**把日志量堆到目标区间**。

============================ 两个入口 ============================

  1) 命令行：  python -m world.loadgen.main
                （读环境变量，打一次负载，打印报告）

  2) 被调用：  from world.loadgen.main import run_load
                await run_load(concurrency=50, duration_s=40, order_url=...)
                （供 scripts/inject_fault.py 与后续评测框架使用）

============================ 给 Python 新手的说明 ============================

并发模型：**固定数量的 worker 协程**，而不是"每来一个请求开一个"。

    tasks = [asyncio.create_task(worker(...)) for _ in range(concurrency)]

对应 Java：相当于固定大小的线程池 —— 但这里是协程，单线程，
靠 `await` 在 IO 等待时切换（比线程轻得多）。

============================ 实测校准值（别重测）============================

    每请求约产生   7 行日志（跨三个服务）
    50 并发×30 秒  → 4,668 请求 → 29,479 行（差 521 行达标）
    50 并发×40 秒  → 预估约 3.9 万行（默认配置）
    RPS            91 → 155，随时间加速（预热效应）
    日志分布       order 18558 / inventory 12567 / payment 2157
                   （'HTTP Request:' 由 httpx 在调用方打印，下游天然更少）
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx  # noqa: E402

DEFAULT_ORDER_URL = os.environ.get("LOADGEN_ORDER_URL", "http://127.0.0.1:8080")
DEFAULT_CONCURRENCY = int(os.environ.get("LOADGEN_CONCURRENCY", "50"))

# 默认 40 秒（不是 30）。
# 实测：50 并发 × 30 秒产约 2.95 万行，恰好卡在 3 万目标线下面、没有余量。
# 40 秒约产 3.9 万行，落在目标区间 3–8 万的中段。
DEFAULT_DURATION_S = float(os.environ.get("LOADGEN_DURATION_S", "40"))

DEFAULT_MAX_REQUESTS = int(os.environ.get("LOADGEN_MAX_REQUESTS", "200000"))
DEFAULT_SKUS = os.environ.get("LOADGEN_SKUS", "SKU-001,SKU-002,SKU-003").split(",")


def _total(stats: dict) -> int:
    """已发出的请求总数。"""
    return stats["ok"] + stats["http_4xx"] + stats["http_5xx"] + stats["error"]


async def _worker(
    client: httpx.AsyncClient,
    order_url: str,
    skus: list[str],
    deadline: float,
    stop: asyncio.Event,
    stats: dict,
    max_requests: int,
) -> None:
    """一个 worker 协程：循环打请求，直到到时间、达到请求数上限、或被叫停。"""
    while not stop.is_set() and time.perf_counter() < deadline:
        # ⚠️ worker 自己检查请求数上限，不等监控循环。
        #    之前只靠监控循环（每 5 秒醒一次）检查，会超发 50% 以上
        #    （目标 1200 实发 1841），数据量就不可复现了。
        if _total(stats) >= max_requests:
            stop.set()
            return

        started = time.perf_counter()
        try:
            resp = await client.post(
                f"{order_url}/orders",
                json={"sku": random.choice(skus), "qty": random.randint(1, 3)},
                timeout=20.0,
            )
            stats["latency_sum_ms"] += (time.perf_counter() - started) * 1000
            if resp.status_code == 200:
                stats["ok"] += 1
            elif resp.status_code >= 500:
                # 故障期这是**期望**结果，不是脚本出错
                stats["http_5xx"] += 1
            else:
                stats["http_4xx"] += 1
        except Exception:
            stats["error"] += 1


async def run_load(
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    duration_s: float = DEFAULT_DURATION_S,
    order_url: str = DEFAULT_ORDER_URL,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    skus: list[str] | None = None,
    verbose: bool = True,
    poll_s: float = 5.0,
) -> dict:
    """跑一次负载，返回统计结果字典。

    返回值字段：
        total / ok / http_5xx / http_4xx / error
        wall_s / rps / avg_latency_ms
    """
    skus = skus or DEFAULT_SKUS
    stats = {"ok": 0, "http_4xx": 0, "http_5xx": 0, "error": 0, "latency_sum_ms": 0.0}

    def total() -> int:
        return stats["ok"] + stats["http_4xx"] + stats["http_5xx"] + stats["error"]

    if verbose:
        print("=" * 70)
        print("  流量生成器")
        print("=" * 70)
        print(f"  目标      {order_url}/orders")
        print(f"  并发      {concurrency}")
        print(f"  时长      {duration_s}s")
        print(f"  请求上限  {max_requests}")
        print()

    started = time.perf_counter()
    deadline = started + duration_s
    stop = asyncio.Event()

    async with httpx.AsyncClient() as client:
        tasks = [
            asyncio.create_task(
                _worker(client, order_url, skus, deadline, stop, stats, max_requests)
            )
            for _ in range(concurrency)
        ]

        while not stop.is_set():
            await asyncio.sleep(poll_s)
            elapsed = time.perf_counter() - started
            if verbose:
                print(
                    f"  [{elapsed:5.1f}s] 已发 {total():6d}  成功 {stats['ok']:6d}  "
                    f"5xx {stats['http_5xx']:5d}  错误 {stats['error']:4d}  "
                    f"RPS {total() / max(elapsed, 0.001):7.1f}"
                )
            if time.perf_counter() >= deadline:
                stop.set()

        for t in tasks:
            await t

    wall = time.perf_counter() - started
    n = total()
    # 延迟只统计"真的收到了响应"的那些请求（网络错误没有延迟可言）
    responded = n - stats["error"]
    avg_latency = stats["latency_sum_ms"] / responded if responded > 0 else 0.0

    result = {
        **{k: v for k, v in stats.items() if k != "latency_sum_ms"},
        "total": n,
        "wall_s": round(wall, 2),
        "rps": round(n / max(wall, 0.001), 1),
        "avg_latency_ms": round(avg_latency, 1),
    }

    if verbose:
        print()
        print("=" * 70)
        print("  结果")
        print("=" * 70)
        print(f"  总请求      {result['total']}")
        print(f"  成功 (200)  {result['ok']}")
        print(f"  服务端 5xx  {result['http_5xx']}   ← 故障期这是期望结果")
        print(f"  客户端 4xx  {result['http_4xx']}")
        print(f"  网络错误    {result['error']}")
        print(f"  墙钟        {result['wall_s']}s")
        print(f"  平均 RPS    {result['rps']}")
        print(f"  平均延迟    {result['avg_latency_ms']}ms")

    return result


def main() -> int:
    """命令行入口：读环境变量 → 跑一次 → 打印报告。"""
    result = asyncio.run(
        run_load(
            concurrency=DEFAULT_CONCURRENCY,
            duration_s=DEFAULT_DURATION_S,
            order_url=DEFAULT_ORDER_URL,
            max_requests=DEFAULT_MAX_REQUESTS,
            skus=DEFAULT_SKUS,
        )
    )
    print()
    print("  下一步：用下面的命令看日志量，判断是否达到 3 万行目标")
    print("    docker compose logs --no-log-prefix order inventory payment | Measure-Object -Line")
    return 0 if result["error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
