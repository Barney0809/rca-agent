"""
流量生成器 —— 给被诊断系统施加持续负载，产生足够多的日志。

============================ 为什么需要它 ============================

需求假设 A1 说：单次故障的原始遥测必须**远超单个上下文容量**。

为什么这是生死线：
    如果一次故障只有 200 行日志，单 Agent 一次就能读完 —— 那就没有
    "多 Agent"的必要了。只有数据量足够大、单 Agent 必须做摘要时，
    摘要漏掉关键信号（比如"14 分钟前有一次配置变更"）才会真实发生。

所以本脚本的任务不是"压测性能"，而是**把日志量堆到目标区间**。

============================ 给 Python 新手的说明 ============================

并发模型：**固定数量的 worker 协程**，而不是"每来一个请求开一个"。

    for i in range(concurrency):
        tasks.append(asyncio.create_task(worker(i)))

对应 Java：相当于固定大小的线程池 —— 但这里是协程，单线程，
靠 `await` 在 IO 等待时切换（比线程轻得多）。

本脚本既能从宿主机跑（用 .venv 里的 httpx），也能在容器里跑。

运行示例：
    python -m world.loadgen.main
    LOADGEN_CONCURRENCY=50 LOADGEN_DURATION_S=30 python -m world.loadgen.main
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

ORDER_URL = os.environ.get("LOADGEN_ORDER_URL", "http://127.0.0.1:8080")
CONCURRENCY = int(os.environ.get("LOADGEN_CONCURRENCY", "50"))
# 默认 40 秒（不是 30）。
# 实测：50 并发 × 30 秒产约 2.95 万行，恰好卡在 3 万目标线下面、没有余量。
# 40 秒约产 3.9 万行，落在目标区间 3–8 万的中段。
DURATION_S = float(os.environ.get("LOADGEN_DURATION_S", "40"))
MAX_REQUESTS = int(os.environ.get("LOADGEN_MAX_REQUESTS", "100000"))
SKUS = os.environ.get("LOADGEN_SKUS", "SKU-001,SKU-002,SKU-003").split(",")

# 统计用。因为是单线程协程模型，普通整数自增就是安全的，不需要锁。
stats = {
    "ok": 0,
    "http_4xx": 0,
    "http_5xx": 0,
    "error": 0,
    "latency_sum_ms": 0.0,
}


async def worker(client: httpx.AsyncClient, deadline: float, stop: asyncio.Event) -> None:
    """一个 worker 协程：循环打请求，直到到时间或达到上限。"""
    while not stop.is_set() and time.perf_counter() < deadline:
        sku = random.choice(SKUS)
        qty = random.randint(1, 3)
        started = time.perf_counter()
        try:
            resp = await client.post(
                f"{ORDER_URL}/orders",
                json={"sku": sku, "qty": qty},
                timeout=20.0,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            stats["latency_sum_ms"] += elapsed_ms
            if resp.status_code == 200:
                stats["ok"] += 1
            elif resp.status_code >= 500:
                # 故障期这是**期望**结果，不是脚本出错
                stats["http_5xx"] += 1
            else:
                stats["http_4xx"] += 1
        except Exception:
            stats["error"] += 1


async def main() -> int:
    print("=" * 70)
    print("  流量生成器")
    print("=" * 70)
    print(f"  目标      {ORDER_URL}/orders")
    print(f"  并发      {CONCURRENCY}")
    print(f"  时长      {DURATION_S}s")
    print(f"  请求上限  {MAX_REQUESTS}")
    print()

    deadline = time.perf_counter() + DURATION_S
    stop = asyncio.Event()

    async with httpx.AsyncClient() as client:
        tasks = [asyncio.create_task(worker(client, deadline, stop)) for _ in range(CONCURRENCY)]

        # 监控循环：每 5 秒报一次进度，并在超过请求上限时叫停
        started = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(5)
            total = sum(stats.values()) - int(stats["latency_sum_ms"] > 0)
            total = stats["ok"] + stats["http_4xx"] + stats["http_5xx"] + stats["error"]
            elapsed = time.perf_counter() - started
            print(
                f"  [{elapsed:5.1f}s] 已发 {total:6d}  成功 {stats['ok']:6d}  "
                f"5xx {stats['http_5xx']:5d}  错误 {stats['error']:4d}  "
                f"RPS {total / max(elapsed, 0.001):7.1f}"
            )
            if total >= MAX_REQUESTS:
                print("  达到请求上限，停止。")
                stop.set()
            if time.perf_counter() >= deadline:
                stop.set()

        for t in tasks:
            await t

    total = stats["ok"] + stats["http_4xx"] + stats["http_5xx"] + stats["error"]
    wall = time.perf_counter() - started
    avg_ms = stats["latency_sum_ms"] / max(total - stats["error"], 1)

    print()
    print("=" * 70)
    print("  结果")
    print("=" * 70)
    print(f"  总请求      {total}")
    print(f"  成功 (200)  {stats['ok']}")
    print(f"  服务端 5xx  {stats['http_5xx']}   ← 故障期这是期望结果")
    print(f"  客户端 4xx  {stats['http_4xx']}")
    print(f"  网络错误    {stats['error']}")
    print(f"  墙钟        {wall:.1f}s")
    print(f"  平均 RPS    {total / max(wall, 0.001):.1f}")
    print(f"  平均延迟    {avg_ms:.0f}ms")
    print()
    print("  下一步：用下面的命令看日志量，判断是否达到 3 万行目标")
    print("    docker compose logs --no-log-prefix order inventory payment | Measure-Object -Line")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
