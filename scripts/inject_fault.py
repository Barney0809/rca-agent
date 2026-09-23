"""
故障注入驱动 —— 施加六种故障、记录场景、产出可复现的数据。

============================ 用法 ============================

    python scripts/inject_fault.py list                 # 列出六种故障
    python scripts/inject_fault.py status               # 看三个服务当前参数
    python scripts/inject_fault.py baseline             # 无故障跑一轮（对照）
    python scripts/inject_fault.py scenario F2          # 施加 F2 跑一个完整场景
    python scripts/inject_fault.py apply F2             # 只施加，不跑流量（手工排查用）
    python scripts/inject_fault.py revert F2            # 撤销
    python scripts/inject_fault.py revert-all           # 把全部参数恢复默认

============================ 一个重要的设计：机械动作 vs 语义动作 ============================

每种故障有两组东西：

  patches —— **机械动作**：实际要改哪些服务的哪些参数。
             这是"为了让故障发生"必须做的事。

  changes —— **语义动作**：哪些改动**在现实世界里会留下变更记录**。
             这些才会写进变更事件日志，供 Agent 的"变更"数据源读取。

两者**故意不相等**。以 F2 为例：

  patches 要改两个地方：
      order.pool_limit      = 2    ← 配置变更（现实里会留痕）
      payment.risk_latency  = 800  ← 外部依赖劣化（现实里**不会**留痕）

  changes 只记一条：
      W_ORDER_POOL_SIZE: 8 → 2

于是 Agent 的变更数据源里**恰好只有 order 池被改小这一条** ——
它看起来完全像是根因，其实是**红鲱鱼**。真根因（payment 的外部依赖变慢）
在变更日志里根本没有记录，只能从指标里看出来。

**这就是"有变更 ≠ 是它"的设计**，也是 F2 必须靠交叉举证才能答对的原因。

============================ 产出 ============================

    runs/<run_id>/scenario.json     完整场景记录（含标准答案，供对账）
    runs/<run_id>/changes.ndjson    变更事件日志（**Agent 唯一能读到的变更来源**）

⚠️ `runs/` 已在 .gitignore 中：故障定义是代码（入库），运行数据是数据（不入库）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from world.loadgen.main import run_load  # noqa: E402

SERVICES = {
    "order": "http://127.0.0.1:8080",
    "inventory": "http://127.0.0.1:8081",
    "payment": "http://127.0.0.1:8082",
}
RUNS_DIR = ROOT / "runs"


# ================================================================
# 六种故障的定义（与 docs/04-故障目录.md 一一对应）
# ================================================================

@dataclass(frozen=True)
class Fault:
    id: str
    name: str
    patches: dict[str, dict]          # 机械动作：服务 -> 参数补丁
    changes: tuple[dict, ...]         # 语义动作：记入变更日志的条目
    symptom_at: str                   # 症状出现在哪个服务
    root_at: str                      # 根因在哪个服务
    cross_service: bool               # 症状与根因是否跨服务
    ground_truth: str                 # 标准答案
    expect: tuple[str, ...] = ()      # 期望在日志里看到的关键词（供人工核对）


FAULTS: dict[str, Fault] = {
    f.id: f
    for f in [
        Fault(
            id="F1",
            name="外部依赖变慢",
            patches={"payment": {"risk_latency_ms": 800}},
            changes=(),
            symptom_at="order", root_at="payment", cross_service=True,
            ground_truth="payment 的外部风控依赖延迟升高（≥800ms）",
            expect=("外部风控响应缓慢",),
        ),
        Fault(
            id="F2",
            name="连接池耗尽（被下游变慢放大）",
            patches={
                "order": {"pool_limit": 2, "pool_acquire_timeout_ms": 400},
                "payment": {"risk_latency_ms": 800},
            },
            # ⚠️ 只记池大小这一条 —— payment 的延迟劣化不是"配置变更"
            changes=({"target": "order", "key": "W_ORDER_POOL_SIZE"},),
            symptom_at="order", root_at="payment", cross_service=True,
            ground_truth="外部风控延迟升高；order 池耗尽是被放大的症状，不是根因",
            expect=("连接池等待", "连接池获取超时", "外部风控响应缓慢"),
        ),
        Fault(
            id="F3",
            name="本环节处理变慢",
            patches={"inventory": {"slow_op_ms": 600}},
            changes=(),
            symptom_at="order", root_at="inventory", cross_service=True,
            ground_truth="inventory 本环节处理变慢（其下游 payment 耗时正常）",
            expect=("库存预留成功",),
        ),
        Fault(
            id="F4",
            name="重试风暴（配置漂移）",
            # ⚠️ 重试只在【调用失败】时才发生。
            #    所以 F4 必须同时让 payment 失败 —— 否则重试 5 次和 1 次毫无区别
            #    （这是第一版设计漏掉的地方，实测 5xx=0、完全没有效果）。
            patches={
                "payment": {"risk_error_rate": 0.6},
                "inventory": {"downstream_retries": 5},
            },
            # 只记重试次数这一条：payment 的错误率上升是"外部依赖故障"，
            # 在现实世界里不是一次配置变更，所以不进变更日志。
            changes=({"target": "inventory", "key": "W_INVENTORY_DOWNSTREAM_RETRIES"},),
            symptom_at="payment", root_at="inventory", cross_service=True,
            ground_truth="inventory 的重试次数被从 1 改为 5，把失败流量放大 5 倍打给 payment",
            expect=("调用下游 payment 第",),
        ),
        Fault(
            id="F5",
            name="外部依赖报错",
            patches={"payment": {"risk_error_rate": 0.5}},
            changes=(),
            symptom_at="order", root_at="payment", cross_service=True,
            ground_truth="外部风控的错误率升高（耗时正常，错误率飙升）",
            expect=("外部风控调用失败",),
        ),
        Fault(
            id="F6",
            name="内存泄漏（对照组）",
            patches={"order": {"leak_mb_per_req": 2}},
            changes=(),
            symptom_at="order", root_at="order", cross_service=False,
            ground_truth="order 自身的内存泄漏（症状与根因同源，单 Agent 也能定位）",
            expect=("下单成功",),
        ),
        # ============================================================
        # F7 —— **合格的红鲱鱼场景**（harness-log #20 的产物）
        # ============================================================
        #
        # ## 为什么要有它
        #
        # F2 本来承担"红鲱鱼"这个职责，但对照实验证明**它出反了**：
        # 那条被当作"红鲱鱼"的变更（池 64→2）**就是真根因**，
        # 而声明的真根因（风控变慢）只是放大器。
        # 结果是**答对的被扣分、答错的被加分**。
        #
        # F7 是重做的版本，判据是：
        #     **那条被记录的变更，必须在因果上与事故无关。**
        # 而且这一点不能靠声明，必须能被两件事验证：
        #     ①  单独施加它 → 什么都不发生
        #     ②  单独施加真根因 → 事故出现
        #
        # ## 本场景的设计
        #
        #   真根因（**不**写进变更日志）：`payment.risk_latency_ms: 30 → 800`
        #   红鲱鱼（**写进**变更日志）：`order.pool_acquire_timeout_ms: 2000 → 1200`
        #
        # 那条红鲱鱼为什么在因果上无关？因为它**从来不会被触及**：
        #     负载并发 50 ⇒ order 侧最多 50 个请求同时在途 ⇒ 最多占 50 个连接；
        #     而池容量是 64 ⇒ **池永远不满 ⇒ 没有任何请求等待过连接
        #     ⇒ 那个"获取连接的等待上限"根本没有被读过**。
        #
        # 它又为什么像一个根因？因为它长得太像了：
        #     "有人在故障前一分钟把连接池的等待上限从 2000ms 砍到 1200ms"
        #     —— 一个只看变更日志的人会立刻得出"超时被调小导致请求失败"。
        #
        # 而**数据能证伪它**（这正是本场景要考的能力）：
        #     · 池从没满过（`pool_in_flight` 峰值 < 64）
        #     · 池耗尽次数 = 0、连接池等待 WARNING = 0
        #     · **HTTP 5xx = 0** —— 那条变更预言的是"请求失败"，而一个失败都没有
        #
        # ## 与 F1 构成一对严格对照 ★
        #
        # F7 的物理条件与 **F1 完全相同**（都只有风控变慢这一件事），
        # 唯一差别是**变更日志里多了一条无关变更**。
        # 所以 (F1, F7) 这一对能把"**是不是被无关变更带偏了**"单独隔离出来 ——
        # 而"抗不抗得住无关变更"恰恰是多 Agent 交叉举证机制声称要解决的问题。
        #
        # ⚠️ 前提必须被强制，不能靠文档提醒（#20 的教训）：
        #    **并发必须小于池容量（64）**，否则池会真的满、那条变更就会真的生效。
        #    见 `_check_f7_precondition()`。
        Fault(
            id="F7",
            name="无关变更干扰（红鲱鱼 / 真根因未记录）",
            patches={
                "order": {"pool_acquire_timeout_ms": 1200},
                "payment": {"risk_latency_ms": 800},
            },
            changes=({"target": "order", "key": "W_ORDER_POOL_ACQUIRE_TIMEOUT_MS"},),
            symptom_at="order", root_at="payment", cross_service=True,
            ground_truth=(
                "payment 的外部风控依赖延迟升高；"
                "order 的池获取超时被调小是与故障无关的变更"
                "（从未被触及：池从未满、零等待、零池耗尽、零 5xx）"
            ),
            expect=("外部风控响应缓慢",),
        ),
    ]
}


def _check_f7_precondition(concurrency: int, pool_limit: int = 64) -> str | None:
    """校验 F7 成立的前提。返回 None 表示通过，否则返回拒绝理由。

    F7 的全部价值在于"那条被记录的变更在因果上无关"。
    而它之所以无关，是因为**池从来不会满**：
        并发 N < 池容量 64 ⇒ 最多 N 个连接被占 ⇒ 没有请求等过连接
        ⇒ "池获取等待上限"这个参数根本没被读到。
    一旦 N >= 64，池就会真的满，那条变更就会真的生效，
    **场景立刻退化成"变更就是根因"——也就是 F2 犯过的那个错。**

    ⚠️ 所以这是个**硬前提，必须被强制**，不能只在文档里提醒一句。
       本仓库已经吃过一次"靠文档提醒"的亏（harness-log #1：靠人记得）。
    """
    if concurrency >= pool_limit:
        return (
            f"F7 要求并发 < order 池容量（{pool_limit}），当前并发 = {concurrency}。\n"
            "  原因：并发 >= 池容量时池会真的被占满，那条'池获取超时被调小'的变更\n"
            "        就会真的生效 —— 场景立刻退化成'变更就是根因'（F2 犯过的错）。\n"
            "  请用更低的并发重跑，例如 --concurrency 50。"
        )
    return None


# ================================================================
# 工具函数
# ================================================================

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_knobs(client: httpx.Client) -> dict[str, dict]:
    out = {}
    for name, base in SERVICES.items():
        try:
            out[name] = client.get(f"{base}/_knobs", timeout=10.0).json()
        except Exception as e:
            out[name] = {"error": str(e)}
    return out


def count_log_lines() -> int | None:
    """统计三个服务的日志总行数。

    ⚠️ 这是【累计值】（自容器启动以来），所以场景的净增量 = 之后 - 之前。
    没有别的进程在写日志时这个差值就是本场景的产出。
    """
    try:
        proc = subprocess.run(
            ["docker", "compose", "logs", "--no-log-prefix", "order", "inventory", "payment"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
        return len(proc.stdout.splitlines())
    except Exception:
        return None


def snapshot_metrics(run_dir: Path, tag: str) -> int:
    """抓一份三个服务的指标快照到 runs/<id>/metrics-<tag>.json。

    ============================ 为什么必须有两份快照 ============================

    Prometheus 的**计数器是自进程启动以来的累计值**。
    容器从 D1 起就没重启过 —— 所以直接 GET live `/metrics` 拿到的
    是**前面所有场景的叠加**。

    2026-09-24 踩过这个坑：F6 场景（只注入内存泄漏）的 Agent 从指标里读到
    了 F5/F4/F2 残留的 "3931 次风控错误、2495 次池耗尽"，
    于是得出了完全错误的结论，**D5 的全部结论因此作废**。
    见 docs/harness-log.md #12。

    有了 before/after 两份快照，采集层才能算出**本场景窗口内**的增量：
        计数器 → 差值（after - before）
        仪表   → 结束时的瞬时值
    """
    data: dict[str, str] = {}
    with httpx.Client(timeout=15.0) as client:
        for svc, base in SERVICES.items():
            try:
                data[svc] = client.get(f"{base}/metrics").text
            except Exception as exc:  # noqa: BLE001
                data[svc] = f"# __snapshot_error__ {type(exc).__name__}: {exc}"
    (run_dir / f"metrics-{tag}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return sum(len(v.splitlines()) for v in data.values())


def capture_logs(run_dir: Path, since_iso: str) -> dict[str, int]:
    """把本场景窗口内的日志逐服务抓进 runs/<id>/logs/<svc>.log。

    为什么要抓下来，而不是每次去问 docker：
      `docker compose logs` 返回的是**自容器启动以来的累计日志** ——
      跑的场景越多它越长，最后会慢到不可用；而且每次都要按时间窗再过滤一遍。
      抓一份只含本窗口的，后续降维处理快且干净。

    用 `--since <窗口开始时刻>` 限定范围。若某个 docker 版本不认这个参数，
    兜底逻辑是：文件为空 → 采集层会自动回退到直接问 docker。
    """
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    for svc in ("order", "inventory", "payment"):
        text = ""
        try:
            proc = subprocess.run(
                ["docker", "compose", "logs", "--no-log-prefix", "--since", since_iso, svc],
                cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
            )
            text = proc.stdout
        except Exception:
            text = ""
        (log_dir / f"{svc}.log").write_text(text, encoding="utf-8")
        counts[svc] = len(text.splitlines())

    return counts


def apply_patches(client: httpx.Client, fault: Fault) -> dict[str, dict]:
    """施加故障，返回每个服务实际发生变化的字段。

    ⚠️ /_inject 端点**不会往服务日志写任何东西** —— 这是刻意的，
       否则 Agent 只要 grep 一下就拿到答案了。
    """
    changed_by_service: dict[str, dict] = {}
    for svc, patch in fault.patches.items():
        try:
            r = client.post(f"{SERVICES[svc]}/_inject", json=patch, timeout=10.0)
            changed_by_service[svc] = r.json().get("changed", {})
        except Exception as e:
            changed_by_service[svc] = {"__error__": str(e)}
    return changed_by_service


def revert_patches(client: httpx.Client, fault: Fault) -> dict[str, dict]:
    """撤销故障：把故障涉及的字段恢复成未注入时的值。

    注意用的是"反向补丁"：把每个字段设回 0 / 默认值。
    这里写死默认值是有意的 —— 参数不多，写死比引入一套状态管理更不容易出错。
    """
    DEFAULTS = {
        "pool_limit": 64,          # 注意：inventory/payment 的默认是 4，见 compose
        "pool_acquire_timeout_ms": 2000,
        "slow_op_ms": 0,
        "leak_mb_per_req": 0.0,
        "risk_latency_ms": 30,
        "risk_error_rate": 0.0,
        "downstream_retries": 1,
    }
    # pool_limit 的默认值按服务区分（order=64, inventory/payment=4）
    POOL_DEFAULT = {"order": 64, "inventory": 4, "payment": 4}

    changed_by_service: dict[str, dict] = {}
    for svc, patch in fault.patches.items():
        revert = {}
        for k in patch:
            revert[k] = POOL_DEFAULT[svc] if k == "pool_limit" else DEFAULTS[k]
        try:
            r = client.post(f"{SERVICES[svc]}/_inject", json=revert, timeout=10.0)
            changed_by_service[svc] = r.json().get("changed", {})
        except Exception as e:
            changed_by_service[svc] = {"__error__": str(e)}
    return changed_by_service


# ================================================================
# 命令
# ================================================================

def cmd_list() -> int:
    print("=" * 96)
    print("  故障目录")
    print("=" * 96)
    print(f"  {'ID':<4} {'名称':<26} {'症状在':<10} {'根因在':<10} {'跨服务':<7} 记录变更")
    print("  " + "-" * 92)
    for f in FAULTS.values():
        print(
            f"  {f.id:<4} {f.name:<26} {f.symptom_at:<10} {f.root_at:<10} "
            f"{'是' if f.cross_service else '否(对照)':<7} "
            f"{'是' if f.changes else '否'}"
        )
    print()
    print("  ⚠️ '记录变更'=是 的才会出现在 Agent 能看到的变更日志里。")
    print("     它们的性质**各不相同**，不能一概而论：")
    print("       F4 —— 那条变更是**真根因**（重试次数 1→5）")
    print("       F7 —— 那条变更是**无关变更**（从未被触及），是合格的红鲱鱼")
    print("       F2 —— ⚠️ 原本按'红鲱鱼'设计，但对照实验证明**它出反了**：")
    print("              那条变更（池 64→2）**就是真根因**。见 harness-log #20。")
    print("              本场景**保留原样作为反例留档**，红鲱鱼职责已由 F7 承担。")
    return 0


def cmd_status() -> int:
    with httpx.Client() as client:
        knobs = read_knobs(client)
    print("=" * 96)
    print("  三个服务的当前参数")
    print("=" * 96)
    for name, k in knobs.items():
        print(f"  {name:<10} {json.dumps(k, ensure_ascii=False)}")
    return 0


def cmd_apply(fault_id: str) -> int:
    fault = FAULTS[fault_id]
    with httpx.Client() as client:
        changed = apply_patches(client, fault)
    print(f"已施加 {fault.id} {fault.name}")
    for svc, ch in changed.items():
        print(f"  {svc}: {json.dumps(ch, ensure_ascii=False)}")
    print()
    print("  记住跑完要 revert，否则下一个场景会被污染。")
    return 0


def cmd_revert(fault_id: str) -> int:
    fault = FAULTS[fault_id]
    with httpx.Client() as client:
        changed = revert_patches(client, fault)
    print(f"已撤销 {fault.id}")
    for svc, ch in changed.items():
        print(f"  {svc}: {json.dumps(ch, ensure_ascii=False)}")
    return 0


def cmd_revert_all() -> int:
    with httpx.Client() as client:
        for fault in FAULTS.values():
            revert_patches(client, fault)
        knobs = read_knobs(client)
    print("已把全部参数恢复默认：")
    for name, k in knobs.items():
        print(f"  {name:<10} {json.dumps(k, ensure_ascii=False)}")
    return 0


async def _run_scenario(
    fault: Fault | None, concurrency: int, duration_s: float, max_requests: int
) -> int:
    # ⚠️ 前提校验放在**建目录之前** —— 不通过就一个字节都不写，
    #    免得留下一个"看着像场景、其实前提不成立"的数据目录，
    #    被后来的评测当成有效场景用了。
    if fault is not None:
        # 任何故障都要先过这一关：变更清单必须能对上补丁清单（纯静态，不需要服务）
        reason = _check_changes_are_resolvable(fault)
        if reason:
            print("=" * 96)
            print("  拒绝执行：故障定义的变更记录取不到值")
            print("=" * 96)
            print(f"  {reason}")
            return 2

        if fault.id == "F7":
            reason = _check_f7_precondition(concurrency)
            if reason:
                print("=" * 96)
                print("  拒绝执行：F7 的前提不成立")
                print("=" * 96)
                print(f"  {reason}")
                return 2

    run_id = f"r-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    started_at = now_iso()

    label = f"{fault.id} {fault.name}" if fault else "baseline（无故障）"
    print("=" * 96)
    print(f"  场景 {run_id} —— {label}")
    print("=" * 96)

    with httpx.Client() as client:
        # ---------- 1. 记录基线 ----------
        print("\n[1/6] 读取基线")
        logs_before = count_log_lines()
        knobs_before = read_knobs(client)
        # ★ 指标快照 before：没有它就无法区分"本场景发生的事"与"历史累计"
        snapshot_metrics(run_dir, "before")
        print(f"      日志累计行数 {logs_before}  已抓指标快照 before")

        # ---------- 2. 施加故障 ----------
        print("\n[2/6] 施加故障")
        changed = {}
        if fault:
            changed = apply_patches(client, fault)
            for svc, ch in changed.items():
                print(f"      {svc}: {json.dumps(ch, ensure_ascii=False)}")
        else:
            print("      （无故障）")

        # ---------- 3. 写变更事件日志 ----------
        # 这是 Agent 的第三路数据源。只有"真实世界会留痕"的改动才写进去。
        print("\n[3/6] 写变更事件日志")
        change_records = []
        if fault:
            for ch in fault.changes:
                target = ch["target"]
                key = ch["key"]
                # 取"改动前"的真实值（从补丁结果里读，避免写死）
                actual = changed.get(target, {}).get(_knob_of(key))
                from_val = actual[0] if actual else None
                to_val = actual[1] if actual else None
                change_records.append({
                    "ts": now_iso(),
                    "target": target,
                    "key": key,
                    "from": str(from_val),
                    "to": str(to_val),
                    "by": "injector",
                })
        if change_records:
            with (run_dir / "changes.ndjson").open("w", encoding="utf-8") as fh:
                for rec in change_records:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    print(f"      {json.dumps(rec, ensure_ascii=False)}")
        else:
            (run_dir / "changes.ndjson").write_text("", encoding="utf-8")
            print("      （本故障在现实世界中不留变更记录）")

        # ---------- 4. 打流量 ----------
        # ⚠️ 用【请求数】控制数据量，而不是用时长。
        #    实测发现：系统冷/热状态下 RPS 差 2–3 倍（91→276），
        #    按固定时长跑出来的日志量不可复现。按请求数控制则稳定得多。
        #    duration_s 这里只作为**安全上限**（防止故障导致极慢时卡死）。
        print(f"\n[4/6] 打流量（{concurrency} 并发，目标 {max_requests} 个请求，"
              f"上限 {duration_s:.0f} 秒）")
        load = await run_load(
            concurrency=concurrency,
            duration_s=duration_s,
            max_requests=max_requests,
            verbose=False,
        )
        print(f"      请求 {load['total']}  成功 {load['ok']}  5xx {load['http_5xx']}  "
              f"错误 {load['error']}  RPS {load['rps']}  平均延迟 {load['avg_latency_ms']}ms")

        # ---------- 5. 撤销并收尾 ----------
        print("\n[5/6] 撤销故障并抓取快照")
        # ★ 指标快照 after 必须在**撤销之前**抓：
        #   撤销只改 Knobs（无日志、无指标变化），但把顺序写死能避免将来出错
        snapshot_metrics(run_dir, "after")
        if fault:
            revert_patches(client, fault)
            print("      已恢复默认")
        time.sleep(2)   # 等最后几行日志落盘
        logs_after = count_log_lines()
        log_counts = capture_logs(run_dir, started_at)
        print("      已抓指标快照 after；已抓日志："
              + "  ".join(f"{k}={v}行" for k, v in log_counts.items()))
        knobs_after = read_knobs(client)

    delta = None
    if logs_before is not None and logs_after is not None:
        delta = logs_after - logs_before

    print(f"      日志累计行数 {logs_after}（本场景净增 {delta}）")

    # ---------- 6. 写场景记录 ----------
    print("\n[6/6] 写场景记录")
    scenario = {
        "run_id": run_id,
        "fault_id": fault.id if fault else None,
        "name": fault.name if fault else "baseline",
        "started_at": started_at,
        "finished_at": now_iso(),
        "patches": fault.patches if fault else {},
        "changes": change_records,
        "symptom_at": fault.symptom_at if fault else None,
        "root_at": fault.root_at if fault else None,
        "cross_service": fault.cross_service if fault else None,
        "ground_truth": fault.ground_truth if fault else None,
        "expect_log_keywords": list(fault.expect) if fault else [],
        "load": load,
        "log_lines": delta,
        "captured_log_lines": log_counts,
        "knobs_before": knobs_before,
        "knobs_after": knobs_after,
    }
    (run_dir / "scenario.json").write_text(
        json.dumps(scenario, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"      {run_dir.relative_to(ROOT)}\\scenario.json")
    print(f"      {run_dir.relative_to(ROOT)}\\changes.ndjson")

    # ---------- 报告 ----------
    print()
    print("=" * 96)
    print("  场景结论")
    print("=" * 96)
    print(f"  症状出现在   {scenario['symptom_at']}")
    print(f"  根因在       {scenario['root_at']}")
    print(f"  跨服务       {'是' if scenario['cross_service'] else '否（对照组）'}")
    print(f"  标准答案     {scenario['ground_truth']}")
    print(f"  日志行数     {delta}  "
          f"{'✅ 达标（≥3 万）' if (delta or 0) >= 30000 else '⚠️ 未达 3 万，需调时长/并发'}")
    if fault and fault.expect:
        print()
        print("  人工核对：下面这条命令应该能搜到故障证据")
        kw = fault.expect[0]
        print(f"    docker compose logs --no-log-prefix order inventory payment | Select-String '{kw}'")
    print()
    return 0


# 少数历史上名字不一致的字段走别名表。
# 只有"环境变量名去掉前缀后 ≠ Knobs 字段名"的才需要列在这里。
_KNOB_ALIASES = {
    "pool_size": "pool_limit",          # W_ORDER_POOL_SIZE → pool_limit
}

_SERVICE_PREFIXES = ("ORDER", "INVENTORY", "PAYMENT")


def _knob_of(env_key: str) -> str:
    """把环境变量名映射成 Knobs 字段名。

    规则：去掉 `W_` 和 `W_<SERVICE>_` 前缀后小写，就是 Knobs 的字段名。

        W_ORDER_POOL_ACQUIRE_TIMEOUT_MS  →  pool_acquire_timeout_ms
        W_INVENTORY_DOWNSTREAM_RETRIES   →  downstream_retries
        W_ORDER_POOL_SIZE                →  pool_size → 别名 → pool_limit

    ⚠️ 这里**刻意不写一张手维护的映射表**（原来只有两条，就是这么写的）。

    真实缺陷：新增 F7 时用了 `W_ORDER_POOL_ACQUIRE_TIMEOUT_MS`，
    它不在那张表里，于是回退成 `w_order_pool_acquire_timeout_ms` ——
    查不到 → 变更日志写出 **`"from": null, "to": null`**。

    后果不是"场景变难了"，而是**数据坏了**：
    Agent 看到的是一条没有数值的变更，而它本该看到 `2000 → 1200`。
    更糟的是**没有任何报错** —— 场景照样跑完、数据照样落盘，
    只有人去逐行读 changes.ndjson 才会发现。

    所以除了改推导规则，调用方还必须**硬校验**（见 `_check_changes_are_resolvable`）：
    推导不出来就拒绝执行，而不是写个 null 下去。
    """
    name = env_key
    if name.startswith("W_"):
        name = name[2:]
    for svc in _SERVICE_PREFIXES:
        prefix = f"{svc}_"
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    field = name.lower()
    return _KNOB_ALIASES.get(field, field)


def _check_changes_are_resolvable(fault: Fault) -> str | None:
    """静态校验：故障声明的 `changes` 必须能对上它自己的 `patches`。

    这是**纯静态**的（不需要服务活着），所以可以放在最前面挡掉坏数据。

    为什么必须有这道校验：`changes` 和 `patches` 是**两份手写的清单**，
    它们之间唯一的联系是那个环境变量名 ——
    写错了不会有任何报错，只会安静地写出 `from/to = null`，
    然后污染整个场景数据（真实踩过，见 `_knob_of` 的注释）。
    """
    for ch in fault.changes:
        target = ch["target"]
        key = ch["key"]
        patch = fault.patches.get(target)
        if patch is None:
            return (
                f"{fault.id} 的变更记录指向 {target}，但该故障没有给 {target} 打任何补丁。\n"
                f"  变更日志里的条目必须对应一次**真实的**参数改动。"
            )
        field = _knob_of(key)
        if field not in patch:
            return (
                f"{fault.id} 的变更记录 {key} 推导出字段 {field!r}，"
                f"但它不在 {target} 的补丁里：{sorted(patch)}\n"
                f"  ⇒ 变更日志会写出 from/to = null（数据坏了，不是'难'）。\n"
                f"  请修 _knob_of 的别名表，或改故障定义里的 key。"
            )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="故障注入驱动")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出六种故障")
    sub.add_parser("status", help="看三个服务当前参数")
    sub.add_parser("revert-all", help="全部恢复默认")

    p_scen = sub.add_parser("scenario", help="跑一个完整场景")
    p_scen.add_argument("fault_id", nargs="?", default=None, help="F1..F6；省略则跑 baseline")
    p_scen.add_argument("--concurrency", type=int, default=50)
    # 默认按【请求数】控制数据量（可复现），duration 只作安全上限。
    # 实测每请求约 5 行日志，8000 个请求约产 4 万行，落在目标区间 3–8 万内。
    p_scen.add_argument("--max-requests", type=int, default=8000)
    p_scen.add_argument("--duration", type=float, default=180.0,
                        help="安全上限（秒）；故障导致极慢时兜底")

    p_apply = sub.add_parser("apply", help="只施加故障")
    p_apply.add_argument("fault_id")
    p_revert = sub.add_parser("revert", help="撤销故障")
    p_revert.add_argument("fault_id")

    args = parser.parse_args()

    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "revert-all":
        return cmd_revert_all()
    if args.cmd == "apply":
        return cmd_apply(args.fault_id)
    if args.cmd == "revert":
        return cmd_revert(args.fault_id)
    if args.cmd == "scenario":
        fault = FAULTS[args.fault_id] if args.fault_id else None
        if args.fault_id and args.fault_id not in FAULTS:
            print(f"未知故障 {args.fault_id}，用 list 看可选项")
            return 2
        return asyncio.run(
            _run_scenario(fault, args.concurrency, args.duration, args.max_requests)
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
