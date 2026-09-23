"""
离线回归用例 —— 不需要被诊断系统在运行，秒级完成。

每一条都对应 `docs/harness-log.md` 里一个**真实发生过**的错误。
测试名里带 `regression_#N` 的就是它在清单里的编号。

============================ 什么是回归用例 ============================

"回归"= 已经修好的 bug 又回来了。
回归用例就是一段**专门盯住那个 bug 的自动测试**：平时是绿的，
**一旦 bug 复活立刻变红**。

它和"我手动试过了"的区别：
    手动试过  → 证明【现在】是对的，靠人的记性维持
    回归用例  → 证明【以后一直】是对的，靠机器维持

============================ 为什么这里不用 Docker ============================

能用单元测试覆盖的，就不要用集成测试。
`call_downstream` 的契约（4xx 怎么处理）完全可以用一个假的 HTTP 客户端测出来，
不需要真的起三个容器 —— 这样它可以在任何地方、任何时间跑，几毫秒出结果。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from world.common.config import Settings
from world.common.downstream import (
    DownstreamFailed,
    DownstreamRejected,
    call_downstream,
)
from world.common.obs import Metrics, get_logger
from rca.telemetry.parse import parse_log_line, template_of

ROOT = Path(__file__).resolve().parent.parent


# ================================================================
# 辅助
# ================================================================

def _settings() -> Settings:
    return Settings(
        service="unittest",
        downstream_name="upstream",
        downstream_url="http://upstream.test",
        downstream_timeout_ms=1000,
        retry_backoff_ms=1,        # 测试里退避要极短，否则白等
    )


def _client(handler) -> httpx.AsyncClient:
    """一个永不真正联网的 HTTP 客户端。

    httpx.MockTransport 让你自己决定每个请求返回什么 —— 对应 Java 的
    MockWebServer / WireMock，但不需要起服务。
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _call(client: httpx.AsyncClient, retries: int = 1) -> dict:
    return await call_downstream(
        client=client,
        settings=_settings(),
        path="/reserve",
        payload={"sku": "SKU-001"},
        trace="t-test",
        log=get_logger("unittest"),
        metrics=Metrics("unittest"),
        retries=retries,
    )


# ================================================================
# regression_#4：下游 4xx 绝不能被当成成功
# ================================================================

async def test_regression_4_downstream_4xx_is_rejected_not_success():
    """回归 #4 —— 这是本项目最危险的一类缺陷：**静默吞错**。

    历史：原实现只把 `>= 500` 当错误，于是 `409 库存不足` 被当成成功返回，
    上游 order 报出 `CONFIRMED`。指标里 5xx=0，看起来一切正常，
    排障时被引向完全错误的方向。

    这条用例一旦变红，说明有人又把 4xx 放过去了。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "库存不足"})

    async with _client(handler) as client:
        with pytest.raises(DownstreamRejected) as ei:
            await _call(client)

    assert ei.value.status_code == 409, "拒绝的状态码必须原样保留，不能改写"
    assert "库存不足" in ei.value.detail, "拒绝的原因必须原样保留，便于上游上抛"


async def test_regression_4_4xx_must_not_retry():
    """回归 #4（续）—— 4xx 不该重试。

    重试能不能成功？不能。下游是"明确拒绝"，不是"没答上来"。
    重试只会把无效请求放大 N 倍打过去 —— 那正是 F4（重试风暴）的成因。
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(409, json={"detail": "库存不足"})

    async with _client(handler) as client:
        with pytest.raises(DownstreamRejected):
            await _call(client, retries=5)

    assert len(calls) == 1, f"4xx 只应尝试 1 次，实际尝试了 {len(calls)} 次"


async def test_regression_4_5xx_does_retry_then_fails():
    """对照：5xx 是"没答上来"，应当重试到上限再抛。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="boom")

    async with _client(handler) as client:
        with pytest.raises(DownstreamFailed):
            await _call(client, retries=3)

    assert len(calls) == 3, f"5xx 应重试到上限 3 次，实际 {len(calls)} 次"


async def test_regression_4_success_path_returns_payload():
    """正向对照：2xx 才允许返回响应体。没有这条，上面的断言可能是"永远抛异常"造成的假绿。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "remaining": 998})

    async with _client(handler) as client:
        result = await _call(client)

    assert result == {"ok": True, "remaining": 998}


# ================================================================
# regression_#3：可执行脚本必须纯 ASCII
# ================================================================

# 这些文件会被 shell / make 直接解析，一旦含非 ASCII 就有风险
ASCII_EXACT_NAMES = {"Makefile"}
ASCII_SUFFIXES = {".ps1", ".psm1", ".bat", ".cmd"}

SKIP_DIRS = {".venv", ".git", "node_modules", "__pycache__", "target", "runs", "recordings"}


def _executable_scripts() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        if path.suffix.lower() in ASCII_SUFFIXES or path.name in ASCII_EXACT_NAMES:
            found.append(path)
    return sorted(found)


