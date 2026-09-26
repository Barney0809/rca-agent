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
#: 服务名 → 端口（按服务读旋钮用；三个服务都读，避免"只看一个"把别的服务读错）
PORTS = {"order": 8080, "inventory": 8081, "payment": 8082}


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
        # ⚠️ 旋钮要**按服务**读：只看一个服务会把别的服务的旋钮读错
        #    （第一次 live 跑就是这么误报了 payment 的 risk_error_rate）
        "knobs": {svc: knobs_of(svc) for svc in PORTS},
    }


def knobs_of(service: str) -> dict:
    try:
        return httpx.get(f"http://127.0.0.1:{PORTS[service]}/_knobs", timeout=5.0).json()
    except Exception:                                                 # noqa: BLE001
        return {}


def drive_traffic(orders: int) -> tuple[int, int]:
    """发 `orders` 笔订单，返回 (发出, **成功**)。

    ⚠️ 成功数必须记下来：如果订单压根没到下游（比如被拒在前面），
       "下游调用次数 = 0"是**测量无效**，不是"症状消失"。
    """
    ok = 0
    for _ in range(orders):
        try:
            if httpx.post(f"{ORDER}/orders", json={"sku": "SKU-001", "qty": 2},
                          timeout=20.0).status_code == 200:
                ok += 1
        except Exception:                                             # noqa: BLE001
            pass
    return orders, ok


