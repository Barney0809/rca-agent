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
# 静态守卫：不许有**未定义的名字**（ruff F821）
# ================================================================
#
# 真实经历（同一个手误犯了三次）：
#
#   用"替换 def 行"的方式插入新函数时，忘了把原函数的 `def` 行拼回去。
#   结果是 —— **原函数的函数体被吞进了上一个函数**。
#
#   而且它**不会报语法错**：Python 不要求缩进回退，
#   所以 4 空格缩进的旧函数体紧接着新函数体，就被当成同一个函数的延续。
#   `ast.parse` 也认为合法。
#
#   后果：函数静默消失，调用它的地方要到**运行时**才 NameError。
#   第一次是 docstring 首行被删（语法错，立刻发现），
#   后两次都是这个"函数被吞"，其中一次是 `_convergence_breakdown`
#   被吞进了 `_print_stop_reasons` —— 报告一打印就会炸，
#   但如果那条路径没被测到，它会一直藏着。
#
# ⚠️ **手工"记得把 def 行拼回去"是纪律，纪律会失效（#1 的教训）。**
#    所以这里把它变成自动检查：未定义的名字在 ruff 里是 F821。
#
# 为什么只查 F821 而不查全部规则：其余规则（未用导入、行太长、重复导入）
# 是**风格**，把它们做成硬门禁会制造大量与缺陷无关的假红。
# 这一条不一样 —— 它对应的正是"代码已经坏了"。


def test_no_undefined_names_in_the_source_tree():
    """静态检查：src / eval / scripts / world / tests 里不许有未定义的名字。"""
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", ".", "--select", "F821", "--no-cache"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, (
        "发现未定义的名字（F821）—— 通常意味着有函数/变量在编辑时被吞掉了：\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


def test_no_blocking_calls_inside_async_functions():
    """静态检查：异步函数里不许出现阻塞调用（ASYNC 家族）。

    ⚠️ 这一条对本项目**特别要紧**：被诊断系统的三个服务全是 async 处理器，
    而本项目最依赖的观测量就是**延迟**。
    在 async 函数里调用 `time.sleep` / 阻塞式 HTTP，
    会**卡住整个事件循环** —— 于是：
      · 所有在途请求一起变慢（看起来像"池耗尽"或"下游变慢"）
      · 而且这种变慢**不反映任何被注入的故障**
    ⇒ 它会直接伪造出一个不存在的故障，或者掩盖一个真实的故障。

    实测：全仓库只有一处（`inject_fault.py` 场景末尾等日志落盘），
    已改成 `await asyncio.sleep`。`world/` 里当时是干净的。
    """
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", ".", "--select", "ASYNC", "--no-cache"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, (
        "异步函数里出现了阻塞调用 —— 它会卡住事件循环，"
        "从而**伪造出延迟类故障或掩盖真实故障**：\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


