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

import hashlib
import json
import os
import re
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
# 数据根目录：本机 runs/ vs 便携重放包
# ================================================================
#
# 为什么需要这一层（2026-09-25，D19）：
#
# 录制里的 key 是 (tag, model, messages) 的哈希，而 **tag 里嵌着场景目录名**
# （例如 specialist/metrics/r-20260925-072345）。
# 场景目录名是一次注入产生的时间戳 ⇒ **换一次场景目录，key 必然不同**。
#
# 实测过：拿旧的录制去重放新生成的场景，第一步就 miss
# （runs/_replay_probe.log 的报错原文）。
#
# ⇒ “零成本重放”成立的前提不是“有录制文件”，而是
#   **录制 + 同一批场景目录（同名同内容）**。
#   后者有 24.9 MB 原始日志，所以做成压缩包：RCA_REPLAY_ROOT 指过去。


def data_root() -> Path:
    """场景数据与录制的根目录。

    默认就是 `runs/`；设置 `RCA_REPLAY_ROOT` 时指向便携重放包。

    ⚠️ 只影响**读**（场景发现 + 录制文件）。评测结果仍然写回
       `runs/_eval/`，这样重放包始终是只读的、不会被跑脏。
    """
    raw = os.environ.get("RCA_REPLAY_ROOT", "").strip()
    return Path(raw) if raw else RUNS_DIR


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_replay_pack(root: Path) -> dict:
    """重放包自检：清单在、清单里的文件都在、哈希都对得上。

    为什么要自检：重放的价值全押在“同一批数据”上。如果包里少一个场景
    或某个日志被改过，key 就会 miss —— 而 miss 在 replay 模式下会抛错，
    看起来像“模型/代码出了问题”，实际是**包坏了**。
    所以先验包，把“包坏了”和“真的对不上”分开。
    """
    manifest_path = root / "pack.json"
    if not manifest_path.exists():
        raise RuntimeError(
            f"重放包缺少清单：{manifest_path}\n"
            f"  用 scripts/make_replay_pack.py 生成，或去掉 RCA_REPLAY_ROOT。"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad: list[str] = []
    for rel, want in sorted(manifest.get("files", {}).items()):
        p = root / rel
        if not p.exists():
            bad.append(f"缺失 {rel}")
            continue
        got = sha256_file(p)
        if got != want:
            bad.append(f"内容不一致 {rel}（清单 {want[:12]}… 实际 {got[:12]}…）")
    if bad:
        raise RuntimeError(
            "重放包已损坏，拒绝在它上面跑（否则会把『包坏了』误报成『对不上』）：\n  "
            + "\n  ".join(bad)
        )
    return manifest


def check_replay_root_mode(mode: str) -> None:
    """重放包是**只读**的：只有 replay 模式允许在它上面跑。

    为什么必须硬拒绝（2026-09-25 自审时发现，D19b）：

    `run()` 里录制路径是 `data_root() / "_recordings" / f"{mode}-..."`，
    而 `data_root()` 在设了 `RCA_REPLAY_ROOT` 时就是那个包 ——
    于是 `--mode record` **会往只读产物里追加一个 `record-*.ndjson`**。

    更麻烦的是它**不会报警**：自检只校验清单里列出的文件，
    新增的文件它既看不见、也不会让任何断言变红。

    ⇒ 把"在这个包上能做什么"变成一句可执行的判断，而不是一条注释。
    """
    if mode != "replay":
        raise RuntimeError(
            f"RCA_REPLAY_ROOT 指向的是只读重放包，只允许 --mode replay（当前 mode={mode!r}）。\n"
            f"  要录新的批次，请去掉这个环境变量、用本机 runs/ 录。"
        )


# ================================================================
# 场景发现
# ================================================================

def discover_runs(fault_ids: list[str] | None = None) -> dict[str, Path]:
    """在数据根目录下找出每种故障**最新**的一次场景。

    同一个故障可能跑过很多次（调试、变异测试等），取最新的一次。
    """
    found: dict[str, tuple[float, Path]] = {}
    root = data_root()
    if not root.exists():
        return {}
    for d in root.iterdir():
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


def _fatal_api_error(exc: BaseException) -> str:
    """这是"再跑下去也没用"的错误吗？返回一句人话，否则返回空串。

    为什么要有这个（2026-09-25，M2 实测时被真实撞上）：

        臂 B 跑到**第一次尝试**时抛了
        `openai.APIStatusError: 402 - Insufficient Balance`
        —— 账户余额不足。结果是整个 21 次尝试的运行**直接崩掉**，
        已经跑完的部分（这里是 0 次，但换一次运行就可能是 20 次）**全部丢掉**。

    ⇒ 分两类处理：
      · **致命**（401 未授权 / 402 余额不足 / 403 无权限）：再跑只会继续失败，
        立刻停，但**把已完成的尝试存下来**，并说清是哪一种；
      · **其它**：跳过这一次，继续跑（一次网络抖动不该毁掉整轮）。
    """
    text = f"{type(exc).__name__}: {exc}"
    for code, why in (
        ("402", "账户余额不足（请充值后重跑；已完成的尝试会照常存档）"),
        ("401", "API Key 无效或未授权（检查 DEEPSEEK_API_KEY）"),
        ("403", "账户无权调用该模型（检查账号权限）"),
    ):
        if code in text or f"status_code: {code}" in text:
            return why
    return ""


def _report_attempt_failure(fid: str, rnd: int, exc: BaseException, *, fatal: str) -> None:
    where = f"{fid} 第{rnd}轮"
    if fatal:
        print(f"\n  ⛔ {where} 中断：{fatal}")
        print(f"     原始错误：{type(exc).__name__}: {str(exc)[:200]}")
    else:
        print(f"\n  ⚠️ {where} 失败（跳过这一次）：{type(exc).__name__}: {str(exc)[:200]}")


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
    # ★ 打转统计（D9）：参数完全相同的重复工具调用次数。
    #   默认为 0 —— 这样既有的 results.json 仍能被 load_report 读回。
    repeat_calls: int = 0
    # ★ 完整轨迹（D11）：每一步调了什么工具、参数是什么、返回了什么。
    #   ⚠️ **不写进 results.json**（那是给人和脚本读的汇总，会被几 MB 的工具返回撑爆），
    #      而是由 save_report 单独落到 traces/ 下，这里只记路径。
    trace: list = field(default_factory=list)
    trace_path: str = ""    # 多 Agent 模式下的额外信息（交叉质证的改变次数、驳回项、分歧等）。
    # 默认为空 dict —— 这样既有的 results.json 仍能被 load_report 读回。
    detail: dict = field(default_factory=dict)
    # ★ 护栏（M2 软提醒）自己的账：审了几条、报了没有、改了几次、花了多少。
    #   ⚠️ 与 `correct` **完全无关**（ADR-0007 决定 2）—— 摆在一起只是为了做 2×2 对照。
    #   默认为空 dict：既有的 results.json（没有护栏）仍能被读回。
    guard: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        # ⚠️ trace 要**排除**：它是几 MB 的原始工具返回，
        #    塞进 results.json 会让那个文件没法读（也没法 diff）。
        #    它单独落在 traces/ 下，这里只留路径。
        d = {k: v for k, v in self.__dict__.items() if k != "trace"}
        return d


@dataclass
class Report:
    model: str
    mode: str
    rounds: int
    started_at: str
    agent: str = "baseline"
    # ★ 计价时段（D11/#30）：峰时单价是谷时的 **2 倍**（0.04/2.0/8.0 vs 0.02/1.0/4.0）。
    #   不记下来的话，跨时段比较成本会得出**假的一倍增长** —— 我自己就踩了（#30）。
    pricing_tier: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    # ★ 运行被**中途放弃**的原因（默认空 = 正常跑完）。
    #   为什么要有：一次真实的 402（余额不足）曾让整轮运行抛异常，
    #   已完成的尝试跟着一起丢 —— 那些尝试是**花了钱**的。
    aborted_reason: str = ""

    # ---- 聚合 ----
    def agg(self) -> dict:
        if not self.attempts:
            return {}

        # ⚠️ 作废的场景**不得参与聚合**（harness-log #20）。
        #
        # 为什么必须在聚合层挡：一处作废的题目如果留在数字里，
        # 它会把**每一次**比较都污染一遍，而且看不出来 ——
        # 你只会觉得"准确率怎么这么低"。
        # 这是本项目"数字测的不是你以为的东西"家族的又一例。
        invalidated = {
            fid: SCENARIOS[fid].invalidated_reason
            for fid in {a.fault_id for a in self.attempts}
            if fid in SCENARIOS and SCENARIOS[fid].invalidated_reason
        }
        scored = [a for a in self.attempts if a.fault_id not in invalidated]

        by_fault: dict[str, list[Attempt]] = {}
        for a in scored:
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

        excluded = [a for a in self.attempts if a.fault_id in invalidated]

        accs = [i.correct for i in scored]
        costs = [i.cost_yuan for i in scored]
        steps = [i.steps for i in scored]

        # ---- 噪声带 = 【轮与轮之间】的波动，不是"每一次尝试的最大最小值" ----
        #
        # ⚠️ 踩过的坑：第一版直接对每次尝试的真假值求 min/max，
        #    得到的"噪声带"是 0.0%–100.0% —— 那是**单次尝试只能对或错**这个事实，
        #    而不是波动幅度。它把一个毫无信息量的数字伪装成了统计量。
        #
        # 正确做法：先算每一轮的**聚合准确率**，再看这些轮之间的极差。
        by_round: dict[int, list[Attempt]] = {}
        for a in scored:
            by_round.setdefault(a.round_no, []).append(a)
        round_acc = [
            sum(1 for a in items if a.correct) / len(items)
            for items in by_round.values()
        ]
        round_cost = [statistics.mean(a.cost_yuan for a in items) for items in by_round.values()]
        round_steps = [statistics.mean(a.steps for a in items) for items in by_round.values()]

        return {
            "n_attempts": len(scored),
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
            "fully_converged_rate": sum(1 for i in scored if i.finished) / len(accs),
            "json_parse_rate": sum(1 for i in scored if i.parse_ok) / len(accs),
            "per_fault": per_fault,
            # ---- 被排除的作废场景（必须在报告里显式说出来）----
            # ---- 两把尺子（D10）----
            **_judge_agreement(scored),
            "invalidated": invalidated,
            "n_excluded_attempts": len(excluded),
            "n_attempts_total": len(self.attempts),
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
            "pricing_tier": self.pricing_tier,
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
    cross_exam_steps: int = 14,
    mode: str = "live",
    agent: str = "baseline",
    include_invalidated: bool = False,
    judge_fn=None,
    guard: bool = False,
    verbose: bool = True,
) -> Report:
    cfg = LlmConfig.from_env()
    recorder = None
    if os.environ.get("RCA_REPLAY_ROOT", "").strip():
        # 只读包：先判断"在这个包上允许做什么"，再验包。
        check_replay_root_mode(mode)
        # 把「包坏了」和「真的没命中」分开。
        verify_replay_pack(data_root())
    if mode in ("record", "replay"):
        rec_path = data_root() / "_recordings" / f"{mode}-{model or cfg.model_cheap}.ndjson"
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
    from rca.llm.provider import is_peak_hour
    report.pricing_tier = "peak" if is_peak_hour() else "off_peak"

    if not runs:
        print("没有找到可用场景。先跑：python scripts/inject_fault.py scenario F1")
        return report

    if verbose:
        print("=" * 96)
        print(f"  {agent} 评测   模型={report.model}  模式={mode}  轮数={rounds}  "
              f"场景数={len(runs)}  最大步数={max_steps}  "
              f"护栏={'开（软提醒+一次修正）' if guard else '关'}")
        print("=" * 96)
        print()

    baseline_agent = BaselineAgent(client, model=model, max_steps=max_steps)

    for fid, run_dir in runs.items():
        score = SCENARIOS.get(fid)
        if score is None:
            if verbose:
                print(f"  {fid}: 没有评分规则，跳过")
            continue

        # ⚠️ 作废的场景默认**不跑**（harness-log #20）。
        #    理由有两条：
        #      1. 跑了也没法用 —— 它的判分会把正确答案判错；
        #      2. 跑它要花钱（每轮 ¥0.06~0.08），而钱应该花在有效的题上。
        #    需要复现"我们出错过一道题"时才用 --include-invalidated。
        if score.invalidated_reason and not include_invalidated:
            if verbose:
                print(f"  {fid}: ⛔ 场景已作废，跳过")
                print(f"        理由：{score.invalidated_reason}")
                print("        要强行跑它：加 --include-invalidated（结果不参与聚合）")
            continue

        for rnd in range(1, rounds + 1):
            if verbose:
                print(f"  [{fid} 第{rnd}轮] ", end="", flush=True)
            # ⚠️ 单次尝试失败**不能**毁掉整轮：完成过的尝试是花了钱的。
            #    见 `_fatal_api_error` 的说明（M2 实测时真撞上 402）。
            try:
                ctx = RunContext.from_run_dir(run_dir)

                if agent == "multi":
                    attempt = _run_multi_slice(client, ctx, fid, rnd, score, model,
                                               max_steps, cross_exam_steps, judge_fn, guard=guard)
                else:
                    attempt = _run_baseline_slice(baseline_agent, ctx, fid, rnd, score, judge_fn)
            except Exception as exc:                      # noqa: BLE001 —— 见上
                fatal = _fatal_api_error(exc)
                _report_attempt_failure(fid, rnd, exc, fatal=fatal)
                if fatal:
                    report.aborted_reason = f"{fid} 第{rnd}轮：{fatal}"
                    return report          # 已完成的尝试留在 report 里，由调用方存档
                continue

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
    judge_fn=None,
) -> Attempt:
    diag = baseline_agent.diagnose(ctx)
    jd = judge_detail(score, diag.root_cause, judge_fn)
    correct, _ = score.judge(diag.root_cause)
    return Attempt(
        fault_id=fid,
        round_no=rnd,
        correct=correct,
        explanation=score.explain(diag.root_cause),
        # ⚠️ 存**全文**，不许截断。
        #
        # 这里原本是 `diag.root_cause[:400]`，而 `correct` 是按**全文**算的。
        # 后果：结论超过 400 字符时，存档里的文本重新判一遍会得出**不同的结论** ——
        # 18 条里正好有 2 条是这样（F4 第2轮、F5 第2轮，都是 400 字符整）。
        #
        # 也就是说：**存档的证据无法复核它自己记录的判定**。
        # 这直接违反需求 FR-C（可复现）：面试官拿到 results.json，
        # 应该能自己重算每一个数字，而不是只能相信里面写的布尔值。
        #
        # 发现方式：`eval/rescore.py` 用新规则重算历史结果时，
        # 逐条比对"存档里的 correct"与"用存档文本重算的判定"，对不上才暴露出来。
        root_cause=diag.root_cause,
        steps=diag.steps,
        tool_calls=diag.tool_calls,
        input_tokens=diag.input_tokens,
        output_tokens=diag.output_tokens,
        elapsed_s=diag.elapsed_s,
        finished=diag.finished,
        parse_ok=diag.parse_ok,
        # ★ 打转统计（D9）：`ctx` 就是这次 diagnose 用的上下文，
        #   工具调用全走 ToolBox，所以计数都在它身上。
        repeat_calls=getattr(ctx, "repeat_calls", 0),
        detail=jd,
        trace=list(getattr(diag, "trace", []) or []),
        # ⚠️ 裁判的成本**必须回填**，否则"总计"低估真实花费
        cost_yuan=diag.cost_yuan + jd.get("judge_cost_yuan", 0.0),
    )


