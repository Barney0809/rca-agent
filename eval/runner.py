"""
评测运行器 —— 跑 baseline，产出三个数字。

============================ 三个数字 ============================

    **准确率**  答对根因的比例（按 eval/scenarios.py 的关键词组判定）
    **步数**    LLM 轮次 + 工具调用次数
    **成本**    单次诊断的 token 花费（元）

并重复 N 轮，给出**噪声带** —— 单次抽样出来的数字说明不了任何问题。

============================ 三种运行模式（必须分清）============================

    --live    （默认）真实调用。**测量噪声带必须用这个** ——
              回放会给出完全一致的结果，那就测不出波动了。
    --record  真实调用 + 录下来。用于生成可分享的回放包。
    --replay  只读录制，零成本、完全可复现。用于 CI 与面试官复跑。

⚠️ 常见误用：用回放模式测"三轮的波动"。
   那三轮结果必然完全一样，噪声带是假的。
"""

from __future__ import annotations

import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from rca.agents.baseline import BaselineAgent  # noqa: E402
from rca.llm.provider import DeepSeekClient, LlmConfig  # noqa: E402
from rca.llm.recording import Recorder  # noqa: E402
from rca.tools import RunContext  # noqa: E402

from eval.scenarios import SCENARIOS, ScenarioScore  # noqa: E402
RUNS_DIR = ROOT / "runs"


# ================================================================
# 场景发现
# ================================================================

def discover_runs(fault_ids: list[str] | None = None) -> dict[str, Path]:
    """在 runs/ 下找出每种故障**最新**的一次场景。

    同一个故障可能跑过很多次（调试、变异测试等），取最新的一次。
    """
    found: dict[str, tuple[float, Path]] = {}
    if not RUNS_DIR.exists():
        return {}
    for d in RUNS_DIR.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        scen = d / "scenario.json"
        logs = d / "logs"
        if not scen.exists() or not logs.exists():
            continue
        try:
            data = json.loads(scen.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        fid = data.get("fault_id")
        if not fid:
            continue
        if fault_ids and fid not in fault_ids:
            continue
        mtime = scen.stat().st_mtime
        if fid not in found or mtime > found[fid][0]:
            found[fid] = (mtime, d)

    return {fid: path for fid, (_, path) in sorted(found.items())}


# ================================================================
# 结果结构
# ================================================================

@dataclass
class Attempt:
    fault_id: str
    round_no: int
    correct: bool
    explanation: str
    root_cause: str
    steps: int
    tool_calls: int
    cost_yuan: float
    input_tokens: int
    output_tokens: int
    elapsed_s: float
    finished: bool
    parse_ok: bool
    # 多 Agent 模式下的额外信息（交叉质证的改变次数、驳回项、分歧等）。
    # 默认为空 dict —— 这样既有的 results.json 仍能被 load_report 读回。
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Report:
    model: str
    mode: str
    rounds: int
    started_at: str
    agent: str = "baseline"
    attempts: list[Attempt] = field(default_factory=list)

    # ---- 聚合 ----
    def agg(self) -> dict:
        if not self.attempts:
            return {}
        by_fault: dict[str, list[Attempt]] = {}
        for a in self.attempts:
            by_fault.setdefault(a.fault_id, []).append(a)

        per_fault = {}
        for fid, items in sorted(by_fault.items()):
            per_fault[fid] = {
                "label": SCENARIOS[fid].label,
                "accuracy": sum(1 for i in items if i.correct) / len(items),
                "mean_steps": statistics.mean(i.steps for i in items),
                "mean_tool_calls": statistics.mean(i.tool_calls for i in items),
                "mean_cost_yuan": statistics.mean(i.cost_yuan for i in items),
                "mean_elapsed_s": statistics.mean(i.elapsed_s for i in items),
                "finished_rate": sum(1 for i in items if i.finished) / len(items),
            }

        accs = [i.correct for i in self.attempts]
        costs = [i.cost_yuan for i in self.attempts]
        steps = [i.steps for i in self.attempts]

        # ---- 噪声带 = 【轮与轮之间】的波动，不是"每一次尝试的最大最小值" ----
        #
        # ⚠️ 踩过的坑：第一版直接对每次尝试的真假值求 min/max，
        #    得到的"噪声带"是 0.0%–100.0% —— 那是**单次尝试只能对或错**这个事实，
        #    而不是波动幅度。它把一个毫无信息量的数字伪装成了统计量。
        #
        # 正确做法：先算每一轮的**聚合准确率**，再看这些轮之间的极差。
        by_round: dict[int, list[Attempt]] = {}
        for a in self.attempts:
            by_round.setdefault(a.round_no, []).append(a)
        round_acc = [
            sum(1 for a in items if a.correct) / len(items)
            for items in by_round.values()
        ]
        round_cost = [statistics.mean(a.cost_yuan for a in items) for items in by_round.values()]
        round_steps = [statistics.mean(a.steps for a in items) for items in by_round.values()]

        return {
            "n_attempts": len(self.attempts),
            "n_rounds": len(by_round),
            "accuracy": sum(accs) / len(accs),
            "accuracy_per_round": round_acc,
            "accuracy_band": self._band(round_acc),
            "mean_cost_yuan": statistics.mean(costs) if costs else 0.0,
            "cost_per_round": round_cost,
            "cost_band_yuan": self._band(round_cost),
            "total_cost_yuan": sum(costs),
            "mean_steps": statistics.mean(steps) if steps else 0.0,
            "steps_per_round": round_steps,
            "steps_band": self._band(round_steps),
            "fully_converged_rate": sum(1 for i in self.attempts if i.finished) / len(accs),
            "json_parse_rate": sum(1 for i in self.attempts if i.parse_ok) / len(accs),
            "per_fault": per_fault,
        }

    @staticmethod
    def _band(values: list) -> list:
        """噪声带 = [最小值, 最大值]。

        单轮时两者相同 —— 那是在如实反映"只跑了一轮，没测出波动"，
        而不是"波动为零"。这一点必须在报告里说清楚。
        """
        if not values:
            return [0, 0]
        fv = [float(v) for v in values]
        return [min(fv), max(fv)]

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "model": self.model,
            "mode": self.mode,
            "rounds": self.rounds,
            "started_at": self.started_at,
            "aggregate": self.agg(),
            "attempts": [a.to_dict() for a in self.attempts],
        }


