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

import asyncio
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from world.common.config import PoolExhausted, RedisPool, knobs_from, load_settings
from world.common.downstream import (
    DownstreamFailed,
    DownstreamRejected,
    call_downstream,
)
from world.common.obs import (
    TRACE_HEADER,
    Metrics,
    get_logger,
    new_trace_id,
    setup_logging,
)
from world.common.runtime import make_inject_router

settings = load_settings("inventory")
setup_logging(settings.service, settings.log_level)
log = get_logger(settings.service)
metrics = Metrics(settings.service)

knobs = knobs_from(settings)

metrics.describe("requests_total", "收到的业务请求数")
metrics.describe("downstream_duration_ms", "调用下游的耗时（毫秒）")
metrics.describe("downstream_calls_total", "调用下游的次数")
metrics.describe("pool_in_flight", "当前占用中的连接数")
metrics.describe("pool_limit", "连接池当前容量")
metrics.describe("handler_duration_ms", "本服务处理一次请求的耗时（毫秒）")
metrics.describe("stock_level", "当前库存量")

SEED_SKUS = ["SKU-001", "SKU-002", "SKU-003"]

# 种子库存：刻意给得很大。
#
# ⚠️ 踩过的坑：原来是 1000，被压测几轮就打空了。
#    库存一空，请求就不再往下游走 —— 于是 payment 收不到请求、日志断流、
#    响应变成 15ms。现象看起来像"故障注入没生效"，实际是库存耗尽的干扰，
#    排查花了不少时间。见 docs/harness-log.md。
DEFAULT_STOCK = 1_000_000


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = RedisPool(settings, log, knobs)
    app.state.http = httpx.AsyncClient()

    # 初始化库存。
    # ⚠️ 这里刻意【覆盖】写，而不是"仅当键不存在才写"（即不加 nx=True）：
    #    库存是**夹具数据**，重启后应当是确定值。
    #    如果只在不存在时写，那么压测打空之后重启仍然是空的，场景无法复现。
    async with app.state.pool.acquire("-") as r:
        for sku in SEED_SKUS:
            await r.set(f"stock:{sku}", DEFAULT_STOCK)

    log.info(
        f"服务启动 service={settings.service} port={settings.port} "
        f"池大小={knobs.pool_limit} 下游={settings.downstream_name or '(无)'} "
        f"重试次数={knobs.downstream_retries}"
    )
    yield
    await app.state.pool.close()
    await app.state.http.aclose()
    log.info("服务停止")


app = FastAPI(title="inventory", lifespan=lifespan)

# 故障注入端点。⚠️ 刻意不写任何日志 —— 见 world/common/runtime.py。
app.include_router(make_inject_router(knobs))


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
    request.app.state.pool and metrics.set_gauge("pool_limit", request.app.state.pool.limit)
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
        # ---- F3（本环节处理变慢）的注入点 ----
        # ⚠️ 刻意放在连接池【之外】。
        #    如果放在池内，它会把连接一直占住，从而也触发连接池耗尽 ——
        #    那样 F3 与 F2 的机制就混在一起，Agent 无法区分是哪一种。
        #    放在池外，F3 就是干净的"这一层自己慢"，可与 F1（下游慢）
        #    通过"耗时断层在哪一层"区分开。
        if knobs.slow_op_ms > 0:
            await asyncio.sleep(knobs.slow_op_ms / 1000)

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
                retries=knobs.downstream_retries,
            )

    except PoolExhausted as exc:
        metrics.inc("requests_total", endpoint="reserve", result="pool_exhausted")
        log.error(
            f"库存预留失败（连接池耗尽）order_id={body.order_id}: {exc}",
            extra={"trace": trace},
        )
        raise HTTPException(status_code=503, detail=f"服务繁忙：{exc}") from exc

    except DownstreamRejected as exc:
        metrics.inc("requests_total", endpoint="reserve", result="rejected")
        log.error(
            f"库存预留被下游拒绝 order_id={body.order_id} HTTP {exc.status_code}: {exc.detail}",
            extra={"trace": trace},
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

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