def _run_multi_slice(
    client: DeepSeekClient,
    ctx: RunContext,
    fid: str,
    rnd: int,
    score: ScenarioScore,
    model: str | None,
    max_steps: int,
    cross_exam_steps: int,
    judge_fn=None,
    guard: bool = False,
) -> Attempt:
    """跑一次完整的多 Agent 流程（三轮：调查 → 交叉质证 → 裁决）。

    ⚠️ 步数预算与 baseline **必须一致** —— 否则就是 harness-log #13 那个坑：
       一个配置差异会被当成能力差异。

    `guard=True` 时，裁决之后再跑一次**证据审查者**，并给它**一次**自我修正机会（M2）。
    """
    from rca.agents.coordinator import diagnose_multi

    res = diagnose_multi(client, ctx, model=model, max_steps=max_steps,
                         cross_exam_steps=cross_exam_steps, guard=guard)
    verdict_text = res.verdict.root_cause
    jd = judge_detail(score, verdict_text, judge_fn)

    # ★ 各环节的完整轨迹（#29 补完）：三个调查 + 三个质证，各带自己的工具调用
    #   ⚠️ 之前**只有 baseline 这一路**有轨迹存档，多 Agent 的六个环节全丢了 ——
    #      而多 Agent 恰恰是最需要事后追溯的那个（#16/#21 都是在读原文时发现的）。
    combined_trace: list[dict] = [
        {"phase": "investigate", "role": h.role, "steps": list(h.trace)}
        for h in res.hypotheses
    ] + [
        {"phase": "cross_exam", "role": c.role, "steps": list(c.trace)}
        for c in res.cross_exams
    ]
    correct, _ = score.judge(verdict_text)

    return Attempt(
        fault_id=fid,
        round_no=rnd,
        correct=correct,
        explanation=score.explain(verdict_text),
        # 存全文，理由同 `_run_baseline_slice`（截断会让判定无法复核）
        root_cause=verdict_text,
        # 统一口径：steps = 总 LLM 调用次数（baseline 的 steps 也是 LLM 轮次）
        steps=res.n_llm_calls,
        tool_calls=res.total_tool_calls,
        cost_yuan=res.total_cost_yuan + jd.get("judge_cost_yuan", 0.0),
        input_tokens=0,          # 多 Agent 的 token 汇总见 detail
        output_tokens=0,
        elapsed_s=res.elapsed_s,
        finished=all(h.finished for h in res.hypotheses)
        and all(c.finished for c in res.cross_exams)
        and res.verdict.parse_ok,
        parse_ok=res.verdict.parse_ok,
        repeat_calls=res.repeat_calls,          # ★ 打转统计（D9）
        guard=(res.to_dict().get("guard") or {}),   # ★ 护栏自己的账（M2）
        trace=combined_trace,                   # ★ 各环节轨迹（#29）
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
            **jd,
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
        # ⚠️ 必须恢复计价时段（#34）：不恢复的话，`--from-json` 重新聚合时
        #    那条"只与同时段比较成本"的警告会**消失** —— 而重载正是我做比较时走的路径。
        #    这是 #18 的同族：**字段存了，但没接上**。
        pricing_tier=data.get("pricing_tier", ""),
    )
    report.attempts = [Attempt(**a) for a in data["attempts"]]
    return report


