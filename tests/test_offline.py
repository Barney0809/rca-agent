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

import ast
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
        # ⚠️ 必须比对**相对于项目根**的路径，而不是绝对路径的每一段。
        #
        # 用绝对路径比对会误伤：只要项目恰好被放在名为 runs / target / recordings
        # 之类的目录下面，`SKIP_DIRS & set(path.parts)` 就恒为真，
        # 整个扫描会**静默跳过全部文件**、扫到 0 个脚本 —— 然后这条用例仍然"通过"。
        #
        # 这不是假想：变异检查脚本把代码副本放在 `runs/_mutants/` 下时，
        # 副本里就正好扫到 0 个脚本（被 test_regression_3_the_check_actually_scans_something 抓到）。
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
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
# P4：会 print 的脚本必须给 stdout 兜底（同一个编码机制的第四次复现）
# ================================================================
#
# 事实：本机控制台代码页是 GBK（936）。stdout **接到管道/文件**时，
#   Python 会退回本地编码 GBK，打印它编不出的字符（emoji 之类）就抛
#   `UnicodeEncodeError: 'gbk' codec can't encode character '\u2705'`。
#
# 后果特别恶劣的地方在于：**它是在最后一步炸的** ——
#   脚本可能已经跑完了全部工作（包括真实调用了 LLM、花了钱），
#   只因为打印一个 ✅ 就崩掉，整段输出和退出码一起丢掉，看起来像"脚本坏了"。
#
# ============================ 正确写法是实测出来的 ============================
#
#   这个仓库里曾经有两种"看起来都像兜底"的写法。用 `scripts/probe_stdout_encoding.py`
#   在**真实管道**下实测（本机 cp936），结果如下：
#
#     | 写法                                | 中文      | emoji | 结论 |
#     |-------------------------------------|-----------|-------|------|
#     | 完全不兜底                          | GBK 字节  | 崩溃  | ❌   |
#     | `reconfigure(errors="replace")`     | 仍是 GBK  | `?`   | ❌ 不崩，但读不成 |
#     | `reconfigure(encoding="utf-8", ...)`| 正常      | 正常  | ✅   |
#
#   所以用例必须**同时**钉住 encoding 和 errors。
#   只查"有没有调用 reconfigure"是不够的 —— 作者本人第一版就是这么写的，
#   还配了一段"不要用 encoding=utf-8，那会让中文变乱码"的注释，
#   然后被上面这张表直接推翻。
#
# 为什么不做"print 里一律不许非 ASCII"的一刀切禁令：
#   仓库里已有 170+ 处中文 print。它们是**能正常显示**的（中文在 GBK 里有编码），
#   禁令会把"能工作"的东西判成违规，属于为了洁癖制造假红。


# 哪些 errors= 策略算"真的把崩溃降级了"。
# strict 不在其中 —— 它是 Python 的默认值，写了等于没写。
FORGIVING_ERROR_MODES = frozenset(
    {"replace", "backslashreplace", "ignore", "xmlcharrefreplace"}
)


def _script_printers() -> list[tuple[Path, ast.Module]]:
    """所有"会 print"的 python 脚本（连同它的语法树，避免重复解析）。"""
    out: list[tuple[Path, ast.Module]] = []
    for path in sorted((ROOT / "scripts").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
            for node in ast.walk(tree)
        ):
            out.append((path, tree))
    return out


