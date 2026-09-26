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
from rca.remediation import (                                         # noqa: E402
    knob_name_from_env,
    precheck_metric_signal,
    propose,
    verify,
)

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


def fault_is_armed(records: list[dict], knobs: dict | None = None) -> tuple[bool, str]:
    """核对**故障真的施加上了**：每条变更记录说"被改成 `to`"，世界现在就必须是那个值。

    ⚠️ 为什么要有这一步（#63 的实测）：注入器有两种模式 ——
       `scenario` 打完流量会在 [5/6] **撤销故障**（免得污染下一个场景），`apply` 只施加。
       少做一步，就会在一个**健康**世界上量出"故障态"，
       然后把"没有症状"读成"修好了" —— 又一个"用漂亮的措辞报告不成立的结论"。
       所以**动手之前**先把这件事量一次；量不上就停在这里，世界一个字节都不动。

    `knobs` 可以注入（`{服务: {旋钮: 值}}`）—— 那样这条判断就能**离线被测**，
    否则它只能靠"真跑一次世界"来体现，而真跑一次要两分钟、还要往世界里注入故障。
    """
    if knobs is None:
        knobs = {svc: knobs_of(svc) for svc in PORTS}
    if not records:
        return False, "没有变更记录 ⇒ 无从核对故障是否施加（也不会有可提案的依据）"
    for rec in records:
        svc = str(rec.get("target") or "")
        name = knob_name_from_env(str(rec.get("key") or ""), svc)
        if not (svc and name):
            continue
        now = knobs.get(svc, {}).get(name)
        if str(now) != str(rec.get("to")):
            return False, (f"{svc}.{name} 现在是 {now!r}，而变更记录说它应当被改成 "
                           f"{rec.get('to')!r} ⇒ 故障**没有**施加在这个世界上")
    return True, f"变更记录里 {len(records)} 条改动，在世界里都对得上 ✓（故障确实在）"


def metrics_disagree(*, user_facing: dict, causal_aligned: list[dict]) -> bool:
    """**两把尺子分歧**：用户可感的症状没有变好，而"与故障因果对齐"的量都变好了。

    ⚠️ 为什么单独立一个纯函数（#64 的教训）：
       这条判定原来**直接写在 `main()` 里**，验证方式是"跑一次 live、看打印"——
       而那是"我跑过"，不是"它被守住了"：若它恒为 `False`，**全套测试一条都不会红**。
       抽出来才能离线测、才能配变异体。

    ⚠️ 口径（ADR-0008）：★ 判据是**用户可感**的那个量；因果对齐的量只作**解释**。
       两者方向相反时**不许只报一把** —— 只报用户侧会漏掉"故障其实被削弱了"，
       只报因果侧会把灾难说成修复。

    ⚠️ `causal_aligned` 为空时**必须返回 False**：Python 的 `all([])` **恒为 True**，
       不显式挡住就会在"一把尺子都没有"的情况下报出"分歧" —— 又一个空绿。
    """
    return (str(user_facing.get("status")) != "improved"
            and bool(causal_aligned)
            and all(str(m.get("status")) == "improved" for m in causal_aligned))


def _svc_knob(p: object) -> tuple[str, str]:
    """从 `Proposal`（或同形状的 dict）里取 `(service, knob)` —— 两种都认，方便离线测。"""
    if isinstance(p, dict):
        return str(p.get("service", "")), str(p.get("knob", ""))
    return str(getattr(p, "service", "")), str(getattr(p, "knob", ""))


def patch_coverage(patches: dict, proposals: list) -> dict:
    """**故障的补丁**里有多少个旋钮被提案覆盖了？

    ⚠️ 为什么要有这一步（HANDOFF §4 那条"新认识"，现在做成**机制**而不是一句打印）：

      故障补丁与变更记录**不是一回事**：补丁是"实际改了什么"，变更记录只是
      "现实世界里会留下痕迹的那部分"（注入器的原话）。M5 的铁律是**不猜修法** ——
      只依据变更记录提案。两者差额 ⇒ 这次**只覆盖故障的一部分**。

      F4 就是这么设计的：补丁改两个旋钮（`inventory.downstream_retries` 有记录、
      `payment.risk_error_rate` 没有），于是"修完"仍然有一半故障在。
      而**只修一半可能比不修更差**（被撤掉的那个旋钮可能正在兜住另一半）——
      所以必须在**动手之前**就把这件事告警出来，而不是事后补一句解释。

    ⚠️ 覆盖是**按 (服务, 旋钮)** 算的，不看值：`Proposal` 已经带着翻译好的旋钮名。
    ⚠️ 没有补丁（例如 baseline 场景）时返回 `full` —— 不许把"没有故障"说成"只修了一半"。
    """
    want = sorted({(str(svc), str(knob)) for svc, knobs in (patches or {}).items()
                   for knob in (knobs or {})})
    got = {_svc_knob(p) for p in proposals or []}
    covered = [x for x in want if x in got]
    uncovered = [x for x in want if x not in got]
    return {
        "patched": want,
        "covered": covered,
        "uncovered": uncovered,
        "fraction": round(len(covered) / len(want), 3) if want else 1.0,
        "verdict": "full" if not uncovered else "partial",
    }