def unsupported_cause_claims(attempt: Attempt) -> list[str]:
    """答案**自己列出**的原因里，**对不上场景真值**的那些（结论精度诊断）。

    ⚠️ 先说清楚它**不是**什么（这是两次实测换来的结论，见 harness-log #44）：

        我本来想把它做成"#44 检测器" —— 也就是自动抓出
        「把一条**没有基线对比的**指标（如 `stock_level ≈ 99.8 万`，
        其实是库存夹具的正常余量）当成异常/根因」这种毛病。**试了两版，都不合格**：

        · 第一版："分句不匹配真值 ⇒ 记一条"。实测 **multi 11 条 vs baseline 9 条** ——
          它把"支撑性观察"（`其下游仅 45ms，说明慢在 inventory 内部`）、
          传播描述（`上游只是透传症状`）全算上了。**两侧都中招的指标不区分好坏。**
        · 第二版：再加"必须含指标形状 + 异常措辞"。实测 4 vs 0，**看着能区分**，
          但它是靠**偶然命中**（真实文本里有"水位"二字）才工作的 ——
          我的合成对照用例当场把它戳穿：`{SKU-001}=998092` 这种真实的指标写法
          根本匹配不上我写的正则。**一个靠偶然命中工作的检测器不能进报告。**

        ⇒ 结论：**那个现象需要语义判断，纯关键词做不到。**
          按本项目处理 #32 的先例，**不采用一个会误导的指标**；
          #44 因此仍标 🟡（未封堵），"回去读原文"这一步保留。

    它真正回答的是一个更朴素、可以**逐条复核**的问题：

        **结论里列出来的原因，有几条对不上场景声明的真值？**

    （实测：multi 11 条 / baseline 9 条 —— 两侧差不多，所以它**不区分好坏**，
      只用来提醒"结论里夹带了别的东西"，别拿它当"谁更强"的证据。）

    实现：结论文本就是模型自己的原因列表拼起来的（`；` 分隔，见 baseline/coordinator），
    所以按 `；`/换行拆开就等于还原模型**自己列出的**条目 —— 这不是我发明的切分。
    """
    from eval.scenarios import SCENARIOS, Cause

    sc = SCENARIOS.get(attempt.fault_id)
    text = (attempt.root_cause or "").strip()
    if sc is None or not text:
        return []

    declared = list(sc.required_causes) or [
        Cause(name=sc.label, keyword_groups=sc.keyword_groups)
    ]
    pieces = [p.strip() for p in re.split(r"[；;\n]", text) if p.strip()]
    return [p for p in pieces if not any(c.asserted(p) for c in declared)]