def _has_stdout_guard(tree: ast.Module) -> bool:
    """是否调用了 `sys.stdout.reconfigure(encoding="utf-8", errors=<宽容策略>)`。

    ⚠️ 这里**不能**只检查"有没有调用 reconfigure"，也不能只检查 errors=。

    因为：
      · `sys.stdout.reconfigure(errors="strict")` 与**根本不调用它完全等价**
        （strict 就是默认值），但它在源码里长得和真守卫一模一样；
      · `sys.stdout.reconfigure(errors="replace")` 虽然不崩了，
        但输出仍是 GBK 字节，被按 UTF-8 读的地方全是乱码 —— 实测表里的第二行。

    只查形状的断言会把这两种写法判成合格，那就成了一条**装饰性用例**：
    看着在守护，实际什么都守护不了。

    所以必须一路查到 `encoding=` 与 `errors=` 的**取值**上。
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "reconfigure":
            continue
        # 必须是 sys.stdout.reconfigure(...)，不是 sys.stderr 之类
        if not (isinstance(func.value, ast.Attribute) and func.value.attr == "stdout"):
            continue

        # reconfigure 的 encoding / errors 都是 keyword-only，所以只查 keywords
        kwargs = {
            kw.arg: kw.value.value
            for kw in node.keywords
            if kw.arg and isinstance(kw.value, ast.Constant)
        }
        if kwargs.get("encoding") == "utf-8" and kwargs.get("errors") in FORGIVING_ERROR_MODES:
            return True
    return False


def test_regression_p4_printing_scripts_guard_against_gbk_console():
    """P4 —— 会 print 的脚本必须给 stdout 兜底。

    真实触发（四次同一机制，见 harness-log #1 #2 #3 与 P4）：
    变异检查脚本 `scripts/mutate_check.py` 第一次运行，
    在**打印结果的那一行**抛 `UnicodeEncodeError: '\u2713'`，
    结果是一条结论都没输出就崩了 —— 而它明明已经跑完了全部检查。
    """
    offenders = [
        str(path.relative_to(ROOT)) for path, tree in _script_printers() if not _has_stdout_guard(tree)
    ]

    assert not offenders, (
        "以下脚本会 print，却没有给 stdout 兜底，在有非 ASCII 字符时会直接崩溃：\n  "
        + "\n  ".join(offenders)
        + "\n请在 import sys 之后加上（encoding 与 errors 缺一不可，见上面的实测表）：\n"
        + "    try:\n"
        + '        sys.stdout.reconfigure(encoding="utf-8", errors="replace")\n'
        + "    except Exception:\n"
        + "        pass\n"
        + "详见 docs/harness-log.md P4。"
    )


def test_regression_p4_the_scan_actually_found_printing_scripts():
    """元测试：确认上面的扫描不是"扫了 0 个脚本"造成的假绿。"""
    printers = _script_printers()
    names = sorted(str(p.relative_to(ROOT)) for p, _ in printers)

    assert len(printers) >= 5, f"只扫到 {len(printers)} 个会 print 的脚本：{names}"
    assert any(p.name == "mutate_check.py" for p, _ in printers), (
        "没扫到 mutate_check.py —— 要么它被改名了，要么 scripts/ 路径规则错了"
    )


# ================================================================
# 故障目录自检：变更记录必须能对上真实的参数改动
# ================================================================
#
# 真实缺陷（新增 F7 时踩到）：
#
#   故障定义里的 `changes`（写进变更日志的条目）和 `patches`（真正改哪些参数）
#   是**两份手写的清单**，它们之间唯一的联系就是那个环境变量名。写错了不会有
#   任何报错 —— 只会安静地写出 `"from": null, "to": null`。
#
#   后果不是"场景变难了"，而是**数据坏了**：Agent 看到的是一条没有数值的变更，
#   而它本该看到 `2000 → 1200`。而且**场景照样跑完、数据照样落盘**，
#   只有人去逐行读 changes.ndjson 才会发现。
#
#   原来的 `_knob_of` 是一张手维护的映射表（只有两条），新增故障时必须记得来加。
#   **"必须记得"就是这类缺陷的定义。** 现在改成按命名规则推导，
#   并且加了这道硬校验：推不出来就拒绝执行。


def test_every_fault_change_entry_resolves_to_a_real_patch():
    """故障目录自检：每条变更记录都要能对应上一次真实的参数改动。"""
    from scripts.inject_fault import FAULTS, _check_changes_are_resolvable

    problems = []
    for fid, fault in FAULTS.items():
        reason = _check_changes_are_resolvable(fault)
        if reason:
            problems.append(f"{fid}: {reason}")

    assert not problems, (
        "以下故障的变更记录取不到值 —— 会让变更日志写出 from/to = null"
        "（数据坏了，不是'难'）：\n  " + "\n  ".join(problems)
    )


def test_fault_catalog_selfcheck_actually_covers_something():
    """元测试：确认上面那条不是"目录是空的"造成的假绿。"""
    from scripts.inject_fault import FAULTS

    assert len(FAULTS) >= 7, f"故障目录只有 {len(FAULTS)} 条：{sorted(FAULTS)}"

    with_changes = [fid for fid, f in FAULTS.items() if f.changes]
    assert len(with_changes) >= 3, (
        f"只有 {len(with_changes)} 个故障带变更记录：{with_changes} —— "
        "校验这些条目的用例才有意义"
    )
    # 至少一条的 key 需要走别名（否则"推导规则"没被测到）
    assert "F2" in with_changes, "F2 的 W_ORDER_POOL_SIZE 需要走别名，必须被覆盖"


def test_knob_name_derivation_handles_the_awkward_names():
    """锁住推导规则本身：这几种写法都要能对上，否则变更日志会写出 null。"""
    from scripts.inject_fault import _knob_of

    assert _knob_of("W_ORDER_POOL_ACQUIRE_TIMEOUT_MS") == "pool_acquire_timeout_ms"
    assert _knob_of("W_INVENTORY_DOWNSTREAM_RETRIES") == "downstream_retries"
    assert _knob_of("W_ORDER_POOL_SIZE") == "pool_limit"          # 走别名
    assert _knob_of("W_PAYMENT_RISK_LATENCY_MS") == "risk_latency_ms"


def test_regression_20_f7_precondition_is_enforced_not_just_documented():
    """F7 的前提必须被**强制**，不能只在文档里提醒。

    F7 的全部价值在于"那条被记录的变更在因果上无关"，
    而它之所以无关，是因为池从来不会满（并发 < 池容量 64）。
    并发一旦 >= 池容量，池就会真的满、那条变更就真的生效 ——
    场景立刻退化成"变更就是根因"，也就是 F2 犯过的那个错。

    `harness-log #1` 的教训就是"靠人记得必然失效"，所以这里要求：
    **不满足前提时拒绝执行，而不是打印警告然后照跑。**
    """
    from scripts.inject_fault import _check_f7_precondition

    assert _check_f7_precondition(50) is None, "并发 50 < 64，应当通过"
    assert _check_f7_precondition(63) is None, "并发 63 < 64，边界内应当通过"

    reason = _check_f7_precondition(64)
    assert reason is not None, (
        "并发 64 = 池容量时池会被占满，那条变更就会真的生效 —— 必须拒绝执行"
    )
    assert "拒绝" in reason or "重跑" in reason or "并发" in reason, (
        f"拒绝理由必须说清怎么办，实际：{reason}"
    )


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
