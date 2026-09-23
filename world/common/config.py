"""
配置读取 + Redis 连接池。

============================ 给 Python 新手的说明 ============================

【配置】
Python 里读环境变量是 os.environ（类似 Java 的 System.getenv）。
下面用 os.environ.get("KEY", 默认值) 的形式，
好处是：没配也能跑（有默认值），配了就生效。

⚠️ 本项目所有"可调参数"都做成环境变量，原因有两个：
  1) docker-compose 里可以直接改，不用改代码
  2) D2 的「故障注入」和「配置漂移」故障，就是要靠改这些参数实现

【连接池】
这是被诊断系统的核心机制，也是最重要的一类故障来源。

为什么要"池"？因为建立连接很贵（要握手、要认证）。
所以预先建好 N 个连接反复用，"池"就是这 N 个连接的借还管理。

池的核心矛盾：
    并发请求数 > 池大小时，后来的请求必须【等】别人还回来。
    等太久 → 超时 → 报错。这就是"连接池耗尽"。

所以这个类的关键产出是【等待时长】——它是诊断这类故障的
第一手证据。每一次获取连接，都记录等了多久：

    等 0ms      → 正常
    等 800ms    → 池开始紧张（记 WARNING）
    等 12000ms  → 超时失败（记 ERROR）★ 这就是故障信号

对应的日志会长这样，Agent 要能从中认出问题：

    2026-09-24 03:50:12.123 ERROR [payment] trace=8f3a1c |
        连接池获取超时：等待 12000ms 后放弃（池大小=2，当前在途=2）

=============================================================================
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import redis.asyncio as aioredis


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    """一个服务的全部可调参数。"""

    service: str
    port: int = 8000
    log_level: str = "INFO"

    # --- Redis ---
    redis_url: str = "redis://redis:6379/0"

    # --- 连接池（★ 故障注入的主要着力点）---
    pool_size: int = 8
    pool_acquire_timeout_ms: int = 2000   # 超过这个时长就放弃 → 池耗尽
    pool_warn_ms: int = 200               # 超过这个时长记 WARNING

    # --- 下游服务（本服务要调用的下一个环节）---
    downstream_name: str = ""
    downstream_url: str = ""

    # --- 调用下游的重试策略（★ "配置漂移"故障就改这里）---
    downstream_retries: int = 1
    downstream_timeout_ms: int = 3000
    retry_backoff_ms: int = 100

    # --- 外部依赖（模拟第三方风控，本身就会慢）---
    external_url: str = ""
    external_timeout_ms: int = 2000

    # --- 业务参数 ---
    extra: dict[str, str] = field(default_factory=dict)


def load_settings(service: str) -> Settings:
    """从环境变量装载配置。变量名统一前缀 W_（world）。"""
    p = f"W_{service.upper()}_"
    g = f"W_{service.upper()}_DOWNSTREAM_"

    return Settings(
        service=service,
        port=_env_int("W_PORT", 8000),
        log_level=_env_str("W_LOG_LEVEL", "INFO"),
        redis_url=_env_str("W_REDIS_URL", "redis://redis:6379/0"),
        pool_size=_env_int("W_POOL_SIZE", 8),
        pool_acquire_timeout_ms=_env_int("W_POOL_ACQUIRE_TIMEOUT_MS", 2000),
        pool_warn_ms=_env_int("W_POOL_WARN_MS", 200),
        downstream_name=_env_str(p + "DOWNSTREAM_NAME", ""),
        downstream_url=_env_str(p + "DOWNSTREAM_URL", ""),
        downstream_retries=_env_int(g + "RETRIES", 1),
        downstream_timeout_ms=_env_int(g + "TIMEOUT_MS", 3000),
        retry_backoff_ms=_env_int(g + "RETRY_BACKOFF_MS", 100),
        external_url=_env_str(p + "EXTERNAL_URL", ""),
        external_timeout_ms=_env_int(p + "EXTERNAL_TIMEOUT_MS", 2000),
    )


class PoolExhausted(RuntimeError):
    """获取连接超时——即"连接池耗尽"。

    单独定义异常类型，方便上层区分"池满了"和"别的问题"。
    对应 Java：自定义一个 extends RuntimeException 的异常类。
    """


class RedisPool:
    """带等待时长测量的 Redis 连接池。

    实现方式：一个计数信号量（asyncio.Semaphore）。
    Semaphore 的概念和 Java 的 java.util.concurrent.Semaphore 完全一样：
        获取许可（acquire）→ 用 → 归还许可（release）
        许可用完时，acquire 会一直等，直到有人 release。

    我们额外做的是：把"等了多久"量出来，写进日志。
    """

    def __init__(self, settings: Settings, log) -> None:
        self._settings = settings
        self._log = log
        self._sem = asyncio.Semaphore(settings.pool_size)
        self._size = settings.pool_size
        self._in_flight = 0
        self._client = aioredis.Redis.from_url(
            settings.redis_url, decode_responses=True
        )

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def ping(self) -> bool:
        try:
            await self._client.ping()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()

    @asynccontextmanager
    async def acquire(self, trace: str):
        """借一个连接。用法：

            async with pool.acquire(trace) as r:
                await r.get("key")

        这个 `async with` 就是 Java 的 try-with-resources：
        退出时自动归还，异常也不会漏还。
        """
        s = self._settings
        started = time.perf_counter()

        try:
            await asyncio.wait_for(
                self._sem.acquire(), timeout=s.pool_acquire_timeout_ms / 1000
            )
        except (TimeoutError, asyncio.TimeoutError):
            waited_ms = int((time.perf_counter() - started) * 1000)
            # ★ 这条日志就是"连接池耗尽"的第一手证据
            self._log.error(
                f"连接池获取超时：等待 {waited_ms}ms 后放弃"
                f"（池大小={self._size}，当前在途={self._in_flight}）",
                extra={"trace": trace},
            )
            raise PoolExhausted(
                f"redis pool exhausted after {waited_ms}ms "
                f"(size={self._size}, in_flight={self._in_flight})"
            ) from None

        waited_ms = int((time.perf_counter() - started) * 1000)
        self._in_flight += 1
        try:
            if waited_ms >= s.pool_warn_ms:
                # 池开始紧张但还没坏——这是"故障前兆"，比 ERROR 更早出现
                self._log.warning(
                    f"连接池等待 {waited_ms}ms（池大小={self._size}，"
                    f"当前在途={self._in_flight}）",
                    extra={"trace": trace},
                )
            yield self._client
        finally:
            self._in_flight -= 1
            self._sem.release()