def test_regression_3_scripts_must_be_pure_ascii():
    """回归 #3 —— 同一个编码机制已经造成三次不同后果。

        第 1 次：路径解析错位 → 删除了一个真实目录（**不可恢复**）
        第 2 次：脚本语法解析错位 → 脚本直接崩溃
        第 3 次：数据解析错位 → PowerShell 读 JSON 失败

    根因相同：**Windows PowerShell 5.1 把 UTF-8 无 BOM 的文件按 GBK 读**。

    前两次之后我们只建立了"靠纪律"的约定。第三次证明了纪律不够：
    那次零损失只是因为**当时有人在盯**。这条自动化检查才是真正的封堵。
    """
    offenders: list[str] = []

    for path in _executable_scripts():
        data = path.read_bytes()
        bad_positions = [i for i, b in enumerate(data) if b > 0x7F]
        if bad_positions:
            line = data[: bad_positions[0]].count(b"\n") + 1
            offenders.append(
                f"{path.relative_to(ROOT)} 有 {len(bad_positions)} 个非 ASCII 字节"
                f"（首个在第 {line} 行）"
            )

    assert not offenders, (
        "以下可执行脚本含非 ASCII 字符，在 Windows PowerShell 5.1 下会被误读：\n  "
        + "\n  ".join(offenders)
        + "\n把中文移到 .md 文档里，脚本一律用纯 ASCII。"
        + "\n详见 docs/harness-log.md #1 #2 #3。"
    )


def test_regression_3_the_check_actually_scans_something():
    """元测试：确认上面的扫描不是"扫了 0 个文件"造成的假绿。

    这类"测试自己失效了却显示通过"的情况很常见 —— 断言一下扫描范围。
    """
    scripts = _executable_scripts()
    names = sorted(str(p.relative_to(ROOT)) for p in scripts)
    # 目前仓库里有 Makefile 和 scripts/dev.ps1 两个可执行脚本。
    # 门槛写 2 而不是 3：写死过高的数字会让"新增脚本还没加"变成假红。
    assert len(scripts) >= 2, f"只扫到 {len(scripts)} 个脚本：{names}"
    assert any(p.name == "Makefile" for p in scripts), "没扫到 Makefile，路径规则可能错了"


# ================================================================
# regression_#6：第三方库写的日志不能整类被漏掉
# ================================================================

def test_regression_6_third_party_log_lines_are_parsed():
    """回归 #6 —— httpx 等第三方库写的日志**没有 `[service]` 段**。

    历史：解析正则要求必须有 `[service]`，于是这类行整类被漏掉 ——
    实测占全部日志的 **21%**，而且它承载关键证据（"下游 502 Bad Gateway"）。

    这条用例盯住三种日志变体都能解析。
    """
    cases = [
        # a) 我们自己写的：有 service、有 trace
        (
            "2026-09-24 03:52:19.348 INFO  [order] trace=abc123 | 收到下单请求",
            "order",
            "收到下单请求",
        ),
        # b) 我们自己写的：trace 为 -
        (
            "2026-09-24 03:52:19.348 INFO  [order] trace=- | 服务启动",
            "order",
            "服务启动",
        ),
        # c) ★ 第三方库写的：既没有 service 也没有 trace —— 就是这一条曾经被漏掉
        (
            '2026-09-24 04:59:22.064 INFO  HTTP Request: POST '
            'http://inventory:8000/reserve "HTTP/1.1 502 Bad Gateway"',
            "order",              # 没有 service 标记，应当回退到调用方传入的默认值
            "502 Bad Gateway",
        ),
    ]

    for raw, default_svc, expected_fragment in cases:
        rec = parse_log_line(raw, default_service=default_svc)
        assert rec is not None, f"这一行没能解析：{raw!r}"
        assert rec.service == default_svc
        assert expected_fragment in rec.message, f"消息体丢了内容：{rec.message!r}"


def test_regression_6_unparsed_lines_are_counted_not_silently_dropped():
    """配套：真正无法解析的行必须能被识别出来（好让上游统计，而不是静默丢）。"""
    assert parse_log_line("这是一行完全不符合任何格式的垃圾", "order") is None
    assert parse_log_line("", "order") is None
    assert parse_log_line("   ", "order") is None


# ================================================================
# 降维的核心机制：模板归一化
# ================================================================

def test_template_normalization_groups_variable_parts():
    """模板归一化是降维能成立的关键。

    几万行日志之所以能收敛成几十个模板，是因为可变部分（数字、ID、耗时）
    被替换成了占位符。如果这个机制坏了，每个不同的数值都会变成一个独立模板，
    降维立刻失效（模板数会爆炸到与行数同量级）。
    """
    a = template_of("连接池等待 350ms（池大小=2，当前在途=2）")
    b = template_of("连接池等待 812ms（池大小=8，当前在途=7）")
    assert a == b, f"同一个事件的不同数值应当归一到同一模板：\n  {a}\n  {b}"

    assert "<N>" in a, "数字应当被替换为占位符"

    # 订单号（长十六进制串）应当被归一，而不是被数字规则切碎
    c = template_of("下单失败 order_id=ORD-6E4BC1C290")
    d = template_of("下单失败 order_id=ORD-1CDA2D0AEE")
    assert c == d, f"订单号应当被归一：\n  {c}\n  {d}"
    assert "ORD-<ID>" in c, f"订单号归一方式不对：{c}"

    # 不同事件**不能**被错误合并
    e = template_of("连接池获取超时：等待 400ms 后放弃")
    assert e != a, "不同事件的模板不应相同"