def _print_unsupported_claims(report: Report) -> None:
    """**假阳性轴**（#44）：把"没有依据的额外原因主张"打出来（有才打）。

    ⚠️ 与两轴一样，它**不参与** `correct`，也不加总 —— 它是一个**诊断量**：
       回答"结论里有没有夹带没依据的东西"，而不是"答对没有"。

    ⚠️ 已知的**假阳性风险**（如实写在输出里）：某条主张如果是事故叙事的一部分
       （例如"下游依赖不可用"），它也可能被这条轴打中。所以它是**给人看的提示**，
       不是分数；看到就回去读原文。
    """
    hits = []
    for a in report.attempts:
        try:
            claims = unsupported_cause_claims(a)
        except Exception:  # noqa: BLE001 —— 诊断量坏了不该让整份报告挂掉
            continue
        if claims:
            hits.append((a, claims))
    if not hits:
        return
    total = sum(len(c) for _a, c in hits)
    print()
    print("  ── 假阳性轴（额外的、无据的原因主张）──")
    print(f"     合计 {total} 条，出现在 {len(hits)} 次尝试里")
    for a, claims in hits[:3]:
        print(f"     · {a.fault_id} 第{a.round_no}轮（correct={a.correct}）：{len(claims)} 条")
        for c in claims[:1]:
            print(f"         {c[:110]}")
    print("     ⚠️ 这是**诊断量，不是分数**：某条如果本就是事故叙事的一部分，也可能被打中 ——")
    print("        看到就回去读原文，别拿它当判据。")


