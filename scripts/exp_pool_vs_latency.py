"""对照实验台：用"控制变量"检验场景声明的标准答案**是不是真的**。

================================= 这个脚本要解决的问题 =================================

场景目录（`docs/04-故障目录.md`）给每种故障写了一个"标准答案"。
D2 验证了这些故障**可注入、可观测、可复现** —— 但**从来没有验证过
"声明的那个答案是不是真的答案"**。

这是整整缺失的一层验证。而且它一直没被要求过，直到 D7 出现这个结果：

    F1（多 Agent）→ 3/3 答对"外部风控变慢"
    F2（多 Agent）→ 0/3，三轮都说"池被改小 64→2 才是根因"

如果只看 LLM 的输出，有两种解释**都成立**：

    假设 A（机制缺陷）：它本来能答对，只是被变更记录带偏了 → 它错了
    假设 B（场景定错）：池被改小确实就是根因，场景声明的答案是错的 → 它对

**光看模型输出分不出来。** 必须绕开 LLM，直接问一个物理问题：

    把变量一个一个动，看事故到底是被谁触发的。

================================= 用法：控制变量，不是跑 LLM =================================

对每个场景，构造一个 2×2（或更多）的格：
    · 只动被声明为"根因"的那个变量
    · 只动被声明为"红鲱鱼/症状"的那个变量
    · 两个都动（= 原始场景）
    · 都不动（= 基线）

判读：**哪一个变量的移除能让事故消失，它就是根因。**

    · 移除后事故消失 ⇒ 必要因（but-for cause）
    · 单独保留就能造成事故 ⇒ 充分因

    实际参数不传 = 保持当前值不动。

================================= F2 的实测结果（本脚本的第一次使用）=================================

| 格 | pool_limit | risk_latency_ms | 池耗尽 | HTTP 5xx |
|---|---|---|---|---|
| 基线 | 64 | 30（正常） | 0 | 0 |
| **F1** | 64 | 800 | 0 | **0** |
| **只动池** ★ | **2** | **30（正常）** | **851** | **851（56%）** |
| **F2** | 2 | 800 | 1487 | 1487（97%） |

结论：**池改小单独就足以造成事故**（风控完全正常时也有 851 个 5xx），
风控变慢只是把失败率从 56% 抬到 97%。

⇒ F2 声明的答案「order 池耗尽是被放大的后果，不是根因」**与实测相反**。
⇒ **多 Agent 三轮给出的答案才是对的，而它被记成了 0/3。**

⚠️ 同时作废了 `docs/04` 里那句用来证明"朴素答案错"的理由
   「把池调大只会把崩溃推迟」—— 实测：池保持 64、风控同样 800ms，**5xx = 0**，
   不是"推迟"，是**根本不发生**。

================================= 关于指标：绝不用累计值 =================================

指标快照在负载前后各取一次，用 `rca.telemetry.collect.collect_metrics(run_dir)`
算**窗口增量** —— 直接 GET live `/metrics` 会读到自容器启动以来的累计值，
那正是 harness-log #12 那个让 D5 结论全部作废的坑。
这里复用项目自己的窗口化代码，而不是手写一遍。

================================= 用法 =================================

    # F2：补上缺失的那一格（最关键的一次实验）
    .\\.venv\\Scripts\\python.exe scripts\\exp_pool_vs_latency.py --pool-limit 2 --risk-latency-ms 30

    # F4：重试次数 vs 风控错误率
    .\\.venv\\Scripts\\python.exe scripts\\exp_pool_vs_latency.py --downstream-retries 5 --risk-error-rate 0
    .\\.venv\\Scripts\\python.exe scripts\\exp_pool_vs_latency.py --downstream-retries 1 --risk-error-rate 0.5

> 文件名里的 `pool_vs_latency` 来自第一次使用（F2）。它后来被泛化成通用实验台，
> 但改文件名要动 git 历史，而本仓库第 0 条禁止移动/删除 —— 所以名字保留，含义以此处为准。

⚠️ 本脚本**不写变更日志**（`/_inject` 端点本身也不写日志，这是刻意的设计）。
   它只用来回答物理问题，不产出场景数据。
⚠️ 结束时会把改过的参数**恢复成默认值**，避免污染后续场景。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# 控制台兜底（AGENTS.md 第 4 条"坑 4"）：encoding 与 errors 缺一不可。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx  # noqa: E402

from rca.telemetry.collect import (  # noqa: E402
    DEFAULT_SERVICES,
    SERVICE_URLS,
    collect_metrics,
)
from world.loadgen.main import run_load  # noqa: E402

# compose 里的默认值 —— 实验结束后恢复到这里的**被改过**的字段。
# 只恢复本次实验真正动过的键，别去动没碰过的参数（那会引入额外变量）。
DEFAULTS = {
    "order": {"pool_limit": 64, "pool_acquire_timeout_ms": 2000, "leak_mb_per_req": 0.0},
    "inventory": {"downstream_retries": 1, "slow_op_ms": 0},
    "payment": {"risk_latency_ms": 30, "risk_error_rate": 0.0},
}


def snapshot(tag: str, out_dir: Path) -> dict[str, str]:
    """抓一份三个服务的原始 /metrics 文本，落盘成快照。"""
    snaps: dict[str, str] = {}
    with httpx.Client(timeout=10.0) as c:
        for svc in DEFAULT_SERVICES:
            try:
                snaps[svc] = c.get(f"{SERVICE_URLS[svc]}/metrics").text
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠️ 抓 {svc} 指标失败：{exc}")
                snaps[svc] = ""
    (out_dir / f"metrics-{tag}.json").write_text(
        json.dumps(snaps, ensure_ascii=False), encoding="utf-8"
    )
    return snaps


def inject(patches: dict[str, dict]) -> None:
    with httpx.Client(timeout=10.0) as c:
        for svc, patch in patches.items():
            # 自报来源（ADR-0009）：留空 ⇒ 世界的历史里出现"匿名改动"，
            # 而那正是 #65 查不出根因的原因（这个脚本以前就是匿名的）。
            payload = {**patch, "by": "script:exp_pool_vs_latency"}
            r = c.post(f"{SERVICE_URLS[svc]}/_inject", json=payload)
            r.raise_for_status()
            print(f"  注入 {svc}: {patch}  → 实际变化 {r.json().get('changed')}")


def knobs(services: tuple[str, ...] = DEFAULT_SERVICES) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with httpx.Client(timeout=10.0) as c:
        for svc in services:
            try:
                out[svc] = c.get(f"{SERVICE_URLS[svc]}/_knobs").json()
            except Exception:  # noqa: BLE001
                out[svc] = {}
    return out


def _pick(view: dict[str, float], needle: str) -> dict[str, float]:
    return {k: v for k, v in view.items() if needle in k}


async def main() -> int:
    ap = argparse.ArgumentParser(description="对照实验台：检验场景声明的标准答案是不是真的")
    # 都不传 = 只跑基线（不改变任何参数）。实际传了哪个，就只动哪个。
    ap.add_argument("--pool-limit", type=int, default=None, help="order 的连接池容量（F2）")
    ap.add_argument("--pool-acquire-timeout-ms", type=int, default=None,
                    help="order 的池获取等待上限（F2 用 400）")
    ap.add_argument("--risk-latency-ms", type=int, default=None,
                    help="payment 的外部风控延迟（F1/F2 用 800；正常 30）")
    ap.add_argument("--risk-error-rate", type=float, default=None,
                    help="payment 的外部风控错误率（F4/F5 用 0.5；正常 0）")
    ap.add_argument("--downstream-retries", type=int, default=None,
                    help="inventory 调用下游的重试次数（F4 用 5；正常 1）")
    ap.add_argument("--leak-mb-per-req", type=float, default=None,
                    help="order 每次请求泄漏的 MB 数（F6 用 2；F8 用 0.5；正常 0）")
    ap.add_argument("--slow-op-ms", type=int, default=None,
                    help="inventory 本环节数据访问的额外延迟（F3 用 600；正常 0）")
    ap.add_argument("--concurrency", type=int, default=30)
    ap.add_argument("--max-requests", type=int, default=1500)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--label", default=None, help="给这次实验起个名字（默认自动生成）")
    args = ap.parse_args()

    # ---- 组装补丁：只包含真的传了的参数 ----
    order_patch: dict = {}
    if args.pool_limit is not None:
        order_patch["pool_limit"] = args.pool_limit
    if args.pool_acquire_timeout_ms is not None:
        order_patch["pool_acquire_timeout_ms"] = args.pool_acquire_timeout_ms
    if args.leak_mb_per_req is not None:
        order_patch["leak_mb_per_req"] = args.leak_mb_per_req

    payment_patch: dict = {}
    if args.risk_latency_ms is not None:
        payment_patch["risk_latency_ms"] = args.risk_latency_ms
    if args.risk_error_rate is not None:
        payment_patch["risk_error_rate"] = args.risk_error_rate

    inventory_patch: dict = {}
    if args.downstream_retries is not None:
        inventory_patch["downstream_retries"] = args.downstream_retries
    if args.slow_op_ms is not None:
        inventory_patch["slow_op_ms"] = args.slow_op_ms

    patches: dict[str, dict] = {}
    if order_patch:
        patches["order"] = order_patch
    if inventory_patch:
        patches["inventory"] = inventory_patch
    if payment_patch:
        patches["payment"] = payment_patch

    if not patches:
        print("没有指定任何参数 —— 那就只是跑一次基线（不会改变任何配置）。")
        print("用法示例：--pool-limit 2 --risk-latency-ms 30")
        label = args.label or "baseline-no-change"
    else:
        label = args.label or "-".join(
            f"{k}{v}" for svc in patches.values() for k, v in svc.items()
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "runs" / "_exp" / f"{label}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 92)
    print(f"  对照实验：{label}")
    for svc, patch in patches.items():
        print(f"    改 {svc}: {patch}")
    print(f"  输出目录：{out_dir.relative_to(ROOT)}")
    print("=" * 92)

    print("\n[0] 注入前的参数：")
    for svc, k in knobs().items():
        print(f"      {svc}: pool_limit={k.get('pool_limit')} "
              f"pool_acquire_timeout_ms={k.get('pool_acquire_timeout_ms')} "
              f"risk_latency_ms={k.get('risk_latency_ms')} "
              f"risk_error_rate={k.get('risk_error_rate')} "
              f"downstream_retries={k.get('downstream_retries')} "
              f"leak_mb_per_req={k.get('leak_mb_per_req')}")

    result: dict = {
        "label": label,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "patches": patches,
        "load": {
            "concurrency": args.concurrency,
            "max_requests": args.max_requests,
            "duration_s": args.duration,
        },
    }

    try:
        print("\n[1] 施加参数：")
        if patches:
            inject(patches)
        else:
            print("      （本次不改任何参数）")

        print("\n[2] 取指标快照（before）")
        snapshot("before", out_dir)

        print(f"\n[3] 打流量：并发 {args.concurrency}，最多 {args.max_requests} 个请求")
        load = await run_load(
            concurrency=args.concurrency,
            duration_s=args.duration,
            max_requests=args.max_requests,
            verbose=False,
        )
        result["load_result"] = load

        print("\n[4] 取指标快照（after）")
        snapshot("after", out_dir)

        print("\n[5] 窗口增量（只用本窗口的数据，不是累计值）")
        view = collect_metrics(out_dir)
        order_view = view.get("order", {})
        payment_view = view.get("payment", {})
        inventory_view = view.get("inventory", {})
        result["order_requests_total"] = _pick(order_view, "requests_total")
        result["order_pool_gauges"] = {
            k: v for k, v in order_view.items() if "pool_" in k and "requests" not in k
        }
        result["payment_risk"] = {k: v for k, v in payment_view.items() if "risk" in k}
        result["order_leak"] = {k: v for k, v in order_view.items() if "leak" in k}
        result["inventory_downstream"] = {
            k: v for k, v in inventory_view.items() if "downstream" in k
        }

        total = load.get("total", 0)
        ok = load.get("ok", 0)
        print(f"      请求总数 {total}  成功 {ok}  5xx {load.get('http_5xx')}  "
              f"4xx {load.get('http_4xx')}  错误 {load.get('error')}")
        print(f"      平均延迟 {load.get('avg_latency_ms', 0):.1f}ms  "
              f"吞吐 {load.get('rps', 0):.1f} rps")
        print("      order 的结果分布：")
        for k, v in sorted(result["order_requests_total"].items()):
            print(f"        {k} = {v:g}")
        for title, bucket in (
            ("order 的池仪表", result["order_pool_gauges"]),
            ("payment 的风控指标", result["payment_risk"]),
            ("order 的泄漏指标", result["order_leak"]),
            ("inventory 的下游调用", result["inventory_downstream"]),
        ):
            if bucket:
                print(f"      {title}：")
                for k, v in sorted(bucket.items()):
                    print(f"        {k} = {v:g}")

        # 结论提示 —— 只陈述数字，判定留给人/文档
        exhausted = sum(
            v for k, v in result["order_requests_total"].items() if "pool_exhausted" in k
        )
        result["pool_exhausted_count"] = exhausted
        result["http_5xx"] = load.get("http_5xx")
        print("\n[6] 本格结果：")
        print(f"      池耗尽次数 = {exhausted:g}      HTTP 5xx = {load.get('http_5xx')}")

    finally:
        print("\n[7] 恢复本次改过的参数（避免污染后续场景）")
        if patches:
            restore = {svc: {k: DEFAULTS[svc][k] for k in patch} for svc, patch in patches.items()}
            inject(restore)
        for svc, k in knobs().items():
            print(f"      {svc}: pool_limit={k.get('pool_limit')} "
                  f"pool_acquire_timeout_ms={k.get('pool_acquire_timeout_ms')} "
                  f"risk_latency_ms={k.get('risk_latency_ms')} "
                  f"risk_error_rate={k.get('risk_error_rate')} "
                  f"downstream_retries={k.get('downstream_retries')} "
                  f"leak_mb_per_req={k.get('leak_mb_per_req')}")

    result["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    (out_dir / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  结果已存：{(out_dir / 'summary.json').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
