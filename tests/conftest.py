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

    #: 三个服务的**全部**旋钮与默认值 —— 必须与 `world/common/runtime.py` 的 `Knobs` 逐字段一致。
    #:
    #: ⚠️ 这里为什么要**逐个列全**（harness-log #65，2026-09-27）：
    #:    原来只列了一部分（order 5/7、inventory 4/7、payment 4/7），
    #:    于是 `reset()` 文档里那句"把三个服务的参数恢复默认"**是假的** ——
    #:    没列到的旋钮会悄悄留在世界里，污染后面的用例。
    #:    （当时每个**故障**的补丁旋钮碰巧都被覆盖，所以看不出问题；一旦有人改动别的旋钮就现形。）
    #:    "覆盖全不全"由一条**离线结构守卫**盯着（`tests/test_offline.py`），不靠人记得。
    DEFAULTS = {
        "order": {"pool_limit": 64, "pool_acquire_timeout_ms": 2000, "slow_op_ms": 0,
                  "leak_mb_per_req": 0.0, "risk_latency_ms": 30, "risk_error_rate": 0.0,
                  "downstream_retries": 1},
        "inventory": {"pool_limit": 64, "pool_acquire_timeout_ms": 2000, "slow_op_ms": 0,
                      "leak_mb_per_req": 0.0, "risk_latency_ms": 30, "risk_error_rate": 0.0,
                      "downstream_retries": 1},
        "payment": {"pool_limit": 64, "pool_acquire_timeout_ms": 2000, "slow_op_ms": 0,
                    "leak_mb_per_req": 0.0, "risk_latency_ms": 30, "risk_error_rate": 0.0,
                    "downstream_retries": 1},
    }

    def __init__(self) -> None:
        self.client = httpx.Client(timeout=30.0)

    # ---- 业务动作 ----
    def place_order(self, sku: str = "SKU-001", qty: int = 1) -> httpx.Response:
        return self.client.post(f"{ORDER_URL}/orders", json={"sku": sku, "qty": qty})

    # ---- 故障注入 ----
    def inject(self, service: str, patch: dict, *, by: str = "") -> dict:
        """改世界的旋钮。`by` 是**自报来源**（ADR-0009）：世界只能记下它，
        事后才回答得了"是谁改的"（#65 就卡在这里）。"""
        payload = dict(patch)
        if by:
            payload["by"] = by
        r = self.client.post(f"{SERVICES[service]}/_inject", json=payload)
        r.raise_for_status()
        return r.json()

    def inject_history(self, service: str) -> list[dict]:
        """读**带外**注入历史（ADR-0009）—— Agent 看不到这条路径，排障用。"""
        return self.client.get(f"{SERVICES[service]}/_inject_history").json()["records"]

    def knobs(self, service: str) -> dict:
        return self.client.get(f"{SERVICES[service]}/_knobs").json()

    def snapshot(self) -> dict:
        """三个服务当前的旋钮 —— 失败时把它打出来，别让复盘靠猜。"""
        return {svc: self.knobs(svc) for svc in SERVICES}

    def reset(self) -> None:
        """把三个服务的参数恢复默认。

        测试凡是改过参数，**必须**在收尾时调用它，否则会污染后续测试 ——
        这类"测试之间互相影响"的问题排查起来极费时间。

        ⚠️ 恢复的是 `DEFAULTS` 里的**全部**旋钮（不是"我这次改过的那几个"）：
        只恢复一部分，等于把"我没动过的那些"交给上一个用例的良心。
        """
        for service, patch in self.DEFAULTS.items():
            self.inject(service, patch, by="tests:clean_world.reset")

    def dirty_knobs(self) -> dict:
        """哪些旋钮**不在默认值**上（失败时一眼看出世界被谁弄脏了）。"""
        out: dict[str, dict] = {}
        for service, defaults in self.DEFAULTS.items():
            now = self.knobs(service)
            off = {k: v for k, v in now.items() if k in defaults and v != defaults[k]}
            if off:
                out[service] = off
        return out

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
