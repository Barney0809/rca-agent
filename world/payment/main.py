"""
payment 服务 —— 链路的末端（叶子节点）。

职责：
    接收扣款请求 → 幂等检查 → 调用【外部风控】（模拟）→ 记账 → 返回

它是链路上最深的环节，也是多条故障链的源头：

  故障链一：「外部风控变慢」
      payment 变慢 → inventory 等 payment → order 等 inventory → 用户看到超时
      症状在最上游（order），根因在最下游（payment 的外部依赖）。
      ★ 这是最典型的一条"跨服务因果链"。

  故障链二：「重试风暴」
      inventory 的重试次数被调大 → payment 被重复打爆 → 雪崩
      症状在 payment（QPS 暴涨），根因却在 inventory 的配置。

外部风控是【模拟】的：它的延迟由环境变量控制，不真的去调第三方。
这样故障注入才有确定性，评测才有标准答案。
"""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from world.common.config import PoolExhausted, RedisPool, knobs_from, load_settings
from world.common.obs import (
    TRACE_HEADER,
    Metrics,
    get_logger,
    new_trace_id,
    setup_logging,
)
from world.common.runtime import make_inject_router

settings = load_settings("payment")
setup_logging(settings.service, settings.log_level)
log = get_logger(settings.service)
metrics = Metrics(settings.service)

knobs = knobs_from(settings)

metrics.describe("requests_total", "收到的业务请求数")
metrics.describe("pool_in_flight", "当前占用中的连接数")
metrics.describe("pool_limit", "连接池当前容量")
metrics.describe("handler_duration_ms", "本服务处理一次请求的耗时（毫秒）")
metrics.describe("risk_control_duration_ms", "调用外部风控的耗时（毫秒）")


async def _call_risk_control(trace: str) -> dict:
    """模拟调用外部风控系统。

    真实世界里，这类外部依赖是最常见的故障源之一：
      - 它不受你控制
      - 它变慢你只能等着
      - 它超时你的重试会让它更慢
    """
    started = time.perf_counter()

    # F1：风控变慢。延迟读自 knobs，可在运行时注入。
    latency_ms = knobs.risk_latency_ms
    if latency_ms > 0:
        await asyncio.sleep(latency_ms / 1000)

    # F5：风控按概率报错。同样可运行时注入。
    if knobs.risk_error_rate > 0 and random.random() < knobs.risk_error_rate:
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics.observe("risk_control_duration_ms", elapsed_ms)
        metrics.inc("requests_total", endpoint="risk_control", result="error")
        log.warning(
            f"外部风控调用失败（模拟）耗时={elapsed_ms:.0f}ms", extra={"trace": trace}
        )
        raise RuntimeError("risk control unavailable")

    elapsed_ms = (time.perf_counter() - started) * 1000
    duration = elapsed_ms
    metrics.observe("risk_control_duration_ms", duration)
    metrics.inc("requests_total", endpoint="risk_control", result="ok")

    # 慢的时候必须留痕，否则上游的等待就失去了证据
    if duration >= 500:
        log.warning(
            f"外部风控响应缓慢 耗时={duration:.0f}ms", extra={"trace": trace}
        )
    else:
        log.debug(f"外部风控通过 耗时={duration:.0f}ms", extra={"trace": trace})

    return {"risk_score": 0.1, "decision": "PASS", "latency_ms": round(duration, 1)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = RedisPool(settings, log, knobs)
    app.state.http = httpx.AsyncClient()
    log.info(
        f"服务启动 service={settings.service} port={settings.port} "
        f"池大小={knobs.pool_limit} "
        f"风控延迟={knobs.risk_latency_ms}ms 风控错误率={knobs.risk_error_rate}"
    )
    yield
    await app.state.pool.close()
    await app.state.http.aclose()
    log.info("服务停止")


app = FastAPI(title="payment", lifespan=lifespan)

# 故障注入端点。⚠️ 刻意不写任何日志 —— 见 world/common/runtime.py。
app.include_router(make_inject_router(knobs))


class ChargeRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=64)
    sku: str = Field(..., min_length=1, max_length=64)
    qty: int = Field(1, ge=1, le=10)
    amount_cents: int = Field(..., ge=0)


@app.get("/health")
async def health(request: Request):
    redis_ok = await request.app.state.pool.ping()
    return {"service": settings.service, "redis": redis_ok}


@app.get("/metrics")
async def metrics_endpoint(request: Request):
    request.app.state.pool and metrics.set_gauge(
        "pool_in_flight", request.app.state.pool.in_flight
    )
    request.app.state.pool and metrics.set_gauge("pool_limit", request.app.state.pool.limit)
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@app.post("/charge")
async def charge(body: ChargeRequest, request: Request):
    trace = request.headers.get(TRACE_HEADER) or new_trace_id()
    started = time.perf_counter()

    metrics.inc("requests_total", endpoint="charge")

    pool: RedisPool = request.app.state.pool
    charge_key = f"charge:{body.order_id}"

    try:
        async with pool.acquire(trace) as r:
            # ---- 幂等：同一个订单只扣一次款 ----
            # 因为上游会重试，没有幂等保护就会重复扣款。
            existing = await r.get(charge_key)
            if existing is not None:
                metrics.inc("requests_total", endpoint="charge", result="duplicate")
                log.info(
                    f"重复扣款请求已拦截 order_id={body.order_id}",
                    extra={"trace": trace},
                )
                return {"order_id": body.order_id, "status": "ALREADY_CHARGED",
                        "trace_id": trace}

            risk = await _call_risk_control(trace)

            await r.hset(
                charge_key,
                mapping={
                    "order_id": body.order_id,
                    "sku": body.sku,
                    "qty": str(body.qty),
                    "amount_cents": str(body.amount_cents),
                    "risk_decision": risk["decision"],
                    "trace_id": trace,
                },
            )
            await r.expire(charge_key, 3600)

    except PoolExhausted as exc:
        metrics.inc("requests_total", endpoint="charge", result="pool_exhausted")
        log.error(f"扣款失败（连接池耗尽）order_id={body.order_id}: {exc}",
                  extra={"trace": trace})
        raise HTTPException(status_code=503, detail=f"服务繁忙：{exc}") from exc

    except Exception as exc:  # 外部风控失败等
        metrics.inc("requests_total", endpoint="charge", result="external_failed")
        log.error(f"扣款失败 order_id={body.order_id}: {type(exc).__name__}: {exc}",
                  extra={"trace": trace})
        raise HTTPException(status_code=502, detail=f"扣款失败：{exc}") from exc

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics.observe("handler_duration_ms", elapsed_ms, endpoint="charge")
    metrics.inc("requests_total", endpoint="charge", result="ok")

    log.info(
        f"扣款成功 order_id={body.order_id} 金额={body.amount_cents}分 "
        f"风控耗时={risk['latency_ms']}ms 总耗时={elapsed_ms:.0f}ms",
        extra={"trace": trace},
    )
    return {
        "order_id": body.order_id,
        "status": "CHARGED",
        "amount_cents": body.amount_cents,
        "risk": risk,
        "trace_id": trace,
    }