def attempt_cause_axes(attempt: Attempt) -> dict[str, dict[str, bool]]:
    """两轴判定 —— 只对声明了 `required_causes` 的多故障场景有意义。

    **为什么需要两轴**（F8 上真实发生的事）：

        multi 的 metrics 专员**找到了**内存泄漏，
        但交叉质证把它说服改口、裁决把它降级成"伴随现象"。

    这是第三种结局：**"找到了，但没当成根因"**。
    单一的关键词组判定表达不了它 —— 要么看到词就算对，要么被否掉就算错，两种都不对。

        **结论轴**：**最终答案**有没有主张这个原因（= 当前任务要求的答案）
        **召回轴**：这次尝试里**任何环节**有没有找到它（= 分工覆盖到了没有）

    ⚠️ 两轴**不能互相替代**，也不能加总：
        · 结论轴是**同口径**比较（两边都只看最终答案），可以拿来比高低；
        · 召回轴**不是同口径**：多 Agent 一轮有 3 个专员 + 3 个质证 + 1 次裁决，
          而 baseline 只有一个答案。它更适合用来回答"分工有没有起作用"，
          而不是"谁更强"。
    """
    from eval.scenarios import SCENARIOS, asserted_causes

    sc = SCENARIOS.get(attempt.fault_id)
    if sc is None or not sc.required_causes:
        return {}

    verdict = asserted_causes(attempt.root_cause, sc.required_causes)

    detail = attempt.detail or {}
    texts = [h.get("claim", "") for h in (detail.get("hypotheses") or [])]
    texts += [x.get("revised_claim", "") for x in (detail.get("cross_exams") or [])]
    if not texts:
        # baseline（或任何单 Agent）：它只有一个"环节"，就是它的最终答案
        texts = [attempt.root_cause]

    recall = {
        c.name: any(c.asserted(t) for t in texts) for c in sc.required_causes
    }
    return {"verdict": verdict, "recall": recall}


def _print_cause_axes(report: Report) -> None:
    """打印两轴判定与各自的命中率。没有多故障场景时什么也不打。"""
    rows = [
        (a, attempt_cause_axes(a))
        for a in report.attempts
    ]
    rows = [(a, ax) for a, ax in rows if ax]
    if not rows:
        return

    names = list(rows[0][1]["verdict"].keys())

    print()
    print("  ── 两轴判定（多故障场景）──")
    print("     结论轴 = 最终答案有没有主张它（同口径，可比较）")
    print("     召回轴 = 这次尝试里任何环节有没有找到它（分工覆盖，不同口径）")
    print()
    for a, ax in rows:
        v = " ".join(f"{n}{'✅' if ax['verdict'][n] else '❌'}" for n in names)
        r = " ".join(f"{n}{'✅' if ax['recall'][n] else '❌'}" for n in names)
        print(f"     {a.fault_id} 第{a.round_no}轮   结论：{v}    召回：{r}")

    print()
    for axis in ("verdict", "recall"):
        label = "结论轴" if axis == "verdict" else "召回轴"
        stats = "  ".join(
            f"{n}={sum(1 for _, ax in rows if ax[axis][n])}/{len(rows)}" for n in names
        )
        print(f"     {label}命中率：{stats}")
    print()
    print("     ⚠️ 召回轴不是同口径比较：多 Agent 一轮有 7 个环节，baseline 只有 1 个答案。")
    print("        它回答的是「分工有没有覆盖到」，不是「谁更强」。")


# ================================================================
# LLM 裁判（D10）：独立于关键词判定的**第二把尺子**
# ================================================================
#
# 设计由数据决定（harness-log #28）：
#   · 关键词判定 —— 免费、确定、已知措辞上全对   → 保留为主判据
#   · LLM 裁判   —— 抗未见措辞（对抗样本 0/3 → 3/3）→ 独立交叉校验
#   · **两者分歧时报出来** —— #16/#21 正是这样被发现的
#
# ⚠️ 裁判**不参与** `correct` 的计算，只做交叉校验。
#    理由：它的成本与抖动都还没在规模上量清楚（目前 8/8 自一致、样本很小），
#    直接拿它当主判据会把一个没量清的误差源引到所有数字里。


