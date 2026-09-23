"""
集成回归用例 —— 需要被诊断系统在运行。

    docker compose up -d
    uv run pytest tests/test_world.py

若系统没起，整个文件会被跳过（而不是失败）—— 离线用例仍可跑。
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter

import httpx
import pytest

ORDER_URL = os.environ.get("WORLD_ORDER_URL", "http://127.0.0.1:8080")
PAYMENT_URL = os.environ.get("WORLD_PAYMENT_URL", "http://127.0.0.1:8082")


async def _orders(n: int, concurrency: int = 20) -> list[int]:
    """并发打 n 个下单请求，返回状态码列表。

    并发才有意义：连接池耗尽这类问题只在并发下暴露 ——
    串行请求永远是"一次一个"，池再小也用不满。
    """
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=30.0) as client:
        async def one() -> int:
            async with sem:
                try:
                    r = await client.post(
                        f"{ORDER_URL}/orders", json={"sku": "SKU-001", "qty": 1}
                    )
                    return r.status_code
                except Exception:
                    return -1

        return list(await asyncio.gather(*[one() for _ in range(n)]))


async def _latency_ms(sample: int = 5) -> float:
    """串行打几个请求，量平均延迟（用于正向对照）。"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        total = 0.0
        import time
        for _ in range(sample):
            t0 = time.perf_counter()
            await client.post(f"{ORDER_URL}/orders", json={"sku": "SKU-001", "qty": 1})
            total += (time.perf_counter() - t0) * 1000
        return total / sample


# ================================================================
# 正向对照：先证明"注入真的生效"，否则后面的断言可能是假绿
# ================================================================

async def test_injection_actually_takes_effect(clean_world):
    """元测试：注入必须真的改变行为。

    没有这条，"F1 不产生失败"可能只是因为**注入根本没生效** ——
    测试显示绿色，实际什么都没验证。这正是我们踩过的坑（harness-log #5）。
    """
    baseline = await _latency_ms()

    clean_world.inject("payment", {"risk_latency_ms": 500})
    try:
        injected = await _latency_ms()
    finally:
        clean_world.inject("payment", {"risk_latency_ms": 30})

    assert injected > baseline + 300, (
        f"注入 500ms 后延迟应明显上升：基线 {baseline:.0f}ms，注入后 {injected:.0f}ms。"
        f"若两者接近，说明注入没生效 —— 后面所有断言都不可信。"
    )


# ================================================================
# regression_#5：库存必须足够大，且只按请求量扣减
# ================================================================

def test_regression_5_stock_is_not_drained(clean_world):
    """回归 #5 —— 库存耗尽曾伪装成"故障注入失效"。

    历史：种子库存 1000/SKU × 3 被压测抽干。库存一空，请求在 inventory
    就短路、不再往下游走，于是 payment 断流、响应降到 15ms。
    现象看起来像"故障没生效"，实际是库存耗尽，排查约 1 小时。

    这条用例盯住两件事：
      1) 连续请求不会因为库存不足而失败
      2) 库存按每个请求的实际数量精确扣减
    """
    n = 60
    assert clean_world.knobs("order")["risk_latency_ms"] == 30  # 确认干净起点

    first_remaining = None
    last_remaining = None

    for i in range(n):
        resp = clean_world.place_order(qty=1)
        assert resp.status_code == 200, (
            f"第 {i + 1} 个请求就失败了（HTTP {resp.status_code}）：{resp.text[:200]}\n"
            f"最可能的原因：库存种子被调小，或初始化改回了『仅在键不存在时写』。"
        )
        remaining = resp.json()["detail"]["remaining"]
        if first_remaining is None:
            first_remaining = remaining
        last_remaining = remaining

    assert first_remaining - last_remaining == n - 1, (
        f"库存扣减数不对：期望每次扣 1，共 {n - 1}；"
        f"实际 {first_remaining - last_remaining}"
    )

    assert last_remaining > 1000, (
        f"跑完 {n} 个请求后库存只剩 {last_remaining} —— 种子值太小了。"
        f"压测单场景可能上千请求，种子必须留足余量（当前设计值 100 万）。"
    )


# ================================================================
# 池默认值回归：只有 F2 允许触发池耗尽
# ================================================================

async def test_f1_does_not_exhaust_connection_pool(clean_world):
    """回归：F1（下游变慢）**不允许**产生失败。

    历史上的坑：三个服务的连接池默认值是 8 / 4 / 4，而它们都持有连接
    跨网络调下游 —— 于是吞吐 ≈ 池容量 / 下游耗时，池太小。

    结果 F1（只把 payment 延迟调高）会顺带把 order 的池也抽干，
    症状与 F2（池耗尽）混在一起，**评测失去区分度**。

    修法：三个池默认值一律 64（见 docs/adr/0004）。

    这条用例一旦变红，说明有人把池默认值调小了。
    """
    for svc in ("order", "inventory", "payment"):
        limit = clean_world.knobs(svc)["pool_limit"]
        assert limit >= 32, (
            f"{svc} 的连接池默认值只有 {limit}，太小。"
            f"它持有连接跨网络调下游，池小会让 F1/F3 也触发池耗尽，"
            f"与 F2 症状混淆。见 docs/adr/0004。"
        )

    clean_world.inject("payment", {"risk_latency_ms": 400})
    try:
        codes = await _orders(120, concurrency=30)
    finally:
        clean_world.inject("payment", {"risk_latency_ms": 30})

    dist = Counter(codes)
    assert set(codes) == {200}, (
        f"F1 只应造成延迟，不应造成失败；实际状态码分布 {dict(dist)}\n"
        f"若出现 503，通常是某个服务的连接池被调小了。"
    )


async def test_f2_does_exhaust_pool_and_show_503(clean_world):
    """正向对照：F2 **应当**产生失败。

    与上一条配对：上一条断言"F1 不失败"，这一条断言"F2 会失败"。
    两条一起，才说明"池耗尽"这件事是被**有区分地**测出来的，
    而不是"某些请求碰巧失败"。
    """
    clean_world.inject("order", {"pool_limit": 2, "pool_acquire_timeout_ms": 400})
    clean_world.inject("payment", {"risk_latency_ms": 800})
    try:
        codes = await _orders(150, concurrency=30)
    finally:
        clean_world.reset()

    dist = Counter(codes)
    failed = sum(v for k, v in dist.items() if k != 200)
    assert failed > 0, (
        f"F2 应当造成大量失败（池容量 2 且下游慢），但一个都没失败：{dict(dist)}"
    )
    assert failed / len(codes) > 0.3, (
        f"F2 的失败比例只有 {failed / len(codes):.0%}，低于预期的 30%；"
        f"状态码分布 {dict(dist)}"
    )
