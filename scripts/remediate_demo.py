"""M5 的症状级验证：**注入故障 → 量症状 → 经 MCP 门改回去 → 再量症状**。

这是 M5 唯一留白的那一步（"动作被执行了"不等于"问题解决了"）。

============================ 怎么让症状可信 ============================

不拿"感觉好了"当证据，而是拿两个**因果上直接相关**的量：

  1. **错误的下游调用数**（`downstream_calls_total{result="error"}` 的增量）——
     注入 F4 后 payment 的 `risk_error_rate=0.6` ⇒ 必然 > 0；把它改回 0 后应当 **= 0**（一票否决式信号）。
  2. **每笔订单的下游调用次数**（`downstream_calls_total{target="payment"}` 增量 ÷ 订单数）——
     注入 F4 后 `downstream_retries=5` ⇒ 失败会重试，比值明显 > 1；改回 1 后应当 **≈ 1**。

另外用一个**独立**的只读视图核对旋钮本身（`/_knobs`），避免"计量方式自己骗自己"。

============================ 三个必须说清的前提 ============================

· 需要世界在线（`docker compose ps` 三个服务健康 + Redis）。
· 会**真的**注入故障到本机世界里（可逆：F4 只改两个旋钮）。
· 改回去的动作**经被批准的 MCP 门**执行（`rca.mcp_client` → `mcp_server` → 策略点），
  不是绕过边界直连 —— 这正是 D17 那条「无旁路保证」守卫要求的路径。

用法：`python scripts/remediate_demo.py`（可选 `--fault F4`、`--orders 10`、`--skip-inject`）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import httpx                                                          # noqa: E402

from rca.remediation import apply as remediation_apply                # noqa: E402
from rca.remediation import propose, verify                           # noqa: E402

ORDER = "http://127.0.0.1:8080"
INVENTORY = "http://127.0.0.1:8081"
PAYMENT = "http://127.0.0.1:8082"
EVIDENCE = ROOT / "runs" / "_evidence" / "m5-live-remediation.json"


# ---------------------------------------------------------------- 世界观测

def world_is_up() -> dict:
    out: dict[str, str] = {}
    for name, url in (("order", ORDER), ("inventory", INVENTORY), ("payment", PAYMENT)):
        try:
            r = httpx.get(f"{url}/health", timeout=5.0)
            out[name] = "up" if r.status_code == 200 else f"http {r.status_code}"
        except Exception as exc:                                      # noqa: BLE001
            out[name] = f"{type(exc).__name__}"
    return out


def _counter(text: str, name: str, **labels: str) -> float:
    """从 Prometheus 文本里取一个计数器（只匹配包含给定标签的行）。"""
    total = 0.0
    for line in text.splitlines():
        if not line.startswith(name + "{") and not line.startswith(name + " "):
            continue
        if any(f'{k}="{v}"' not in line for k, v in labels.items()):
            continue
        try:
            total += float(line.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            continue
    return total


def read_world() -> dict:
    inv = httpx.get(f"{INVENTORY}/metrics", timeout=10.0).text
    return {
        "calls_ok": _counter(inv, "downstream_calls_total", target="payment", result="ok"),
        "calls_err": _counter(inv, "downstream_calls_total", target="payment", result="error"),
        "knobs": httpx.get(f"{INVENTORY}/_knobs", timeout=5.0).json(),
    }


def drive_traffic(orders: int) -> None:
    """发 `orders` 笔订单（数量固定，所以"每笔的下游调用次数"是可控的比值）。"""
    for _ in range(orders):
        try:
            httpx.post(f"{ORDER}/orders", json={"sku": "SKU-001", "qty": 2}, timeout=20.0)
        except Exception:                                             # noqa: BLE001
            pass          # 注入后本来就可能失败 —— 我要的是"下游被调了几次"


def measure(orders: int) -> dict:
    before = read_world()
    drive_traffic(orders)
    after = read_world()
    d_ok = after["calls_ok"] - before["calls_ok"]
    d_err = after["calls_err"] - before["calls_err"]
    return {
        "orders": orders,
        "downstream_calls_delta": d_ok + d_err,
        "downstream_calls_per_order": round((d_ok + d_err) / orders, 2),
        "error_calls_delta": d_err,
        "knobs": after["knobs"],
    }


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="M5 症状级验证（端到端）")
    ap.add_argument("--fault", default="F4")
    ap.add_argument("--orders", type=int, default=10)
    ap.add_argument("--skip-inject", action="store_true", help="世界已经注入过了，不再注入")
    ap.add_argument("--approved-by", default="barneyq（人工审批）")
    args = ap.parse_args()

    print("=" * 96)
    print(f"  M5 症状级验证：注入 {args.fault} → 量症状 → 经 MCP 门改回去 → 再量症状")
    print("=" * 96)

    up = world_is_up()
    print(f"\n  世界状态：{up}")
    if any(v != "up" for v in up.values()):
        print("  ✗ 世界不健康 —— 先 `docker compose up -d`（这一步无法离线替代）")
        return 2

    report: dict = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "fault": args.fault, "orders": args.orders, "world": up}

    if not args.skip_inject:
        print(f"\n  ── 注入 {args.fault}（用项目自己的注入器，可逆）──")
        proc = subprocess.run(
            [sys.executable, "scripts/inject_fault.py", "scenario", args.fault],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        print(f"     注入 exit={proc.returncode}  {(proc.stdout or '').strip().splitlines()[-1][:100] if proc.stdout else ''}")
        if proc.returncode != 0:
            print(f"     ✗ 注入失败：{(proc.stderr or '')[-300:]}")
            return 2

    print(f"\n  ── 注入后量症状（发 {args.orders} 笔订单）──")
    before = measure(args.orders)
    print(f"     每笔订单的下游调用次数 = {before['downstream_calls_per_order']}"
          f"   错误调用增量 = {before['error_calls_delta']}")
    print(f"     旋钮：retries={before['knobs'].get('downstream_retries')} "
          f"risk_error_rate={before['knobs'].get('risk_error_rate')}")
    report["before"] = before

    # ---- M5：提案 → 人审批 → 执行（经 MCP 门）
    changes = _latest_changes()
    print(f"\n  ── M5 提案（依据 {changes['source']} 的变更记录）──")
    proposals = propose(changes["records"])
    if not proposals:
        print("     ✗ 没有可提案的变更记录 —— 不猜修法，停在这里")
        return 2
    for p in proposals:
        print(f"     · {p.describe()}")
    report["proposals"] = [p.to_dict() for p in proposals]

    print("\n  ── 执行（经被批准的 MCP 门；无人审批则不执行）──")
    applied: list[dict] = []
    for p in proposals:
        out = remediation_apply(p, approved_by=args.approved_by)
        applied.append({"proposal": p.to_dict(), "applied": out.get("applied"),
                        "reason": out.get("reason", ""), "audit_written": out.get("audit_written"),
                        "result": out.get("result")})
        print(f"     · {p.knob} → {p.target_value}：applied={out.get('applied')} "
              f"{('（' + str(out.get('reason'))[:60] + '）') if not out.get('applied') else ''}")
    report["applied"] = applied

    print(f"\n  ── 改回去后再量症状（同样 {args.orders} 笔）──")
    after = measure(args.orders)
    print(f"     每笔订单的下游调用次数 = {after['downstream_calls_per_order']}"
          f"   错误调用增量 = {after['error_calls_delta']}")
    print(f"     旋钮：retries={after['knobs'].get('downstream_retries')} "
          f"risk_error_rate={after['knobs'].get('risk_error_rate')}")
    report["after"] = after

    v_ratio = verify(symptom="每笔订单的下游调用次数",
                     before=_to_int(before["downstream_calls_per_order"] * 100),
                     after=_to_int(after["downstream_calls_per_order"] * 100))
    v_err = verify(symptom="下游错误调用增量",
                   before=before["error_calls_delta"], after=after["error_calls_delta"])
    report["verdict"] = {"calls_per_order": v_ratio, "error_calls": v_err}

    print("\n  ── 验证结论（症状级）──")
    print(f"     · {v_ratio['symptom']}：{v_ratio['status']}"
          f"（{v_ratio.get('before', '?')}/100 → {v_ratio.get('after', '?')}/100）")
    print(f"     · {v_err['symptom']}：{v_err['status']}"
          f"（{v_err.get('before')} → {v_err.get('after')}）")

    # 两件事要分开报：**动作生效了吗**（读回确认）与**症状修好了吗**（前后对照）。
    # 第一次 live 跑时它们混在一起，于是"动作其实什么都没改"被当成了"修好了"。
    action_ok = bool(applied) and all(a.get("applied") for a in applied)
    symptom_ok = (v_ratio["status"] == "improved" and v_err["status"] == "improved"
                  and after["knobs"].get("downstream_retries") == 1
                  and float(after["knobs"].get("risk_error_rate") or 0) == 0.0)
    report["outcome"] = ("fixed" if symptom_ok else
                         ("action-ok-symptom-partial" if action_ok else "action-failed"))
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")

    print("\n  ── 结论（两件事分开报）──")
    print(f"     ① 动作生效了吗：{'✅ 是（读回确认）' if action_ok else '❌ 否'} —— "
          f"retries={after['knobs'].get('downstream_retries')}（期望 1）")
    print(f"     ② 症状修好了吗：{'✅ 是' if symptom_ok else '⚠️ 只部分改善'}")
    if action_ok and not symptom_ok:
        unrecorded = sorted({p["knob"] for p in report["proposals"]} ^
                            {"downstream_retries", "risk_error_rate"})
        print(f"        原因：注入的补丁里有的**没有变更记录** ⇒ M5 不提案（不猜修法）。"
              f"本次提案覆盖：{sorted(p['knob'] for p in report['proposals'])}")
        print(f"        世界里仍然偏着的旋钮：risk_error_rate="
              f"{after['knobs'].get('risk_error_rate')}（这个补丁那次场景没有留下记录）")
        _ = unrecorded
    print(f"\n  证据已写：{EVIDENCE.relative_to(ROOT)}")
    return 0 if symptom_ok else (3 if action_ok else 1)


def _to_int(x: float) -> int:
    return int(round(x))


def _latest_changes() -> dict:
    """取最近一次场景运行里的变更记录（M5 提案的**唯一**依据）。"""
    cands = sorted((ROOT / "runs").glob("r-2*/changes.ndjson"), key=lambda p: p.stat().st_mtime)
    if not cands:
        return {"source": "(无)", "records": []}
    path = cands[-1]
    records = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return {"source": path.parent.name, "records": records}


if __name__ == "__main__":
    raise SystemExit(main())