def judge_detail(score, text: str, judge_fn) -> dict:
    """对这次结论跑一遍 LLM 裁判，把结论记进 detail。

    `judge_fn(文本, 原因名) -> verdict`
    或 `-> (verdict, cost_yuan)` —— 两种都认（后者用来把裁判成本回填，见下）。
    传 None 表示这次不跑裁判（默认）。

    ⚠️ 返回里带 `judge_cost_yuan`，调用方必须把它**加进 Attempt.cost_yuan**。
       不加的话报告的"总计"会**低估**真实花费 ——
       而"跑一次评测到底花了多少"正是本项目要如实给出的三个数字之一。
    """
    from eval.scenarios import CAUSE_LABELS, Cause

    if judge_fn is None or score is None or not text.strip():
        return {}

    causes = score.required_causes or (
        Cause(
            name=CAUSE_LABELS.get(score.fault_id, score.fault_id),
            keyword_groups=score.keyword_groups,
        ),
    )

    verdicts: dict[str, str] = {}
    total_cost = 0.0
    for c in causes:
        try:
            out = judge_fn(text, c.name)
            if isinstance(out, tuple):
                verdict, cost = out[0], float(out[1])
                total_cost += cost
            else:
                verdict = out
            verdicts[c.name] = str(verdict)
        except Exception as exc:  # noqa: BLE001
            # 裁判自己出错 → 如实记录，**不许当成一致**
            verdicts[c.name] = f"error:{type(exc).__name__}"

    out_detail: dict = {"judge": verdicts}
    if total_cost:
        out_detail["judge_cost_yuan"] = round(total_cost, 6)
    return out_detail


def judge_asserts_all(attempt: Attempt):
    """裁判是否认为"所有必需原因都被主张了"。

    返回 True / False / **None**：
    None 表示"这次没有可用的裁判结论"（没跑、或裁判自己失败/输出不合法）。
    ⚠️ **None 绝不能被当成一致** —— 那会把"裁判失败"伪装成"两把尺子一致"，
       正是本项目反复在防的那种静默失真。
    """
    info = (attempt.detail or {}).get("judge") or {}
    if not info:
        return None
    for v in info.values():
        v = str(v)
        if v == "unknown" or v.startswith("error:"):
            return None
    return all(str(v) == "asserted" for v in info.values())


def stop_reason(attempt: Attempt) -> str:
    """这次尝试**为什么停下来**。

    ⚠️ 为什么值得单独立一个概念：`finished=False` 是个**有歧义的**布尔值。
    它可能意味着两件完全不同的事：

        在深挖，只是预算不够       → 该加预算
        在打转，反复查同一个东西   → 加预算只会让它更贵地打转

    两者的修法相反。实测证据：新任务下 baseline 有一轮 **14 步里调了 87 次工具、
    最后没产出结论** —— 光看"步数 14、结论为空"根本分不出是哪一种。

    判据用**重复调用占比**：重复调用多，说明它不是在扩证据面，而是在原地转。
    """
    if attempt.finished:
        return "converged"
    if attempt.tool_calls >= 10 and attempt.repeat_calls / max(attempt.tool_calls, 1) >= 0.5:
        return "step_limit_looping"
    return "step_limit"


def _print_stop_reasons(report: Report) -> None:
    """打印停止原因分布与打转统计。全部正常收敛时也打一行（"没问题"本身是信息）。"""
    reasons = [stop_reason(a) for a in report.attempts]
    loop = [r for r in reasons if r == "step_limit_looping"]
    limited = [r for r in reasons if r == "step_limit"]

    total_calls = sum(a.tool_calls for a in report.attempts)
    total_repeat = sum(a.repeat_calls for a in report.attempts)

    print()
    print("  ── 停止原因与打转（D9）──")
    print(f"     收敛 {reasons.count('converged')}/{len(reasons)}"
          f"　撞预算 {len(limited)}　**疑似打转** {len(loop)}")
    if total_calls:
        pct = total_repeat / total_calls
        print(f"     重复工具调用 {total_repeat}/{total_calls}（{pct:.0%}）"
              " —— 参数**完全相同**的调用次数")
    if loop:
        print("     ⚠️ 有尝试疑似在打转：加预算解决不了，得改提示词或工具返回的内容")
        for a in report.attempts:
            if stop_reason(a) == "step_limit_looping":
                print(f"        {a.fault_id} 第{a.round_no}轮："
                      f"{a.steps} 步 / {a.tool_calls} 次调用，其中重复 {a.repeat_calls} 次")


def _convergence_breakdown(report: Report) -> list[str]:
    """列出多 Agent 每个环节的 finished / steps —— 用来定位"到底是哪个环节没收敛"。

    为什么值得单独做：多 Agent 的收敛率是**四个环节的合取**
    （3 个调查 + 3 个质证 + 1 次裁决），只说"收敛率 0%"等于什么也没说。
    harness-log #18 就是存档里漏了 `finished` 字段，
    导致报告说"没收敛"却查不出是谁 —— 存档的价值就在于事后能定位。
    """
    lines: list[str] = []
    for a in report.attempts:
        det = a.detail or {}
        bad: list[str] = []
        for h in det.get("hypotheses", []) or []:
            if not h.get("finished"):
                bad.append(f"调查/{h.get('role')}(steps={h.get('steps')})")
        for c in det.get("cross_exams", []) or []:
            if not c.get("finished"):
                bad.append(f"质证/{c.get('role')}(steps={c.get('steps')})")
        v = det.get("verdict") or {}
        if v and not v.get("parse_ok"):
            bad.append("裁决(未产出结构化结论)")
        if bad:
            lines.append(f"       {a.fault_id} 第{a.round_no}轮：{'、'.join(bad)}")
    if not lines:
        lines.append(
            "       （detail 里没有各环节的 finished 记录 —— 可能是旧存档；"
            "见 harness-log #18）"
        )
    return lines



