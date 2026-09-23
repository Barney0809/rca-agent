"""
pytest 公共装置（fixture）。

============================ 给 Python 新手的说明 ============================

pytest 里的 fixture 相当于 JUnit 的 @BeforeEach / @BeforeAll，
但更灵活：它是个"按需注入"的函数。

    @pytest.fixture(scope="session")
    def world():
        ...                      # 准备
        yield 对象                # 交给测试用
        ...                      # 收尾

测试函数只要把 fixture 名字写成参数，pytest 就会自动传进来：

    def test_something(world):
        world.order(...)

`scope="session"` 表示整个测试会话只建一次（对应 JUnit 的 @BeforeAll）。
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

ORDER_URL = os.environ.get("WORLD_ORDER_URL", "http://127.0.0.1:8080")
INVENTORY_URL = os.environ.get("WORLD_INVENTORY_URL", "http://127.0.0.1:8081")
PAYMENT_URL = os.environ.get("WORLD_PAYMENT_URL", "http://127.0.0.1:8082")

SERVICES = {"order": ORDER_URL, "inventory": INVENTORY_URL, "payment": PAYMENT_URL}


class World:
    """对被诊断系统的一层薄封装，让测试读起来像在说业务。"""

    def __init__(self) -> None:
        self.client = httpx.Client(timeout=30.0)

    # ---- 业务动作 ----
    def place_order(self, sku: str = "SKU-001", qty: int = 1) -> httpx.Response:
        return self.client.post(f"{ORDER_URL}/orders", json={"sku": sku, "qty": qty})

    # ---- 故障注入 ----
    def inject(self, service: str, patch: dict) -> dict:
        r = self.client.post(f"{SERVICES[service]}/_inject", json=patch)
        r.raise_for_status()
        return r.json()

    def knobs(self, service: str) -> dict:
        return self.client.get(f"{SERVICES[service]}/_knobs").json()

    def reset(self) -> None:
        """把三个服务的参数恢复默认。

        测试凡是改过参数，**必须**在收尾时调用它，否则会污染后续测试 ——
        这类"测试之间互相影响"的问题排查起来极费时间。
        """
        self.inject("order", {
            "pool_limit": 64, "pool_acquire_timeout_ms": 2000, "slow_op_ms": 0,
            "leak_mb_per_req": 0.0, "downstream_retries": 1,
        })
        self.inject("inventory", {
            "pool_limit": 64, "pool_acquire_timeout_ms": 2000, "slow_op_ms": 0,
            "downstream_retries": 1,
        })
        self.inject("payment", {
            "pool_limit": 64, "pool_acquire_timeout_ms": 2000,
            "risk_latency_ms": 30, "risk_error_rate": 0.0,
        })

    def close(self) -> None:
        self.client.close()


@pytest.fixture(scope="session")
def world():
    """被诊断系统。若没有运行就跳过（而不是失败）—— 离线测试仍可跑。"""
    probe = httpx.Client(timeout=5.0)
    try:
        probe.get(f"{ORDER_URL}/health").raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"被诊断系统未运行（{exc}）；先执行: docker compose up -d")
    finally:
        probe.close()

    w = World()
    w.reset()                     # 进来先归零，避免上一轮残留
    try:
        yield w
    finally:
        w.reset()                 # 走的时候也归零
        w.close()


@pytest.fixture
def clean_world(world):
    """需要改动参数、但希望每个用例互不影响的场景用这个。

    对应 JUnit 的 @BeforeEach：**每个用例前**都重置一次。
    """
    world.reset()
    yield world
    world.reset()


def wait_until(predicate, timeout_s: float = 10.0, interval_s: float = 0.2) -> bool:
    """轮询等待条件成立。用于等指标/日志落盘。"""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return False