def test_the_undefined_name_check_actually_catches_something(tmp_path):
    """元测试：确认上面那条不是"ruff 没跑起来"造成的假绿。

    直接喂一段**确定含未定义名字**的代码给它，它必须报错。
    这也顺带证明了 ruff 在当前解释器里是可用的。
    """
    import subprocess
    import sys

    bad = tmp_path / "bad_f821_probe.py"
    bad.write_text("def f():\n    return never_defined_anywhere(1)\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(bad), "--select", "F821", "--no-cache"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode != 0, (
        "ruff 没有报出这个明显的未定义名字 —— 那么上面的守卫是假绿的。\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "F821" in (proc.stdout + proc.stderr)


# ================================================================
# 封堵清单对账（D9）：把"声称已封堵"变成机器可校验
# ================================================================
#
# 项目规则是「一条错误只有在回归用例能变红之后才算封堵」，
# `docs/harness-log.md` 的总览表逐条声明了状态。
#
# 但**声明和一个可执行检查是两件事**：表格写"🟢 已封堵"是人的断言，
# 而断言会过期 —— 代码改了、用例删了、变异组失效了，表格不会自己变红。
#
# `scripts/seal_report.py` 把两者对上。下面守的是它的**解析器**：
# 解析错了，对账就会得出假结论（比如"表格里没有任何声明，所以全都一致"）。


def _seal_mod():
    import sys as _sys

    root = Path(__file__).resolve().parent.parent
    if str(root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(root / "scripts"))
    import seal_report  # noqa: PLC0415

    return seal_report


def test_seal_report_parses_the_declared_statuses():
    """必须能读出表格里的封堵标记，且读到的条目够多。"""
    mod = _seal_mod()
    declared = mod.declared_statuses()

    assert len(declared) >= 20, (
        f"只从 harness-log 里读到 {len(declared)} 条声明：{sorted(declared)}\n"
        "表格格式可能变了，而解析器没跟上 —— 那会让对账静默失真。"
    )

    # 抽查几个性质不同的条目
    assert declared.get("#14") == mod.SEALED_MARK, "#14 应读成已封堵"
    assert declared.get("#20") == mod.PARTIAL_MARK, "#20 应读成部分封堵"
    assert declared.get("P4") == mod.SEALED_MARK, "P4 应读成已封堵"


def test_seal_report_covers_every_mutation_group():
    """每个变异组都必须标注它对应哪些 harness-log 条目。

    没有这个映射，对账就不知道该拿哪个变异组去验证哪条声明 ——
    整个机制会退化成"跑了一堆变异，但没有结论"。
    """
    import json

    root = Path(__file__).resolve().parent.parent
    spec = json.loads((root / "scripts" / "mutations.json").read_text(encoding="utf-8"))

    missing = [g for g, s in spec.items() if not s.get("harness_log")]
    assert not missing, f"这些变异组没有标注 harness_log：{missing}"

    all_items = {i for s in spec.values() for i in s["harness_log"]}
    assert len(all_items) >= 8, f"映射覆盖的条目太少：{sorted(all_items)}"


def test_seal_report_finds_no_unbacked_claims_any_more(tmp_path):
    """所有'已封堵'声明现在都有变异组背书 —— **这是 D9 补齐工作的验收**。

    ⚠️ 这条用例的第一版是反过来的：它断言"应该存在若干无背书的条目"，
       用来证明那个检查有东西可查。D9 把这 13 条补齐之后，
       它开始失败 —— 而**失败本身正是这项工作成功的证明**。

       所以现在改成：真实数据里**不许**再有没背书的声明（= 门禁），
       同时另配一条**合成用例**证明这个检查本身仍然能发现问题（见下一条）。
    """
    import json

    mod = _seal_mod()
    root = Path(__file__).resolve().parent.parent
    spec = json.loads((root / "scripts" / "mutations.json").read_text(encoding="utf-8"))

    unbacked = mod.unbacked_claims(mod.declared_statuses(), spec)
    assert unbacked == [], (
        f"以下条目声明为已封堵、却没有任何变异组背书：{unbacked}\n"
        "要么补一个变异组，要么把声明降级为 🟡（如实标注'靠人'）。"
    )

    # ⚠️ **防"空绿"**：如果解析器坏了（读不到任何声明），
    #    上面那句 `unbacked == []` 会**因为"没有声明可查"而通过** ——
    #    检查坏了却显示一切正常，正是本项目的头号大敌。
    #    所以这里再断言"确实读到了一大批声明"。
    declared = mod.declared_statuses()
    assert len(declared) >= 20, (
        f"只读到 {len(declared)} 条声明 —— 解析器很可能坏了，"
        "而那样的话上面的空清单毫无意义（空绿）"
    )


def test_seal_report_can_still_detect_an_unbacked_claim():
    """合成用例：喂一条假的"已封堵"声明给它，它**必须**发现。

    没有这一条，上面那条 `unbacked == []` 可能只是因为**检查坏了** ——
    比如解析器读不到任何声明（那就永远为空），或者映射逻辑写反了。
    """
    mod = _seal_mod()

    fake_declared = {"#99": mod.SEALED_MARK, "#14": mod.SEALED_MARK}
    fake_spec = {"some_group": {"harness_log": ["#14"]}}

    found = mod.unbacked_claims(fake_declared, fake_spec)

    assert found == ["#99"], (
        f"应当发现 #99 没有背书、而 #14 有；实际 {found}"
    )
    # 部分封堵（🟡）与未封堵（🔴）都不该出现在这个清单里
    assert mod.unbacked_claims({"#98": mod.PARTIAL_MARK, "#97": mod.OPEN_MARK}, {}) == []


# ================================================================
# 三条只有集成测试的封堵 → 补一道**离线结构守卫**
# ================================================================
#
# 背景（D9 做封堵对账时发现的）：
#
#   #5（库存耗尽伪装成"注入失效"）、#7（loadgen 超发 53%）、
#   #8（F4 完全没效果）三条，**只有 Docker 集成测试**：
#
#       tests/test_world.py::test_regression_5_stock_is_not_drained
#       tests/test_world.py::test_regression_7_loadgen_respects_max_requests
#       tests/test_world.py::test_regression_8_f4_retry_storm_multiplies_downstream_traffic
#
#   而副本变异**在原理上验证不了它们**：集成测试打的是**正在运行的容器**，
#   容器里跑的是真实源码，不是变异副本。所以"改代码 → 用例变红"这条路走不通。
#
# ⇒ 补三道**离线结构守卫**，把这三条修复的**不变量**搬进离线测试层。
#   它们不替代集成测试（端到端证据仍然是那三条），但它们让修复
#   **可以被持续、离线、可变异地校验**。
#
# ⚠️ 守卫的对象是"不变量本身"，不是"某一行代码"：
#   这样它不会因为无关重构而误报，而真正退回原缺陷时一定会红。


def _read_source(rel: str) -> str:
    return (Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")


def test_regression_5_default_stock_is_large_enough_to_survive_a_scenario():
    """#5 的不变量：**库存默认值必须大到不会在场景中途被抽干**。

    历史：默认库存是 1000，而一个场景要打几千个请求 ——
    库存耗尽后请求开始 409 短路，payment 断流，看起来像"故障注入没生效"。
    排查花了一小时（harness-log #5：**"注入失效"其实是被测系统自己先累死了**）。
    """
    import ast

    src = _read_source("world/inventory/main.py")
    tree = ast.parse(src)

    value = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "DEFAULT_STOCK":
                    value = node.value.value

    assert value is not None, "找不到 DEFAULT_STOCK —— 场景就没法保证库存不会被抽干"
    assert value >= 100_000, (
        f"DEFAULT_STOCK = {value}，太小了。一个场景会打几千个请求，"
        "库存被抽干会让请求 409 短路，并**伪装成'故障注入没生效'**（harness-log #5）"
    )


def test_regression_7_loadgen_worker_checks_the_request_cap_itself():
    """#7 的不变量：**worker 自己必须检查请求数上限**，不能只靠监控循环。

    历史：只靠"每 5 秒醒一次的监控循环"检查，结果**超发 53%**
    （目标 1200 实发 1841）——数据量就不可复现了，而"可复现"是 FR-C 的硬要求。
    """
    import ast

    src = _read_source("world/loadgen/main.py")
    tree = ast.parse(src)

    # ⚠️ 必须同时认 FunctionDef 与 AsyncFunctionDef ——
    #    `_worker` 是 `async def`，在 AST 里是 **AsyncFunctionDef**。
    #    （本文件里我已经在"按名字找用例"和"按名字找函数"上各栽过一次：
    #      `grep '^def test_'` 漏掉了所有 async 用例。别再靠记忆。）
    worker = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_worker"
        ),
        None,
    )
    assert worker is not None, "找不到 _worker"

    # 它必须在自己的循环里拿 max_requests 做比较
    compares_max = [
        n
        for n in ast.walk(worker)
        if isinstance(n, ast.Compare)
        and any(isinstance(x, ast.Name) and x.id == "max_requests" for x in n.comparators)
    ]
    assert compares_max, (
        "_worker 里没有用 max_requests 做比较 —— 上限只由监控循环检查的话，"
        "每 5 秒一次的空档足以让它超发 50% 以上（harness-log #7）"
    )


