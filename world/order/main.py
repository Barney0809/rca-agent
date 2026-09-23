"""
order 服务 —— 链路的最上游，也是唯一对外暴露业务入口的服务。

职责：
    接收下单请求 → 写一条订单记录 → 调用 inventory 预留库存 → 返回结果

⚠️ 本文件里有一处【刻意的反模式】，见 create_order 里的注释。
   它是真实生产系统里常见的写法，也是"连接池耗尽"故障的成因。
   我们保留它，因为被诊断系统必须带有真实的缺陷，否则无从诊断。
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from world.common.config import PoolExhausted, RedisPool, knobs_from, load_settings
from world.common.downstream import DownstreamFailed, call_downstream
from world.common.obs import (
    TRACE_HEADER,
    Metrics,
    get_logger,
    new_trace_id,
    setup_logging,
)
from world.common.runtime import make_inject_router

settings = load_settings("order")
setup_logging(settings.service, settings.log_level)
log = get_logger(settings.service)
metrics = Metrics(settings.service)

# 运行时可调参数。初始值来自环境变量，之后可经 /_inject 在线修改。
knobs = knobs_from(settings)

# ⚠️ F6（内存泄漏，对照组）用的容器：只增不减。
# 只有注入时才会被写入；不注入时它是空的，对正常路径零影响。
_LEAK: dict[str, bytes] = {}

metrics.describe("requests_total", "收到的业务请求数")
metrics.describe("downstream_duration_ms", "调用下游的耗时（毫秒）")
metrics.describe("downstream_calls_total", "调用下游的次数")
metrics.describe("pool_in_flight", "当前占用中的连接数")
metrics.describe("pool_limit", "连接池当前容量")
metrics.describe("handler_duration_ms", "本服务处理一次请求的耗时（毫秒）")
metrics.describe("leak_bytes", "F6 注入时累积的泄漏字节数")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动 / 停止时各做一件事。

    `yield` 之前 = 启动时；之后 = 停止时。
    对应 Java：@PostConstruct 和 @PreDestroy。
    """
    app.state.pool = RedisPool(settings, log, knobs)
    app.state.http = httpx.AsyncClient()

    ok = await app.state.pool.ping()
    log.info(
        f"服务启动 service={settings.service} port={settings.port} "
        f"池大小={knobs.pool_limit} 下游={settings.downstream_name or '(无)'} "
        f"redis={'可用' if ok else '不可用'}"
    )
    yield
    await app.state.pool.close()
    await app.state.http.aclose()
    log.info("服务停止")


app = FastAPI(title="order", lifespan=lifespan)

# 故障注入端点。⚠️ 它刻意不写任何日志 —— 见 world/common/runtime.py 的说明。
app.include_router(make_inject_router(knobs))


class OrderRequest(BaseModel):
    """请求体校验。

    对应 Java：一个 DTO + jakarta.validation 注解。
    校验失败时 FastAPI 自动返回 422，不用手写 if。
    """

    sku: str = Field(..., min_length=1, max_length=64, description="商品编号")
    qty: int = Field(1, ge=1, le=10, description="数量")


@app.get("/health")
async def health(request: Request):
    redis_ok = await request.app.state.pool.ping()
    return {"service": settings.service, "redis": redis_ok}


@app.get("/metrics")
async def metrics_endpoint(request: Request):
    """Prometheus 文本格式的指标。

    对应 Java：Micrometer + /actuator/prometheus。
    """
    request.app.state.pool and metrics.set_gauge(
        "pool_in_flight", request.app.state.pool.in_flight
    )
    request.app.state.pool and metrics.set_gauge("pool_limit", request.app.state.pool.limit)
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@app.post("/orders")
async def create_order(body: OrderRequest, request: Request):
    # ---- trace id：从上游头里取，没有就新生成 ----
    # 显式取、显式往下传。绝不放进全局变量。
    trace = request.headers.get(TRACE_HEADER) or new_trace_id()
    order_id = f"ORD-{uuid.uuid4().hex[:10].upper()}"
    started = time.perf_counter()

    metrics.inc("requests_total", endpoint="create_order")
    log.info(
        f"收到下单请求 order_id={order_id} sku={body.sku} qty={body.qty}",
        extra={"trace": trace},
    )

    pool: RedisPool = request.app.state.pool
    try:
        # ---- F6（内存泄漏，对照组）的注入点 ----
        # 只有 knobs.leak_mb_per_req > 0 时才泄漏；默认 0，对正常路径零影响。
        if knobs.leak_mb_per_req > 0:
            _LEAK[order_id] = b"x" * int(knobs.leak_mb_per_req * 1024 * 1024)
            metrics.set_gauge("leak_bytes", sum(len(v) for v in _LEAK.values()))

        # ⚠️⚠️ 刻意的反模式 ⚠️⚠️
        #
        # 下面这段把 Redis 连接【借出来之后一直拿着】，中间还跨网络去调
        # inventory。生产代码里这是明确的反模式：持有一个池化资源去做
        # 网络调用，会让连接被长时间占住。
        #
        # 后果：只要下游变慢，本服务的连接池就会被慢慢抽干 —— 请求越积
        #       越多，每个都揣着一个连接在等下游。这就是"连接池耗尽"。
        #
        # 我们【故意保留】它，因为：
        #   1) 这是真实系统里真实存在的写法
        #   2) 它让"下游变慢 → 上游池耗尽"这条跨服务因果链真实成立
        #   3) 它就是 Agent 最终要定位出来的根因之一
        async with pool.acquire(trace) as r:
            await r.hset(
                f"order:{order_id}",
                mapping={
                    "order_id": order_id,
                    "sku": body.sku,
                    "qty": str(body.qty),
                    "status": "PENDING",
                    "trace_id": trace,
                },
            )
            await r.expire(f"order:{order_id}", 3600)

            # 在持有连接的情况下调用下游 —— 反模式，见上
            result = await call_downstream(
                client=request.app.state.http,
                settings=settings,
                path="/reserve",
                payload={"sku": body.sku, "qty": body.qty, "order_id": order_id},
                trace=trace,
                log=log,
                metrics=metrics,
                retries=knobs.downstream_retries,
            )

            await r.hset(f"order:{order_id}", mapping={"status": "CONFIRMED"})

    except PoolExhausted as exc:
        metrics.inc("requests_total", endpoint="create_order", result="pool_exhausted")
        log.error(f"下单失败（连接池耗尽）order_id={order_id}: {exc}", extra={"trace": trace})
        raise HTTPException(status_code=503, detail=f"服务繁忙：{exc}") from exc

    except DownstreamFailed as exc:
        metrics.inc("requests_total", endpoint="create_order", result="downstream_failed")
        log.error(f"下单失败（下游不可用）order_id={order_id}: {exc}", extra={"trace": trace})
        raise HTTPException(status_code=502, detail=f"下游服务异常：{exc}") from exc

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics.observe("handler_duration_ms", elapsed_ms, endpoint="create_order")
    metrics.inc("requests_total", endpoint="create_order", result="ok")

    log.info(
        f"下单成功 order_id={order_id} 耗时={elapsed_ms:.0f}ms",
        extra={"trace": trace},
    )
    return {"order_id": order_id, "status": "CONFIRMED", "trace_id": trace, "detail": result}