# ================================================================
# 运行
# ================================================================

def run(
    *,
    fault_ids: list[str] | None = None,
    rounds: int = 3,
    model: str | None = None,
    max_steps: int = 14,
    mode: str = "live",
    agent: str = "baseline",
    verbose: bool = True,
) -> Report:
    cfg = LlmConfig.from_env()
    recorder = None
    if mode in ("record", "replay"):
        rec_path = RUNS_DIR / "_recordings" / f"{mode}-{model or cfg.model_cheap}.ndjson"
        recorder = Recorder(rec_path, mode="record" if mode == "record" else "replay")

    client = DeepSeekClient(cfg, recorder=recorder)
    runs = discover_runs(fault_ids)
    report = Report(
        model=model or cfg.model_cheap,
        mode=mode,
        rounds=rounds,
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    report.agent = agent

    if not runs:
        print("没有找到可用场景。先跑：python scripts/inject_fault.py scenario F1")
        return report

    if verbose:
        print("=" * 96)
        print(f"  {agent} 评测   模型={report.model}  模式={mode}  轮数={rounds}  "
              f"场景数={len(runs)}  最大步数={max_steps}")
        print("=" * 96)
        print()

    baseline_agent = BaselineAgent(client, model=model, max_steps=max_steps)

    for fid, run_dir in runs.items():
        score = SCENARIOS.get(fid)
        if score is None:
            if verbose:
                print(f"  {fid}: 没有评分规则，跳过")
            continue

        for rnd in range(1, rounds + 1):
            if verbose:
                print(f"  [{fid} 第{rnd}轮] ", end="", flush=True)
            ctx = RunContext.from_run_dir(run_dir)

            if agent == "multi":
                attempt = _run_multi_slice(client, ctx, fid, rnd, score, model, max_steps)
            else:
                attempt = _run_baseline_slice(baseline_agent, ctx, fid, rnd, score)

            report.attempts.append(attempt)
            if verbose:
                print(
                    f"{'✅' if attempt.correct else '❌'} 步数={attempt.steps} "
                    f"工具={attempt.tool_calls} 成本=¥{attempt.cost_yuan:.4f} "
                    f"{attempt.elapsed_s:.1f}s"
                )
                if not attempt.correct:
                    print(f"        结论：{attempt.root_cause[:150]}")
                    print(f"        {attempt.explanation}")

    return report


def _run_baseline_slice(
    baseline_agent: BaselineAgent,
    ctx: RunContext,
    fid: str,
    rnd: int,
    score: ScenarioScore,
) -> Attempt:
    diag = baseline_agent.diagnose(ctx)
    correct, _ = score.judge(diag.root_cause)
    return Attempt(
        fault_id=fid,
        round_no=rnd,
        correct=correct,
        explanation=score.explain(diag.root_cause),
        root_cause=diag.root_cause[:400],
        steps=diag.steps,
        tool_calls=diag.tool_calls,
        cost_yuan=diag.cost_yuan,
        input_tokens=diag.input_tokens,
        output_tokens=diag.output_tokens,
        elapsed_s=diag.elapsed_s,
        finished=diag.finished,
        parse_ok=diag.parse_ok,
    )


def _run_multi_slice(
    client: DeepSeekClient,
    ctx: RunContext,
    fid: str,
    rnd: int,
    score: ScenarioScore,
    model: str | None,
    max_steps: int,
) -> Attempt:
    """跑一次完整的多 Agent 流程（三轮：调查 → 交叉质证 → 裁决）。

    ⚠️ 步数预算与 baseline **必须一致** —— 否则就是 harness-log #13 那个坑：
       一个配置差异会被当成能力差异。
    """
    from rca.agents.coordinator import diagnose_multi

    res = diagnose_multi(client, ctx, model=model, max_steps=max_steps)
    verdict_text = res.verdict.root_cause
    correct, _ = score.judge(verdict_text)

    return Attempt(
        fault_id=fid,
        round_no=rnd,
        correct=correct,
        explanation=score.explain(verdict_text),
        root_cause=verdict_text[:400],
        # 统一口径：steps = 总 LLM 调用次数（baseline 的 steps 也是 LLM 轮次）
        steps=res.n_llm_calls,
        tool_calls=res.total_tool_calls,
        cost_yuan=res.total_cost_yuan,
        input_tokens=0,          # 多 Agent 的 token 汇总见 detail
        output_tokens=0,
        elapsed_s=res.elapsed_s,
        finished=all(h.finished for h in res.hypotheses)
        and all(c.finished for c in res.cross_exams)
        and res.verdict.parse_ok,
        parse_ok=res.verdict.parse_ok,
        detail={
            "accepted": res.verdict.accepted,
            "n_rejected": len(res.verdict.rejected),
            "rejected": res.verdict.rejected,
            "dissent": res.verdict.dissent,
            "n_changed_after_crossexam": sum(1 for c in res.cross_exams if c.changed),
            "n_falsify_claims": sum(len(c.falsifies) for c in res.cross_exams),
            "denied_tool_calls": res.denied_tool_calls,
            "hypotheses": [h.to_dict() for h in res.hypotheses],
            "cross_exams": [c.to_dict() for c in res.cross_exams],
            "verdict": res.verdict.to_dict(),
        },
    )


def load_report(path: Path) -> Report:
    """从已保存的 results.json 重新构造报告。

    为什么需要它：**聚合逻辑本身可能有 bug**（我们刚修过一次噪声带的算法）。
    修了逻辑之后不应该再花一遍钱重跑 LLM —— 原始尝试记录都在，
    重新聚合即可。这也是"把原始数据留全"的价值。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    report = Report(
        model=data["model"],
        mode=data["mode"],
        rounds=data["rounds"],
        started_at=data["started_at"],
        agent=data.get("agent", "baseline"),
    )
    report.attempts = [Attempt(**a) for a in data["attempts"]]
    return report


def print_report(report: Report) -> None:
    agg = report.agg()
    if not agg:
        return
    print()
    print("=" * 96)
    print("  结果")
    print("=" * 96)
    print(f"  {report.agent} 评测   模型 {report.model}   模式 {report.mode}  "
          f"{agg['n_rounds']} 轮 × {len(agg['per_fault'])} 场景 = {agg['n_attempts']} 次")
    if agg["n_rounds"] < 3:
        print("  ⚠️ 轮数少于 3，噪声带不可信 —— 单轮结果只是在如实反映'没测出波动'")
    print()
    print("  ── 三个数字 ──")
    band = agg["accuracy_band"]
    per_round = " → ".join(f"{v:.0%}" for v in agg["accuracy_per_round"])
    print(f"  ① 准确率   {agg['accuracy']:.1%}")
    print(f"     各轮：{per_round}    轮间噪声带 {band[0]:.0%}–{band[1]:.0%}")
    print(f"  ② 步数     {agg['mean_steps']:.1f} 轮 LLM"
          f"    轮间噪声带 {agg['steps_band'][0]:.1f}–{agg['steps_band'][1]:.1f}")
    print(f"  ③ 成本     ¥{agg['mean_cost_yuan']:.4f} / 次诊断"
          f"    轮间噪声带 ¥{agg['cost_band_yuan'][0]:.4f}–¥{agg['cost_band_yuan'][1]:.4f}")
    print(f"     总计     ¥{agg['total_cost_yuan']:.4f}")
    print()
    conv = agg["fully_converged_rate"]
    print(f"  收敛率（没撞上步数上限）{conv:.0%}"
          f"   JSON 解析成功率 {agg['json_parse_rate']:.0%}")
    if conv < 1.0:
        # ⚠️ 这一条不是客套话，是 harness-log #13 的封堵。
        #    未收敛的尝试会被记成「答错」，但那是**配置问题**（步数预算不足），
        #    不是模型的归因能力问题。混在一起会让后续所有比较失去意义。
        print()
        print("  ⚠️ 收敛率不足 100% ⇒ **准确率不可用于比较**。")
        print("     未收敛的尝试会被记成「答错」，但那是配置问题（步数预算不足），")
        print("     不是模型的归因能力问题。请提高 --max-steps 后重测，再做比较。")
    print()
    print("  ── 逐个场景 ──")
    print(f"  {'ID':<4} {'场景':<30} {'准确率':<8} {'步数':<6} {'工具':<6} {'成本':<10}")
    print("  " + "-" * 88)
    for fid, row in agg["per_fault"].items():
        print(f"  {fid:<4} {row['label']:<30} {row['accuracy']:<8.0%} "
              f"{row['mean_steps']:<6.1f} {row['mean_tool_calls']:<6.1f} "
              f"¥{row['mean_cost_yuan']:.4f}")


def save_report(report: Report) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = RUNS_DIR / "_eval" / f"{report.agent}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "results.json"
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


# ================================================================
# CLI
# ================================================================

def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Agent 评测（baseline / 多 Agent）")
    p.add_argument("--faults", default=None, help="逗号分隔，如 F1,F2；默认全部")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--model", default=None, help="默认取配置里的便宜模型")
    p.add_argument("--max-steps", type=int, default=14,
                   help="LLM 轮次上限；实测最慢场景需 9 步，故默认 14。"
                        "⚠️ 比较 baseline 与 multi 时必须用同一个值")
    p.add_argument("--mode", choices=["live", "record", "replay"], default="live")
    p.add_argument("--agent", choices=["baseline", "multi"], default="baseline",
                   help="baseline = 单 Agent；multi = 三个专职 Agent + 交叉质证 + 裁决")
    p.add_argument("--list-scenarios", action="store_true", help="只看有哪些场景可用")
    p.add_argument("--from-json", default=None,
                   help="从已保存的 results.json 重新出报告（不调用 LLM、不花钱）")
    args = p.parse_args(argv)

    if args.from_json:
        path = Path(args.from_json)
        if not path.is_absolute():
            path = ROOT / path
        report = load_report(path)
        print(f"从 {path.name} 重新聚合（未调用 LLM）")
        print_report(report)
        return 0

    if args.list_scenarios:
        runs = discover_runs()
        print("可用场景（runs/ 下每种故障的最新一次）：")
        for fid, d in runs.items():
            print(f"  {fid:<4} {d.name}  {SCENARIOS[fid].label if fid in SCENARIOS else '(无评分规则)'}")
        return 0

    fault_ids = args.faults.split(",") if args.faults else None
    report = run(
        fault_ids=fault_ids,
        rounds=args.rounds,
        model=args.model,
        max_steps=args.max_steps,
        mode=args.mode,
        agent=args.agent,
    )
    print_report(report)
    if report.attempts:
        path = save_report(report)
        print(f"\n  结果已存：{path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