def _make_judge_fn():
    """构造真的裁判函数（只在 --judge 时才建，避免默认路径多花钱）。"""
    from eval.judge import judge_cause
    from rca.llm.provider import DeepSeekClient, LlmConfig

    client = DeepSeekClient(LlmConfig.from_env())

    def _fn(text: str, cause: str):
        res = judge_cause(client, text, cause)
        return res.verdict, res.cost_yuan

    return _fn


def _judge_agreement(attempts: list[Attempt]) -> dict:
    """把"两把尺子"的一致情况汇总出来。没跑裁判时返回空 dict。"""
    judged = [a for a in attempts if judge_asserts_all(a) is not None]
    if not judged:
        return {}

    agree = 0
    disagreements: list[dict] = []
    for a in judged:
        j = bool(judge_asserts_all(a))
        if j == bool(a.correct):
            agree += 1
        else:
            disagreements.append({
                "fault": a.fault_id,
                "round": a.round_no,
                "keyword_correct": bool(a.correct),
                "judge_asserts_all": j,
                "judge": (a.detail or {}).get("judge", {}),
                "text": a.root_cause[:300],
            })
    return {
        "n_judged": len(judged),
        "judge_agree": agree,
        "judge_disagreements": disagreements,
    }


def _print_judge_agreement(agg: dict) -> None:
    """打印两把尺子的交叉校验结果。没跑裁判时什么都不打。"""
    if not agg.get("n_judged"):
        return
    n = agg["n_judged"]
    agree = agg["judge_agree"]
    print()
    print("  ── 两把尺子（D10）──")
    print(f"     裁判覆盖 {n} 次尝试；与关键词判定一致 {agree}/{n}")
    print("     ⚠️ 裁判**不参与** correct 的计算，只做独立交叉校验")
    ds = agg.get("judge_disagreements") or []
    if not ds:
        print("     分歧：无")
        return
    print(f"     分歧：{len(ds)} 条 —— **这些必须人工读原文判谁对**")
    for d in ds:
        print(f"       · {d['fault']} 第{d['round']}轮　关键词 correct={d['keyword_correct']}"
              f"　裁判 asserts_all={d['judge_asserts_all']}")
        print(f"         裁判逐项：{d['judge']}")
        print(f"         原文：{d['text'][:160]}")

# 单场景样本量的下限：低于它，**单场景准确率只能看方向、不能比幅度**。
#
# 由来（harness-log #46）：D15 主跑得到 multi 90.5% vs baseline 100%，
# 差异**全部**来自 F4 一个 n=3 的格子；而同配置重跑 baseline 在 F4 上自己就从 3/3 掉到 2/3。
# 3 轮下单场景准确率只能取 {0%, 33%, 67%, 100%} —— 相邻两档差 33 个百分点，
# 这种分辨率撑不住"低 9.5 个百分点"这样的精度。
#
# ⇒ 与其靠我记得加一句"样本小"，不如让**报告自己说出来**。
SMALL_SAMPLE_MIN_ROUNDS = 4


def small_sample_warning(n_rounds: int, n_scenarios: int) -> str | None:
    """单场景样本量太小时，返回一句必须打出来的告警；否则返回 None。

    纯函数（不依赖 Report），所以能被单独测试 —— 这也是把它从打印逻辑里抽出来的原因
    （同一个理由见 #33：内联的逻辑没法验）。
    """
    if n_rounds >= SMALL_SAMPLE_MIN_ROUNDS:
        return None
    ladder = " / ".join(f"{round(100 * i / n_rounds)}%" for i in range(n_rounds + 1))
    return (
        f"  ⚠️ 单场景样本太小：每场景只有 {n_rounds} 轮 ⇒ 准确率只能取 {ladder} 这些档位。\n"
        f"     **单场景的差异只能看方向，不能比幅度**（相邻档位差 "
        f"{round(100 / n_rounds)} 个百分点）。\n"
        f"     要比幅度请加大 --rounds；要下结论请把噪声带一起读。"
    )


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
    if report.pricing_tier:
        # ⚠️ 必须打出来：峰时单价是谷时的 2 倍，
        #    **跨时段比较成本会得出假的一倍增长**（harness-log #30，我自己踩过）
        tier_cn = "峰时（单价 2×）" if report.pricing_tier == "peak" else "谷时（单价 1×）"
        print(f"  ⚠️ 计价时段：{tier_cn} —— **只与同时段的运行比较成本**")
    if agg["n_rounds"] < 3:
        print("  ⚠️ 轮数少于 3，噪声带不可信 —— 单轮结果只是在如实反映'没测出波动'")
    print()
    # ⚠️ 作废的场景必须在报告里**显式说出来**，不能只是悄悄不显示。
    #    否则读报告的人会以为"六/七个场景都算进去了"。
    if agg.get("invalidated"):
        print("  ⛔ 以下场景已作废，**不参与上面的任何数字**（harness-log #20）：")
        for fid, reason in agg["invalidated"].items():
            print(f"     {fid}：{reason}")
        print(f"     （被排除 {agg['n_excluded_attempts']} 次尝试，"
              f"本次共 {agg['n_attempts_total']} 次）")
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
        print("     不是模型的归因能力问题。")
        # ⚠️ 这里**必须说清是哪个预算不够**（harness-log #17）。
        #    多 Agent 的步数预算有两个旋钮，原来这条建议只说 --max-steps ——
        #    而实测中没收敛的是**交叉质证**，改 --max-steps 根本改不动它，
        #    照着建议做会白花一遍钱还得不到结论。
        if report.agent == "multi":
            print("     多 Agent 有**两个**步数预算，先看清楚是哪个撞了：")
            print("       · 调查轮（三个专职 Agent）  → --max-steps")
            print("       · 交叉质证轮（三个质证发言）→ --cross-exam-steps")
            print("     下面列出每个环节的 finished / steps，据此定位：")
            for line in _convergence_breakdown(report):
                print(line)
        else:
            print("     请提高 --max-steps 后重测，再做比较。")
    print()
    print("  ── 逐个场景 ──")
    print(f"  {'ID':<4} {'场景':<30} {'准确率':<8} {'步数':<6} {'工具':<6} {'成本':<10}")
    print("  " + "-" * 88)
    for fid, row in agg["per_fault"].items():
        print(f"  {fid:<4} {row['label']:<30} {row['accuracy']:<8.0%} "
              f"{row['mean_steps']:<6.1f} {row['mean_tool_calls']:<6.1f} "
              f"¥{row['mean_cost_yuan']:.4f}")
    small = small_sample_warning(report.rounds, len(agg["per_fault"]))
    if small:
        print()
        print(small)

    # 多故障场景额外打两轴（结论 / 召回）。单故障场景什么也不打。
    _print_cause_axes(report)
    # 假阳性轴（#44）：答案里"把没有基线对比的指标当成异常/原因"的分句
    _print_unsupported_claims(report)
    # 停止原因与打转统计（D9）—— 把"撞预算"和"在打转"分开
    _print_stop_reasons(report)
    # 两把尺子的交叉校验（D10）—— 没跑裁判时什么也不打
    _print_judge_agreement(agg)