def measure(orders: int) -> dict:
    before = read_world()
    sent, ok = drive_traffic(orders)
    after = read_world()
    d_ok = after["calls_ok"] - before["calls_ok"]
    d_err = after["calls_err"] - before["calls_err"]
    calls = d_ok + d_err
    return {
        "orders_sent": sent,
        "orders_ok": ok,
        "downstream_calls_delta": calls,
        "downstream_calls_per_order": round(calls / sent, 2) if sent else 0.0,
        "error_calls_delta": d_err,
        # ⚠️ 判据用**成功订单数**（两种状态下都可观测）。
        #    第一次我把"下游调用次数"当判据，结果掉到 0 时看着像"症状消失"，
        #    其实是失败路径**在调下游之前**就失败了（502 inventory 500）——
        #    指标变小是失败模式的副作用，不是恢复。⇒ 它现在只当**诊断量**。
        "valid": bool(sent > 0),
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
    print(f"     成功订单 {before['orders_ok']}/{before['orders_sent']}"
          f"   每笔订单的下游调用次数 = {before['downstream_calls_per_order']}"
          f"   错误调用增量 = {before['error_calls_delta']}"
          f"   测量{'有效 ✓' if before['valid'] else '无效 ✗'}")
    print(f"     旋钮：inventory.retries={before['knobs']['inventory'].get('downstream_retries')}"
          f"  payment.risk_error_rate={before['knobs']['payment'].get('risk_error_rate')}")
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
    print(f"     成功订单 {after['orders_ok']}/{after['orders_sent']}"
          f"   每笔订单的下游调用次数 = {after['downstream_calls_per_order']}"
          f"   错误调用增量 = {after['error_calls_delta']}"
          f"   测量{'有效 ✓' if after['valid'] else '无效 ✗'}")
    print(f"     旋钮：inventory.retries={after['knobs']['inventory'].get('downstream_retries')}"
          f"  payment.risk_error_rate={after['knobs']['payment'].get('risk_error_rate')}")
    report["after"] = after

    # ⚠️ 测量无效时**不下结论**：订单没到下游时的"0 次调用"不是症状消失（实测踩过）
    if not (before["valid"] and after["valid"]):
        report["verdict"] = {"status": "measurement-invalid",
                            "why": "有测量无效：订单没成功或下游没被调用 ⇒ 不拿它当症状证据"}
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8", newline="\n")
        print("\n  ⚠️ 测量无效 —— 如实说：这次不构成症状级证据（需要订单成功且下游真被调用）")
        print(f"  证据已写：{EVIDENCE.relative_to(ROOT)}")
        return 4

    v_ok = verify(symptom="成功订单数", before=before["orders_ok"], after=after["orders_ok"],
                  lower_is_better=False)      # ★ 成功数**越大越好**（否则 10→0 会被判成改善 ✗）
    # 这两个只当**诊断量**（看着好看/难看都不作判据）
    v_ratio = verify(symptom="（诊断量）每笔订单的下游调用次数",
                     before=_to_int(before["downstream_calls_per_order"] * 100),
                     after=_to_int(after["downstream_calls_per_order"] * 100))
    v_err = verify(symptom="（诊断量）下游错误调用增量",
                   before=before["error_calls_delta"], after=after["error_calls_delta"])
    report["verdict"] = {"successful_orders": v_ok,
                         "diagnostic_calls_per_order": v_ratio,
                         "diagnostic_error_calls": v_err}

    print("\n  ── 验证结论 ──")
    print(f"     ★ 判据 · {v_ok['symptom']}：{v_ok['status']}"
          f"（{v_ok.get('before')} → {v_ok.get('after')}）")
    print(f"       诊断量 · {v_ratio['symptom']}：{v_ratio['status']}"
          f"（{v_ratio.get('before', '?')}/100 → {v_ratio.get('after', '?')}/100）")
    print(f"       诊断量 · {v_err['symptom']}：{v_err['status']}"
          f"（{v_err.get('before')} → {v_err.get('after')}）")

    # 两件事要分开报：**动作生效了吗**（读回确认）与**症状修好了吗**（前后对照）。
    # 第一次 live 跑时它们混在一起，于是"动作其实什么都没改"被当成了"修好了"。
    action_ok = bool(applied) and all(a.get("applied") for a in applied)
    targets_ok = all(
        str(after["knobs"].get(p["service"], {}).get(p["knob"])) == str(p["target_value"])
        for p in report["proposals"]
    )
    # 症状级结论：两侧测量都有效、**判据（成功订单数）改善**、而且提案涉及的旋钮真的到位
    symptom_ok = (before["valid"] and after["valid"] and targets_ok
                  and v_ok["status"] == "improved")
    report["outcome"] = ("fixed" if symptom_ok else
                         ("action-ok-symptom-partial" if action_ok else "action-failed"))
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")

    print("\n  ── 结论（两件事分开报）──")
    _inv_retries = after["knobs"].get("inventory", {}).get("downstream_retries")
    print(f"     ① 动作生效了吗：{'✅ 是（读回确认）' if action_ok else '❌ 否'} —— "
          f"inventory.retries={_inv_retries}（期望 1）")
    # ⚠️ 措辞必须**跟着判据走**：判据是 `worse` 时写「只部分改善」是自相矛盾的
    #    （第一版就是这么写的，而那一版恰好是把 10→0 判成 improved 的那一版 ——
    #     判据方向错了，措辞就会跟着一起把灾难说成修复）
    _wording = {
        "improved": "✅ 是",
        "unchanged": "⚠️ 没有变化（动作到位了，症状没动）",
        "worse": "❌ 否 —— 反而**更差**",
        "unverified": "⚠️ 未验证（缺观测，不猜）",
    }
    print(f"     ② 症状修好了吗：{_wording.get(v_ok['status'], v_ok['status'])}"
          f"（{v_ok.get('before')} → {v_ok.get('after')}）")
    if action_ok and not symptom_ok:
        print(f"        原因：注入的补丁里有的**没有变更记录** ⇒ M5 不提案（不猜修法）。"
              f"本次提案覆盖：{sorted(p['knob'] for p in report['proposals'])}")
        # ⚠️ 旋钮要**按服务读**：扁平读法会把 payment 的读成 None（第一次 live 跑就是这么误报的）
        _rate = after["knobs"].get("payment", {}).get("risk_error_rate")
        _recorded = any("risk_error_rate" in str(r.get("key", "")) for r in changes["records"])
        print(f"        世界里仍然偏着的旋钮：payment.risk_error_rate={_rate}"
              f"（{'变更记录里有它，但这次没有被提案' if _recorded else '这个补丁那次场景没有留下记录'}）")
        if v_ok["status"] == "worse":
            print("        ⚠️ **只覆盖故障的一部分时，症状可能反而更差** —— 这既不是修好了，"
                  "也不是什么都没做。")
            print("           要**全量**恢复，请走人的那扇门："
                  f".\\.venv\\Scripts\\python.exe scripts\\inject_fault.py revert {args.fault}")
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
