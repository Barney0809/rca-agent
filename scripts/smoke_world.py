"""
被诊断系统的冒烟测试 —— 把"链路是通的"变成一条可重复执行的验收。

============================ 为什么需要它 ============================

"我刚才手动试了一下是好的"不是验收，因为：
  - 换个人不知道要试什么
  - 过几天再回来，不知道当初什么算通过
  - 改了代码后，没人会重新手试一遍

所以把上面那些手动步骤写成一个脚本，任何人都能一条命令跑出结论。

它验证五件事：
  1) 三个服务都活着，且能连上 Redis
  2) 完整链路能走通（order → inventory → payment → 外部风控）
  3) trace id 能跨三个服务正确传递  ★ 这是日志关联的前提
  4) 库存真的被扣减了
  5) 每个服务都暴露了 /metrics

运行：
    python scripts/smoke_world.py
    （需要被诊断系统已启动：docker compose up -d）

退出码 0 = 全部通过；非 0 = 有失败项。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx  # noqa: E402

ORDER_URL = os.environ.get("SMOKE_ORDER_URL", "http://127.0.0.1:8080")
INVENTORY_URL = os.environ.get("SMOKE_INVENTORY_URL", "http://127.0.0.1:8081")
PAYMENT_URL = os.environ.get("SMOKE_PAYMENT_URL", "http://127.0.0.1:8082")

SKU = "SKU-001"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f"  —— {detail}" if detail else ""))
    return ok


def section(t: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {t}")
    print("=" * 72)


def main() -> int:
    section("1. 三个服务的健康检查")

    healths: dict[str, dict] = {}
    for name, base in [("order", ORDER_URL), ("inventory", INVENTORY_URL), ("payment", PAYMENT_URL)]:
        try:
            r = httpx.get(f"{base}/health", timeout=10.0)
            data = r.json()
            healths[name] = data
            check(f"{name} 存活", r.status_code == 200, json.dumps(data, ensure_ascii=False))
            check(f"{name} 能连 Redis", bool(data.get("redis")), "")
        except Exception as e:
            healths[name] = {}
            check(f"{name} 存活", False, f"{type(e).__name__}: {e}")
            check(f"{name} 能连 Redis", False, "服务不可达")

    if len(FAIL) >= len(PASS):
        print("\n  ⚠️  服务不可达。先启动被诊断系统：")
        print("      docker compose up -d --build")
        return 1

    section("2. 走一遍完整链路")

    # 自己生成 trace id，这样我们能验证它是否被原样透传
    trace_id = f"smoke{uuid.uuid4().hex[:8]}"
    print(f"  本次使用的 trace id = {trace_id}")

    order_resp = None
    try:
        t0 = time.perf_counter()
        r = httpx.post(
            f"{ORDER_URL}/orders",
            json={"sku": SKU, "qty": 2},
            headers={"X-Trace-Id": trace_id},
            timeout=30.0,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        check("下单请求返回 2xx", r.status_code == 200, f"HTTP {r.status_code} 耗时 {elapsed_ms:.0f}ms")
        order_resp = r.json()
    except Exception as e:
        check("下单请求返回 2xx", False, f"{type(e).__name__}: {e}")

    if not order_resp:
        print("\n  链路未走通，后续检查跳过。")
        return 1

    print()
    print("  完整响应：")
    for line in json.dumps(order_resp, ensure_ascii=False, indent=2).splitlines():
        print("    " + line)

    section("3. 验证 trace id 跨服务传递")

    check("order 回显的 trace id 与我们发出的一致",
          order_resp.get("trace_id") == trace_id,
          f"发出={trace_id} 返回={order_resp.get('trace_id')}")

    detail = order_resp.get("detail") or {}
    check("inventory 层回显的 trace id 一致",
          detail.get("trace_id") == trace_id,
          f"{detail.get('trace_id')}")

    payment = detail.get("payment") or {}
    check("payment 层回显的 trace id 一致",
          payment.get("trace_id") == trace_id,
          f"{payment.get('trace_id')}")

    section("4. 验证业务结果确实发生了")

    check("order 状态为 CONFIRMED", order_resp.get("status") == "CONFIRMED",
          str(order_resp.get("status")))
    check("payment 状态为 CHARGED", payment.get("status") == "CHARGED",
          str(payment.get("status")))
    check("库存被扣减（remaining 被返回）", "remaining" in detail,
          f"remaining={detail.get('remaining')}")
    risk = payment.get("risk") or {}
    check("外部风控被调用", bool(risk), f"decision={risk.get('decision')} latency={risk.get('latency_ms')}ms")
    check("金额计算正确（qty*1999）", payment.get("amount_cents") == 2 * 1999,
          f"amount_cents={payment.get('amount_cents')}")

    section("5. 验证指标端点")

    for name, base in [("order", ORDER_URL), ("inventory", INVENTORY_URL), ("payment", PAYMENT_URL)]:
        try:
            r = httpx.get(f"{base}/metrics", timeout=10.0)
            ok = r.status_code == 200 and "requests_total" in r.text
            n_lines = len([ln for ln in r.text.splitlines() if ln and not ln.startswith("#")])
            check(f"{name} /metrics 可用", ok, f"{n_lines} 条指标")
        except Exception as e:
            check(f"{name} /metrics 可用", False, f"{type(e).__name__}: {e}")

    section("6. 验证日志里能按 trace 关联")

    print(f"  执行下面这条命令，应当看到三个服务的日志被同一个 trace 串起来：")
    print()
    print(f"    docker compose logs --no-log-prefix order inventory payment | Select-String '{trace_id}'")
    print()

    section("结论")
    print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print()
        print("  失败项：")
        for f in FAIL:
            print(f"    - {f}")
        return 1

    print("\n  被诊断系统就绪 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
