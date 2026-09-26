"""世界"被谁改了"的查证工具（ADR-0009 / harness-log #65）。

============================ 它回答什么问题 ============================

2026-09-27 一次全量门禁里 `test_regression_5` 红在"循环里的 5xx"，
而**是谁把世界弄脏的查不到** —— `/_inject` 当时不留任何痕迹。
现在留痕有了（`GET /_inject_history`），这个脚本就是**用它来查**的那一步：

  ① **基线跑**：连续跑 `tests/test_world.py` N 次，每次跑完统计
     "本次新增了多少次改动、分别来自谁、有没有**匿名**的"；
     ⇒ 匿名数 > 0 就说明**还有路径在偷偷改旋钮**（匿名 = 等于没留痕）。
  ② **阳性对照**：一边跑集成文件，一边用另一个线程每 300ms 改一次旋钮
     （自报 `by="hostile-concurrent-probe"`）。
     ⇒ 这是"#65 那类事故"的**可控复现**：世界在测试期间被别的进程改了。
     集成用例**必须**因此变红，而且历史里**必须**能指认出那个并发进程。
     若它认不出来，那这套留痕就是摆设。

============================ 怎么用 ============================

    .\\.venv\\Scripts\\python.exe scripts\\hunt_world_mutators.py            # 默认：基线 4 次 + 对照
    .\\.venv\\Scripts\\python.exe scripts\\hunt_world_mutators.py --runs 2 --no-control

⚠️ 需要世界在线；跑完请 `scripts/inject_fault.py revert-all`（脚本自己也会在收尾时恢复）。
⚠️ 阳性对照**故意**会让集成用例变红 —— 那是目的，不是缺陷。
⚠️ 退出码：0 = 基线无匿名改动且对照能指认；1 = 有一项不成立（那才是真问题）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                                     # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
PORTS = {"order": 8080, "inventory": 8081, "payment": 8082}

import httpx                                                          # noqa: E402


def history() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for svc, port in PORTS.items():
        try:
            out[svc] = httpx.get(
                f"http://127.0.0.1:{port}/_inject_history", timeout=10.0
            ).json()["records"]
        except Exception as exc:                                      # noqa: BLE001
            out[svc] = [{"__error__": f"{type(exc).__name__}: {exc}"}]
    return out


def world_is_up() -> bool:
    for port in PORTS.values():
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=5.0).raise_for_status()
        except Exception:                                             # noqa: BLE001
            return False
    return True


def run_world_tests() -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_world.py", "-q", "--no-header"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
    )
    lines = [ln for ln in (p.stdout or "").strip().splitlines() if ln.strip()]
    return p.returncode, (lines[-1] if lines else "")


def classify(recs: list[dict], cutoff: float) -> dict:
    """本次跑期间新增的改动：多少条、来自谁、有没有匿名的。"""
    recent = [r for r in recs if float(r.get("ts", 0) or 0) >= cutoff]
    by_kind: dict[str, int] = {}
    anonymous: list[dict] = []
    for r in recent:
        who = str(r.get("by", "")).strip()
        by_kind[who.split(":")[0] if who else "(匿名)"] = \
            by_kind.get(who.split(":")[0] if who else "(匿名)", 0) + 1
        if not who:
            anonymous.append(r)
    return {"n": len(recent), "by_kind": by_kind, "anonymous": anonymous}


def main() -> int:
    ap = argparse.ArgumentParser(description="查证'世界被谁改了'（ADR-0009）")
    ap.add_argument("--runs", type=int, default=4, help="基线连跑几次集成文件")
    ap.add_argument("--no-control", action="store_true", help="跳过阳性对照")
    args = ap.parse_args()

    if not world_is_up():
        print("✗ 世界没起来 —— 先 docker compose up -d")
        return 2

    print("=" * 84)
    print("① 基线：连跑集成文件，统计改动来源（匿名数必须为 0）")
    print("=" * 84)
    print("  起始历史条数：", {k: len(v) for k, v in history().items()})

    bad = 0
    for i in range(1, args.runs + 1):
        t0 = time.time()
        code, tail = run_world_tests()
        info = {svc: classify(recs, t0) for svc, recs in history().items()}
        n = sum(v["n"] for v in info.values())
        anon = sum(len(v["anonymous"]) for v in info.values())
        kinds: dict[str, int] = {}
        for v in info.values():
            for k, c in v["by_kind"].items():
                kinds[k] = kinds.get(k, 0) + c
        print(f"  第 {i} 次：exit={code}　{tail}")
        print(f"          新增改动 {n} 条，**匿名 {anon}** 条，来源 {kinds}")
        if anon:
            bad += 1
            print("          ⚠️ 有匿名改动 ⇒ 还有路径没自报来源：")
            for svc, v in info.items():
                for r in v["anonymous"][:2]:
                    print(f"            {svc}: {r}")

    if args.no_control:
        print("\n（已跳过阳性对照）")
        return 1 if bad else 0

    print()
    print("=" * 84)
    print("② 阳性对照：并发改旋钮（每 300ms 一次），工具必须指认得出")
    print("=" * 84)
    stop = threading.Event()

    def hostile() -> None:
        c = httpx.Client(timeout=5.0)
        while not stop.is_set():
            try:
                # 写一个与默认值不同的值，确保真的产生一条"改动"
                c.post("http://127.0.0.1:8082/_inject",
                       json={"risk_latency_ms": 31, "by": "hostile-concurrent-probe"})
                c.post("http://127.0.0.1:8082/_inject",
                       json={"risk_latency_ms": 30, "by": "hostile-concurrent-probe"})
            except Exception:                                         # noqa: BLE001
                pass
            time.sleep(0.3)

    t0 = time.time()
    th = threading.Thread(target=hostile, daemon=True)
    th.start()
    code, tail = run_world_tests()
    stop.set()
    th.join(timeout=5)

    info = classify(history()["payment"], t0)
    named = info["by_kind"].get("hostile-concurrent-probe", 0)
    print(f"  集成文件 exit={code}　{tail}")
    print(f"  payment 期间改动来源：{info['by_kind']}")
    print(f"  ⇒ 并发进程被指认：{'✅ 是（%d 条）' % named if named else '❌ 否'}")
    print(f"  ⇒ 集成用例被并发改动弄红：{'✅ 是' if code != 0 else '⚠️ 否（这次没撞上）'}")

    # 收尾：把世界恢复默认（并留下一条 by 记录）
    try:
        httpx.post("http://127.0.0.1:8082/_inject",
                   json={"risk_latency_ms": 30, "by": "hunt_world_mutators:cleanup"}, timeout=10.0)
        httpx.post("http://127.0.0.1:8081/_inject",
                   json={"downstream_retries": 1, "by": "hunt_world_mutators:cleanup"}, timeout=10.0)
        httpx.post("http://127.0.0.1:8080/_inject",
                   json={"pool_limit": 64, "pool_acquire_timeout_ms": 2000,
                         "slow_op_ms": 0, "leak_mb_per_req": 0.0,
                         "risk_latency_ms": 30, "risk_error_rate": 0.0,
                         "downstream_retries": 1,
                         "by": "hunt_world_mutators:cleanup"}, timeout=10.0)
    except Exception as exc:                                          # noqa: BLE001
        print(f"  ⚠️ 收尾恢复失败：{exc}")

    ok = (bad == 0) and named > 0
    print()
    print("=" * 84)
    print("结论：" + ("✅ 基线无匿名改动，且对照能指认并发肇事者" if ok
                    else "❌ 有一项不成立 —— 见上面的标记"))
    print("=" * 84)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
