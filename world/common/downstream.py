"""
调用下游服务的统一入口：带重试、带耗时统计、带 trace 传递。

============================ 为什么单独抽一个模块 ============================

链路上有两个服务都要"调用它的下一个环节"（order→inventory→payment），
如果各写一遍，重试逻辑就会分叉，将来"配置漂移"故障没法统一定义。

所以统一在这里。它做四件事：

  1) 把 trace id 通过 HTTP 头传下去  ← 跨服务串联的关键
  2) 超时控制
  3) 重试 + 退避（★ "配置漂移"故障就是改这里的重试次数）
  4) 把耗时和成功/失败计入指标

⚠️ trace id 显式传参，不用任何全局上下文 —— 见 obs.py 里的说明。
"""

from __future__ import annotations

import asyncio
import time

import httpx

from .config import Settings
from .obs import TRACE_HEADER, Metrics, TraceLogger


class DownstreamFailed(RuntimeError):
    """重试全部失败后抛出。"""


async def call_downstream(
    *,
    client: httpx.AsyncClient,
    settings: Settings,
    path: str,
    payload: dict,
    trace: str,
    log: TraceLogger,
    metrics: Metrics,
) -> dict:
    """调用下游服务，返回它的 JSON 响应。

    参数都写成"关键字参数"（前面带 *），是为了调用处一眼能看懂每个值是什么。
    对应 Java：相当于强制使用命名参数（Java 没有这特性，但思路类似 Builder）。
    """
    name = settings.downstream_name or "downstream"
    url = settings.downstream_url.rstrip("/") + path
    attempts = max(1, settings.downstream_retries)
    timeout_s = settings.downstream_timeout_ms / 1000

    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            resp = await client.post(
                url,
                json=payload,
                headers={TRACE_HEADER: trace},
                timeout=timeout_s,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            metrics.observe("downstream_duration_ms", elapsed_ms, target=name)

            if resp.status_code >= 500:
                raise DownstreamFailed(
                    f"{name} 返回 {resp.status_code}: {resp.text[:200]}"
                )

            metrics.inc("downstream_calls_total", target=name, result="ok")
            return resp.json()

        except Exception as exc:  # noqa: BLE001 —— 这里刻意兜住所有异常做统一重试
            last_error = exc
            elapsed_ms = (time.perf_counter() - started) * 1000
            metrics.inc("downstream_calls_total", target=name, result="error")

            # 每次失败都记日志：这是"重试风暴"类故障的原始证据，
            # 也是 D2「配置漂移」故障能被诊断出来的关键信号。
            log.warning(
                f"调用下游 {name} 第 {attempt}/{attempts} 次失败"
                f"（耗时 {elapsed_ms:.0f}ms）：{type(exc).__name__}: {exc}",
                extra={"trace": trace},
            )

            if attempt < attempts:
                await asyncio.sleep(settings.retry_backoff_ms / 1000)

    metrics.inc("downstream_exhausted_total", target=name)
    log.error(
        f"调用下游 {name} 重试 {attempts} 次全部失败：{last_error}",
        extra={"trace": trace},
    )
    raise DownstreamFailed(str(last_error))