def test_regression_8_f4_injects_both_the_trigger_and_the_amplifier():
    """#8 的不变量：**F4 必须同时注入"触发条件"和"放大器"**。

    历史：F4 只改了 inventory 的重试次数（1 → 5），跑出来 5xx=0，与基线一模一样 ——
    **整个场景完全没有效果**。原因：重试只在**失败**时才触发，
    而那时 payment 从不失败，所以"重试 5 次"从来没被执行过。

    实测证据（`scripts/exp_pool_vs_latency.py --downstream-retries 5 --risk-error-rate 0`）：
    1529 个请求全部成功、0 个 5xx。
    """
    import sys as _sys

    root = Path(__file__).resolve().parent.parent
    if str(root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(root / "scripts"))
    from inject_fault import FAULTS  # noqa: PLC0415

    f4 = FAULTS["F4"]
    assert f4.patches["inventory"]["downstream_retries"] > 1, "F4 必须放大重试次数"
    assert f4.patches["payment"]["risk_error_rate"] > 0, (
        "F4 必须**同时**让 payment 报错。"
        "只改重试次数时，重试永远不会被触发（因为没有失败），场景完全没效果 —— harness-log #8"
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

# ================================================================
# #36 让"变异定义过期"**自动**被发现（不再靠人记得跑对账）
# ================================================================
#
# #25 已经咬人**三次**：源码一改，某个变异体的 find 串就失配，
# 而它会一直"假装通过"，直到有人去跑完整对账。
#
# 完整对账（建 24 次副本 + 跑 53 次 pytest）要十几分钟 ——
# **太贵 ⇒ 没人会在每次改源码后跑它 ⇒ 纪律注定失效。**
#
# 而"过期"根本不需要跑测试就能发现：只看 find 串在不在当前源码里。
# 那是**毫秒级**的纯文本检查，可以放进普通测试 —— 于是**每次跑测试都检查一遍**。
#
# ⇒ 原则：**把贵的检查拆一个便宜的近似版，让便宜的那个天天跑。**


def test_every_mutation_definition_still_applies():
    """每条变异定义的 `find` 串必须**在当前源码里恰好出现一次**。

    一旦它失配（源码被重构），这条用例就会红 ——
    不需要等十几分钟的完整对账，也不需要谁记得去跑。
    """
    import sys as _sys

    root = Path(__file__).resolve().parent.parent
    if str(root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(root / "scripts"))
    from mutate_check import verify_spec_applies  # noqa: PLC0415

    problems = verify_spec_applies()
    assert not problems, (
        "以下变异定义已**过期**（源码改了，定义没跟着改）——\n"
        "过期的变异体会一直『假装通过』，所以必须及时重新对准：\n  "
        + "\n  ".join(problems)
        + "\n（这正是 harness-log #25 描述的失败模式；修复后请再跑一次完整对账）"
    )


def test_the_applicability_check_actually_detects_staleness(tmp_path):
    """元测试：喂一条**故意失配**的变异定义给它，它必须发现。

    没有这一条，上面那句 `assert not problems` 可能只是因为**检查坏了**
    （例如它永远返回空清单）—— 那正是 #21 学到的教训。
    """
    import sys as _sys

    root = Path(__file__).resolve().parent.parent
    if str(root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(root / "scripts"))
    from mutate_check import verify_spec_applies  # noqa: PLC0415

    (tmp_path / "probe.py").write_text("x = 1\n", encoding="utf-8")
    bogus = {
        "bogus_group": {
            "harness_log": ["#00"],
            "mutations": [
                {"id": "not-there", "file": "probe.py", "find": "这串根本不存在"},
                {"id": "appears-twice", "file": "probe.py", "find": "x = 1"},
            ],
        }
    }
    # 让 "x = 1" 出现两次
    (tmp_path / "probe.py").write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")

    problems = verify_spec_applies(bogus, root=tmp_path)
    assert any("not-there" in p_ for p_ in problems), f"找不到的串没被发现：{problems}"
    assert any("appears-twice" in p_ for p_ in problems), f"出现两次的串没被发现：{problems}"


def test_a_group_without_a_harness_log_id_is_flagged(tmp_path):
    """没标注 `harness_log` 的组也要报 —— 否则对账时它无法归属到任何条目。"""
    import sys as _sys

    root = Path(__file__).resolve().parent.parent
    if str(root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(root / "scripts"))
    from mutate_check import verify_spec_applies  # noqa: PLC0415

    (tmp_path / "probe.py").write_text("x = 1\n", encoding="utf-8")
    problems = verify_spec_applies(
        {"no_id": {"mutations": [{"id": "m", "file": "probe.py", "find": "x = 1"}]}},
        root=tmp_path,
    )
    assert any("harness_log" in p_ for p_ in problems), problems
