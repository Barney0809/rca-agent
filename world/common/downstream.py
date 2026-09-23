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


class DownstreamRejected(RuntimeError):
    """下游**明确拒绝**了这次请求（4xx）。

    与 DownstreamFailed 的区别：
      DownstreamFailed   —— 下游"没答上来"（5xx / 超时 / 连不上），值得重试
      DownstreamRejected —— 下游"答了，但是拒绝"（4xx），重试没有意义

    ⚠️ 为什么要单独一个类型：
       这里曾经只把 >=500 当错误，于是 409（库存不足）被当成**成功**返回，
       上游接着报出 CONFIRMED —— 明明失败了却报成功，是最危险的一类缺陷。
       见 docs/harness-log.md。区分开之后，拒绝会被原样上抛。
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"downstream rejected with {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


async def call_downstream(
    *,
    client: httpx.AsyncClient,
    settings: Settings,
    path: str,
    payload: dict,
    trace: str,
    log: TraceLogger,
    metrics: Metrics,
    retries: int,
) -> dict:
    """调用下游服务，返回它的 JSON 响应。

    参数都写成"关键字参数"（前面带 *），是为了调用处一眼能看懂每个值是什么。
    对应 Java：相当于强制使用命名参数（Java 没有这特性，但思路类似 Builder）。

    `retries` 单独传进来而不用 settings 里的值，是因为它必须能在**运行时**改
    （F4「重试风暴」就是把它从 1 改成 5）。settings 是启动时读死的，
    运行时的值放在 Knobs 里。
    """
    name = settings.downstream_name or "downstream"
    url = settings.downstream_url.rstrip("/") + path
    attempts = max(1, retries)
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

            # ---- 4xx：下游明确拒绝，不重试，但必须当失败上抛 ----
            # ⚠️ 这里曾经写的是 `>= 500`，导致 409（库存不足）被当成成功返回，
            #    上游于是报出 CONFIRMED —— 静默吞错，比报错危险得多。
            if 400 <= resp.status_code < 500:
                metrics.inc("downstream_calls_total", target=name, result="rejected")
                log.warning(
                    f"下游 {name} 拒绝了请求 HTTP {resp.status_code}：{resp.text[:200]}",
                    extra={"trace": trace},
                )
                raise DownstreamRejected(resp.status_code, resp.text[:500])

            if resp.status_code >= 500:
                raise DownstreamFailed(
                    f"{name} 返回 {resp.status_code}: {resp.text[:200]}"
                )

            metrics.inc("downstream_calls_total", target=name, result="ok")
            return resp.json()

        except DownstreamRejected:
            # 拒绝不是"重试能解决"的问题，直接上抛，不消耗重试次数
            raise

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