def save_report(report: Report) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = RUNS_DIR / "_eval" / f"{report.agent}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 先把轨迹落盘，再写汇总 —— 顺序无所谓，但路径要先进 results.json
    traces_dir = out_dir / "traces"
    for a in report.attempts:
        if not a.trace:
            continue
        traces_dir.mkdir(parents=True, exist_ok=True)
        fp = traces_dir / f"{a.fault_id}-r{a.round_no}.json"
        fp.write_text(
            json.dumps(a.trace, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        a.trace_path = str(fp.relative_to(ROOT))

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
    # ⚠️ 这个旋钮必须存在且必须接线（harness-log #17）。
    #    原先交叉质证的步数预算在 coordinator.py 里写死 4，--max-steps 到不了它；
    #    实测中指标 Agent 正好用满 4 步仍没产出结论 → 收敛率 0%，
    #    而报告却建议"提高 --max-steps"（那个参数根本改不动它）。
    p.add_argument("--cross-exam-steps", type=int, default=14,
                   help="多 Agent 专用：交叉质证轮的 LLM 轮次上限。"
                        "⚠️ 14 是尚未实测校准的临时值（原为写死 4，不够用），"
                        "真实需求由 D12 按实测步数分布定稿")
    p.add_argument("--mode", choices=["live", "record", "replay"], default="live")
    p.add_argument("--agent", choices=["baseline", "multi"], default="baseline",
                   help="baseline = 单 Agent；multi = 三个专职 Agent + 交叉质证 + 裁决")
    p.add_argument("--list-scenarios", action="store_true", help="只看有哪些场景可用")
    p.add_argument("--judge", action="store_true",
                   help="额外跑一次 LLM 裁判做**独立交叉校验**（不参与 correct 计算）；"
                        "两者分歧时会单列出来")
    p.add_argument("--include-invalidated", action="store_true",
                   help="连**已作废**的场景一起跑（结果不参与聚合）。"
                        "只在需要复现'某道题曾经出错过'时用")
    p.add_argument("--guard", action="store_true",
                   help="开**软提醒护栏**（M2，只对 multi 有效）：裁决后跑一次证据审查者，"
                        "若有问题给协调者**一次**自我修正机会。"
                        "⚠️ 审查者的输出**不参与** correct 计算 —— 开/关它才能做 2×2 对照")
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
            sc = SCENARIOS.get(fid)
            if sc is None:
                mark = "(无评分规则)"
            elif sc.invalidated_reason:
                mark = f"⛔ 已作废：{sc.label}"
            else:
                mark = sc.label
            print(f"  {fid:<4} {d.name}  {mark}")
        return 0

    fault_ids = args.faults.split(",") if args.faults else None
    report = run(
        fault_ids=fault_ids,
        rounds=args.rounds,
        model=args.model,
        max_steps=args.max_steps,
        cross_exam_steps=args.cross_exam_steps,
        mode=args.mode,
        agent=args.agent,
        include_invalidated=args.include_invalidated,
        judge_fn=_make_judge_fn() if args.judge else None,
        guard=args.guard,
    )
    print_report(report)
    if report.aborted_reason:
        # ★ 让"为什么没跑完"显式出现在屏幕上，而不是只留一句 traceback（M2 实测撞过 402）
        print(f"\n  ⛔ 本次运行**中途放弃**：{report.aborted_reason}")
        print(f"     已完成的 {len(report.attempts)} 次尝试照常存档 —— 那些是花了钱的。")
    if report.attempts:
        path = save_report(report)
        print(f"\n  结果已存：{path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
