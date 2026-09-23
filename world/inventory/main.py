"""
inventory 服务 —— 链路中间环节。

职责：
    接收预留请求 → 检查并扣减库存 → 调用 payment 扣款 → 返回结果

它同时是"配置漂移"故障的载体：
    它的【重试次数】可以远超合理值，于是下游出问题时，
    inventory 会把流量放大好几倍打给 payment —— 上游的配置问题
    在下游造成雪崩。这是典型的"症状与根因不在同一处"。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from world.common.config import PoolExhausted, RedisPool, load_settings
from world.common.downstream import DownstreamFailed, call_downstream
from world.common.obs import (
    TRACE_HEADER,
    Metrics,
    get_logger,
    new_trace_id,
    setup_logging,
)

settings = load_settings("inventory")
setup_logging(settings.service, settings.log_level)
log = get_logger(settings.service)
metrics = Metrics(settings.service)

metrics.describe("requests_total", "收到的业务请求数")
metrics.describe("downstream_duration_ms", "调用下游的耗时（毫秒）")
metrics.describe("downstream_calls_total", "调用下游的次数")
metrics.describe("pool_in_flight", "当前占用中的连接数")
metrics.describe("handler_duration_ms", "本服务处理一次请求的耗时（毫秒）")
metrics.describe("stock_level", "当前库存量")

SEED_SKUS = ["SKU-001", "SKU-002", "SKU-003"]
DEFAULT_STOCK = 1000


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = RedisPool(settings, log)
    app.state.http = httpx.AsyncClient()

    # 初始化库存：只在键不存在时写入，重启不会把库存重置掉。
    # nx=True 就是 Redis 的"仅当键不存在才设置"。
    async with app.state.pool.acquire("-") as r:
        for sku in SEED_SKUS:
            await r.set(f"stock:{sku}", DEFAULT_STOCK, nx=True)

    log.info(
        f"服务启动 service={settings.service} port={settings.port} "
        f"池大小={settings.pool_size} 下游={settings.downstream_name or '(无)'} "
        f"重试次数={settings.downstream_retries}"
    )
    yield
    await app.state.pool.close()
    await app.state.http.aclose()
    log.info("服务停止")


app = FastAPI(title="inventory", lifespan=lifespan)


class ReserveRequest(BaseModel):
    sku: str = Field(..., min_length=1, max_length=64)
    qty: int = Field(1, ge=1, le=10)
    order_id: str = Field(..., min_length=1, max_length=64)


@app.get("/health")
async def health(request: Request):
    redis_ok = await request.app.state.pool.ping()
    return {"service": settings.service, "redis": redis_ok}


@app.get("/metrics")
async def metrics_endpoint(request: Request):
    request.app.state.pool and metrics.set_gauge(
        "pool_in_flight", request.app.state.pool.in_flight
    )
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@app.post("/reserve")
async def reserve(body: ReserveRequest, request: Request):
    trace = request.headers.get(TRACE_HEADER) or new_trace_id()
    started = time.perf_counter()

    metrics.inc("requests_total", endpoint="reserve")
    log.info(
        f"收到库存预留请求 order_id={body.order_id} sku={body.sku} qty={body.qty}",
        extra={"trace": trace},
    )

    pool: RedisPool = request.app.state.pool
    stock_key = f"stock:{body.sku}"

    try:
        async with pool.acquire(trace) as r:
            raw = await r.get(stock_key)
            if raw is None:
                log.warning(f"库存记录不存在 sku={body.sku}", extra={"trace": trace})
                raise HTTPException(status_code=404, detail=f"未知商品 {body.sku}")

            stock = int(raw)
            metrics.set_gauge("stock_level", stock, sku=body.sku)

            if stock < body.qty:
                metrics.inc("requests_total", endpoint="reserve", result="out_of_stock")
                log.warning(
                    f"库存不足 sku={body.sku} 现有={stock} 需要={body.qty}",
                    extra={"trace": trace},
                )
                raise HTTPException(status_code=409, detail="库存不足")

            # 扣减库存。注意这里是"先读再写"，不是原子操作 ——
            # 又多一处真实系统常见的缺陷（并发下会超卖）。
            # 对诊断而言它不直接影响故障定位，但让系统更真实。
            await r.set(stock_key, stock - body.qty)

            result = await call_downstream(
                client=request.app.state.http,
                settings=settings,
                path="/charge",
                payload={
                    "order_id": body.order_id,
                    "sku": body.sku,
                    "qty": body.qty,
                    "amount_cents": body.qty * 1999,
                },
                trace=trace,
                log=log,
                metrics=metrics,
            )

    except PoolExhausted as exc:
        metrics.inc("requests_total", endpoint="reserve", result="pool_exhausted")
        log.error(
            f"库存预留失败（连接池耗尽）order_id={body.order_id}: {exc}",
            extra={"trace": trace},
        )
        raise HTTPException(status_code=503, detail=f"服务繁忙：{exc}") from exc

    except DownstreamFailed as exc:
        metrics.inc("requests_total", endpoint="reserve", result="downstream_failed")
        log.error(
            f"库存预留失败（下游不可用）order_id={body.order_id}: {exc}",
            extra={"trace": trace},
        )
        raise HTTPException(status_code=502, detail=f"下游服务异常：{exc}") from exc

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics.observe("handler_duration_ms", elapsed_ms, endpoint="reserve")
    metrics.inc("requests_total", endpoint="reserve", result="ok")

    log.info(
        f"库存预留成功 order_id={body.order_id} sku={body.sku} "
        f"剩余={stock - body.qty} 耗时={elapsed_ms:.0f}ms",
        extra={"trace": trace},
    )
    return {
        "order_id": body.order_id,
        "sku": body.sku,
        "remaining": stock - body.qty,
        "payment": result,
        "trace_id": trace,
    }