def remediation_outcome(*, action_ok: bool, symptom_status: str, coverage: str) -> str:
    """把「动作 / 症状 / 覆盖」三件事压成一个**机器可读**的结局标签。

    ⚠️ 为什么要一个函数（#64 的教训）：这个标签原来是一句写死的三元表达式，
      对 `worse` 结果只会说 `action-ok-symptom-partial`（"部分"听起来像"部分改善"）——
      措辞软到能把灾难读成进展。抽成函数才能离线测、才能配变异体。

    词表（自描述，谁都读得懂）：

      `action-failed`                                    动作没生效（其余不论）
      `fixed`                                            动作生效 + 症状改善 + **全覆盖**
      `fixed-partial-coverage`                           动作生效 + 症状改善，但只覆盖了一部分故障
      `action-ok-symptom-<status>`                       动作生效、症状 **没有** 改善（worse/unchanged/…）
      `…-partial-coverage`                               再叠上"只覆盖了一部分"

    ⚠️ 症状改善时也**必须**带上覆盖信息：只修了一半却说 `fixed`，就是把
      "另一半还在"这件事藏起来。
    """
    if not action_ok:
        return "action-failed"
    base = "fixed" if str(symptom_status) == "improved" else f"action-ok-symptom-{symptom_status}"
    return f"{base}-partial-coverage" if str(coverage) == "partial" else base


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
    ap.add_argument("--skip-inject", action="store_true",
                    help="世界**已经处于故障态**（例如已用 apply 施加过）时不跑注入；"
                         "核对不上「故障真的在」就拒绝：不下结论、也不动世界")
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
        # ⚠️ **两步，缺一不可**（harness-log #63 —— 第一版只做了第一步，于是
        #    "故障态"其实是在一个**健康**世界上量的，量出 10/10 全部成功）：
        #    ① `scenario`：打一遍真实流量，并留下**变更记录**（M5 提案的唯一依据）。
        #      但它 [5/6] 会把故障**撤销**（为了不污染下一个场景）⇒ 跑完世界是干净的。
        #    ② `apply`：把故障**再施加回去** —— 这样"故障态"才是真的故障态。
        for step in ("scenario", "apply"):
            proc = subprocess.run(
                [sys.executable, "scripts/inject_fault.py", step, args.fault],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            print(f"     {step:<9} exit={proc.returncode}")
            if proc.returncode != 0:
                print(f"     ✗ {step} 失败：{(proc.stderr or '')[-300:]}")
                return 2

    # 变更记录是 M5 提案的**唯一**依据；顺便用它核对"故障真的施加上了没有"
    changes = _latest_changes()

    # ⚠️ 动手之前先量一次**世界真的坏了吗**（#63）：注入器有两种模式，少做一步
    #    就会拿一个健康世界当"故障态"，然后把"没症状"读成"修好了"。
    armed, why = fault_is_armed(changes["records"])
    print(f"\n  ── 核对故障是否真的在（依据 {changes['source']}）──")
    print(f"     {why}")
    if not armed:
        print("     ✗ 故障没施加上 ⇒ **不下结论、也不动世界**（先修注入，再量症状）")
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
    print(f"\n  ── M5 提案（依据 {changes['source']} 的变更记录）──")
    proposals = propose(changes["records"])
    if not proposals:
        print("     ✗ 没有可提案的变更记录 —— 不猜修法，停在这里")
        return 2
    for p in proposals:
        print(f"     · {p.describe()}")
    report["proposals"] = [p.to_dict() for p in proposals]

    # ★ 覆盖：**动手之前**就量清楚"这次能修到故障的几分之几"（HANDOFF §4 的机制化）
    cov = patch_coverage(_fault_patches(args.fault), proposals)
    report["coverage"] = cov
    print(f"     · 覆盖：{len(cov['covered'])}/{len(cov['patched'])} 个旋钮（{cov['verdict']}）")
    if cov["verdict"] == "partial":
        print("     ⚠️ **这次只覆盖故障的一部分** —— 补丁里有、但没有可用的变更记录的那部分"
              f"**不会被提案**（M5 不猜修法）："
              f"{['.'.join(x) for x in cov['uncovered']]}")
        print("        ⇒ 预期是**部分修复**，而且症状**可能反而更差**"
              "（被撤掉的那个旋钮可能正在兜住另一半故障）")
        print("        ⇒ 要**全量**恢复，请走**人的那扇门**："
              f".\\.venv\\Scripts\\python.exe scripts\\inject_fault.py revert {args.fault}")

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

    # ★ 前置检查：**两种状态下指标都必须有信号**，否则不许下结论（#61 之后的第二种错法）
    #   它是纯函数、可离线测；在这里的作用是"宁可不下结论，也不给一个没依据的结论"。
    pre = precheck_metric_signal(symptom="成功订单数",
                                 sent_before=before["orders_sent"], ok_before=before["orders_ok"],
                                 sent_after=after["orders_sent"], ok_after=after["orders_ok"])
    report["precheck"] = pre
    print("\n  ── 前置检查（这个指标有没有信号）──")
    print(f"     {pre['why']}")
    if not pre["ok"]:
        report["verdict"] = {"status": "measurement-no-signal", "why": pre["why"]}
        report["outcome"] = "measurement-no-signal"
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8", newline="\n")
        print("\n  ✗ 前置检查未通过 ⇒ **不下结论**（构造不出有依据的结论，就不给结论）")
        print(f"  证据已写：{EVIDENCE.relative_to(ROOT)}")
        return 5

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
    # ★ **两把尺子分歧**：必须一起读，不许只挑好看的那把报（同 D10 的原则 ——
    #   关键词与裁判分歧时，分歧本身就是最有信息量的东西）。
    #   ⚠️ 这不是"谁对谁错"：判据（成功订单数）是**用户可感**的，
    #   诊断量（下游放大倍数 / 错误调用）是**与故障因果对齐**的；
    #   重试既放大流量、也把失败订单救回来 —— 撤掉它，两者会朝相反方向动。
    #   ⚠️ 判定本身是纯函数（可离线测 + 有变异体），别把它塞回 main() 里（#64）。
    _disagree = metrics_disagree(user_facing=v_ok, causal_aligned=[v_ratio, v_err])
    report["metrics_disagree"] = {
        "value": _disagree,
        "why": ("用户可感的症状（成功订单数）没有变好，而与故障因果对齐的量"
                "（下游放大倍数 / 错误调用）变好了 —— 重试既是放大流量的机制，"
                "也是把失败订单救回来的机制：撤掉它，流量降下来，被救的订单也没了"
                if _disagree else "两把尺子方向一致（或都没改善）"),
    }
    report["outcome"] = remediation_outcome(action_ok=action_ok, symptom_status=v_ok["status"],
                                            coverage=cov["verdict"])
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
        print(f"        本次提案覆盖：{['.'.join(x) for x in cov['covered']]}"
              f"（补丁共 {len(cov['patched'])} 个旋钮）")
        # ⚠️ "世界里还偏着的旋钮"由**覆盖率**推出来，不再写死任何旋钮名
        #    （上一版硬编码 `payment.risk_error_rate`，换个故障就是空话）
        for svc, knob in cov["uncovered"]:
            print(f"        世界里仍然偏着的旋钮：{svc}.{knob}="
                  f"{after['knobs'].get(svc, {}).get(knob)}"
                  f"（补丁里有它，但没有可用的变更记录 ⇒ M5 不提案）")
        if v_ok["status"] == "worse" and cov["uncovered"]:
            print("        ⚠️ **只覆盖故障的一部分时，症状可能反而更差** —— 这既不是修好了，"
                  "也不是什么都没做。")
            print("           要**全量**恢复，请走人的那扇门："
                  f".\\.venv\\Scripts\\python.exe scripts\\inject_fault.py revert {args.fault}")
    if _disagree:
        print("        ⚠️ **两把尺子分歧**（要一起读，不许只挑好看的那把）：")
        print(f"           · 用户可感的症状（{v_ok['symptom']}）：{v_ok['status']}"
              f"（{v_ok.get('before')} → {v_ok.get('after')}）")
        print(f"           · 与故障**因果对齐**的量：下游放大倍数 {v_ratio['status']}"
              f"（{v_ratio.get('before', '?')}/100 → {v_ratio.get('after', '?')}/100）、"
              f"错误调用 {v_err['status']}（{v_err.get('before')} → {v_err.get('after')}）")
        print("           ⇒ 判决：**动作对（故障被削弱了），但对用户来说更差了** —— "
              "重试既放大流量、也救回订单，撤掉它两头都会露出来。")
    print(f"\n  证据已写：{EVIDENCE.relative_to(ROOT)}")
    return 0 if symptom_ok else (3 if action_ok else 1)


def _to_int(x: float) -> int:
    return int(round(x))


def _fault_patches(fault_id: str) -> dict:
    """取注入器里**这个故障的完整补丁**（`changes` 只是其中"现实世界会留痕"的那部分）。

    ⚠️ 依赖注入器那份定义是**有意为之**：覆盖率的分子分母必须都来自同一个权威 ——
       "补丁"由 `scripts/inject_fault.py` 定义，"提案"由变更记录决定，
       **硬编码旋钮名就又会漂**（上一版就是写死 `risk_error_rate` 才漏掉通用性）。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from inject_fault import FAULTS                                     # noqa: PLC0415

    fault = FAULTS.get(fault_id)
    return {svc: dict(knobs) for svc, knobs in (fault.patches if fault else {}).items()}


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
