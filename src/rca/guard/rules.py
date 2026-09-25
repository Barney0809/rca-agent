"""三条**确定性**规则 —— 护栏的硬层（D21 / M1）。

================================ 为什么只有三条 ================================

护栏只做**形式缺陷**，不做"对错"（ADR-0007 决定 1）。M1 落地三条，
每一条都**不需要标准答案**：只要"证据在不在"。

    1. unsupported_claim      结论里点名的**具体标识符**（指标名/参数名）
                              必须在轨迹里出现过 —— 否则就是凭空发明了一个名字
    2. unbased_metric         被当作根因点名的指标，必须有**参照**（两个值 / 变化箭头 /
                              基线措辞），不能是一个孤零零的绝对值
                              ← 这条正对 harness-log #44 的真实缺陷
    3. unverifiable_number    带单位的测量数字必须可溯源：要么在证据里出现过，
                              要么能由证据里的两个数**一步算出**（±1%）

================================ 刻意的保守与降级 ================================

第 2 条（`unverifiable_number`）宁可**放过**也不误报：模型算出来的派生值
（如 800.5/818 → "98%"）不算幻觉数字 —— 只有"既找不到、又算不出来"的才报。

第 3 条 `unbased_metric` 曾经是旗舰规则，**实测精确率 7% 后被降级为诊断量**
（19 个正确结论里报 14 个）⇒ 它不参与判定，理由写在 `DIAGNOSTIC_RULES` 那段注释里。
这件事本身就是 ADR-0007 决定 1 的**测量版证明**：护栏的表达力边界是真的。

真正决定这些规则好不好用的不是设计，而是**实测的误伤率** ——
见 `scripts/guard_replay.py` 的对照表。**测出误伤就改规则、降级、或如实报出来。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .events import (
    BASELINE_WORDS,
    IDENTIFIER_RE,
    NUMBER_RE,
    UNIT_NUMBER_RE,
    Claim,
    Trace,
    identifiers_in,
    numbers_in,
)

#: 「具体标识符」：**snake_case**（含下划线）或 ENV 风格（大写+下划线），且长度 ≥6。
#:
#: ⚠️ 第一版写成"下划线 / 全大写 / camelCase 都算"，实测**当场误伤**：
#:    结论里写"零 ERROR" ⇒ 规则把 `ERROR` 当成指标名，报了一条 block（F1:1）。
#:    日志级别、格式名、框架类名都不是"点名了某个指标/参数"。
#:    ⇒ 收紧到 snake_case：本世界的指标与开关全是这个形状
#:      （`stock_level` / `leak_bytes_total` / `W_INVENTORY_DOWNSTREAM_RETRIES`）。
_SPECIFIC_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+")

#: 即便含下划线也不是指标名的词（日志级别、协议/格式名、常见技术词）
IDENTIFIER_DENYLIST = frozenset({
    "ERROR", "WARNING", "INFO", "DEBUG", "TRACE", "CRITICAL",
    "json", "http", "https", "utf_8", "read_only", "e_g",
    "RuntimeError", "KeyError", "ValueError", "TypeError",
})

#: 从证据里取 `名字 = 数值` / `名字{标签} = 数值` / `名字: 数值`
#: ⚠️ **标签必须单独捕获**：`stock_level{sku="SKU-001"} = 998092` 与
#:    `stock_level{sku="SKU-002"} = 997912` 是**同一时刻的两个实体**，
#:    不是"这个指标的两个时间点"。第一版把标签丢了，于是 F4 的真缺陷
#:    （无基线的 stock_level 被当根因）**被漏报** —— 实测抓到的。
_METRIC_LINE_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*(\{[^}]*\})?\s*[=:]\s*"
    r"(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
)

#: 单位换算的倍数（证据里是字节/毫秒，结论里可能写成 GB / 秒 / %）
UNIT_SCALES = (1e9, 1e6, 1e3, 1e-3, 1e-6, 1e-9, 100.0, 0.01)

#: 基线措辞的判定见 `_has_baseline_near`（同一行才算）——
#: 词表本身在 `events.py` 里，只有一份（避免两处各写一份、迟早走散）。


def is_specific_identifier(name: str) -> bool:
    """它看起来像"某个具体的指标名/参数名"吗？"""
    if name in IDENTIFIER_DENYLIST or len(name) < 6:
        return False
    return "_" in name


def specific_identifiers(text: str) -> list[str]:
    out: list[str] = []
    for m in _SPECIFIC_RE.finditer(text):
        name = m.group(0)
        if is_specific_identifier(name) and name not in out:
            out.append(name)
    return out


@dataclass(frozen=True)
class Finding:
    """一条判定结果 —— 永远带**证据指针**，否则人无法复核。"""

    rule: str
    severity: str          # block / warn
    subject: str           # 出问题的那个值或标识符
    detail: str
    evidence: tuple[str, ...] = ()

    def line(self) -> str:
        ev = f"  证据：{'、'.join(self.evidence)}" if self.evidence else ""
        return f"[{self.severity.upper()}] {self.rule}：{self.subject} —— {self.detail}{ev}"


# ---------------------------------------------------------------- 证据索引

def metric_series(trace: Trace) -> dict[str, dict[str, set[float]]]:
    """证据里的指标：名字 → 序列（标签）→ 出现过的值。

    **按序列分开**是关键：不同 SKU 的三个值是同一时刻的三个实体，
    不能算作"有基线"。有基线 = **同一个序列**出现过两个不同的值。
    """
    out: dict[str, dict[str, set[float]]] = {}
    for line in trace.evidence_lines():
        for m in _METRIC_LINE_RE.finditer(line):
            name, labels, value = m.group(1), m.group(2) or "", m.group(3)
            try:
                out.setdefault(name, {}).setdefault(labels, set()).add(float(value))
            except ValueError:      # pragma: no cover - 正则形态已保证
                continue
    return out


def metric_values(trace: Trace) -> dict[str, set[float]]:
    """所有序列的值并起来（给"证据里有没有这个指标"用，判基线请用 `metric_series`）。"""
    return {name: {v for vs in series.values() for v in vs}
            for name, series in metric_series(trace).items()}


def _has_baseline_near(trace: Trace, name: str) -> bool:
    """`name` 所在**那一行**有没有"参照"措辞（基线/净增/→/对比…）。

    ⚠️ 第一版用 ±120 字符的窗口，实测太松：指标行旁边常挨着别的行，
       而"→"/"由 " 这类词到处都是 ⇒ 该报的报不出来。
       ⇒ 收紧到**同一行**。
    """
    for line in trace.evidence_lines():
        if name in line and any(w in line for w in BASELINE_WORDS):
            return True
    return False


def _is_measurement_like(raw: str) -> bool:
    """这个数字像"测量值"吗？（带单位 / 科学计数 / ≥4 位 / 有小数）"""
    if re.search(r"[eE][+-]?\d+", raw):
        return True
    if "." in raw:
        return True
    digits = raw.lstrip("0")
    return len(digits) >= 4


def _rounds_to(cand: float, raw: str) -> bool:
    """某个证据值**四舍五入**后能不能等于结论里写的那个数？

    为什么用"四舍五入相等"而不是百分比容差：
    模型算比率时会取整（4433/1400 = 3.166 → 写成 "3.2"），
    1% 的容差恰好差一点点就把它判成幻觉数字 —— 实测误伤过一次（F6:1）。
    "舍入相等"既承认模型会取整，又比放宽容差**更紧**（位数越多要求越严）。
    """
    if "e" in raw.lower():
        mantissa = raw.lower().split("e")[0]
        nd = len(mantissa.split(".")[1]) if "." in mantissa else 0
    else:
        nd = len(raw.split(".")[1]) if "." in raw else 0
    try:
        return round(cand, nd) == float(raw)
    except (ValueError, OverflowError):     # pragma: no cover - 数值极端时兜底
        return False


def _traceable(raw: str, pool: list[float], scaled: list[float]) -> bool:
    """这个数字能不能溯源：证据里出现过（含单位换算）或能由两个证据值一步算出。"""
    try:
        x = float(raw)
    except ValueError:      # pragma: no cover
        return True         # 解析不了就不判它，宁可放过
    if any(_rounds_to(p, raw) for p in scaled):
        return True
    for a in scaled:
        for b in scaled:
            if b == 0:
                continue
            for cand in (a + b, a - b, a * b, a / b):
                if _rounds_to(cand, raw):
                    return True
    return False


# ---------------------------------------------------------------- 三条规则

def rule_unsupported_claim(trace: Trace) -> list[Finding]:
    """结论里点名的具体标识符，必须在轨迹里出现过。"""
    evidence = trace.evidence_text()
    found: list[Finding] = []
    for claim in trace.final_claims:
        for name in specific_identifiers(claim.text):
            if name in evidence:
                continue
            found.append(
                Finding(
                    rule="unsupported_claim",
                    severity="block",
                    subject=name,
                    detail="结论点名了这个标识符，但整条轨迹里没有任何工具证据提到它（疑似凭空发明）",
                    evidence=(trace.pointer(claim.step),),
                )
            )
    return found


def rule_unbased_metric(trace: Trace) -> list[Finding]:
    """被当作根因点名的指标必须有参照，不能是孤零零一个绝对值（#44）。"""
    series = metric_series(trace)
    found: list[Finding] = []
    for claim in trace.final_claims:
        for name in specific_identifiers(claim.text):
            if name not in series:          # 证据里没有 → 归 unsupported_claim 管
                continue
            # 有参照 = **同一个序列**出现过两个不同的值（不同 SKU 不算）
            based = any(len(values) >= 2 for values in series[name].values())
            if based or _has_baseline_near(trace, name):
                continue
            flat = sorted({v for vs in series[name].values() for v in vs})
            found.append(
                Finding(
                    rule="unbased_metric",
                    severity="warn",
                    subject=name,
                    detail=(
                        f"被当作根因，但证据里它只有 {len(flat)} 个值"
                        f"（{flat[0]:g}），**没有任何序列出现过第二个值**，"
                        "也没有基线/变化/对比措辞 —— 无基线的绝对值不能支撑因果"
                    ),
                    evidence=(trace.pointer(claim.step),),
                )
            )
    return found


def rule_unverifiable_number(trace: Trace) -> list[Finding]:
    """带单位的测量数字必须可溯源（出现过，或能由两个证据值一步算出）。"""
    pool = numbers_in(trace.evidence_text())
    # 单位换算：证据里是字节/毫秒，结论里可能写 GB / 秒
    scaled = pool + [v * s for v in pool for s in UNIT_SCALES]
    found: list[Finding] = []
    for claim in trace.final_claims:
        reported: list[str] = []
        for m in UNIT_NUMBER_RE.finditer(claim.text):
            reported.append(m.group(1))          # 带单位的：必须可溯源
        for raw in NUMBER_RE.findall(claim.text):
            if _is_measurement_like(raw) and raw not in reported:
                reported.append(raw)
        for raw in reported:
            if _traceable(raw, pool, scaled):
                continue
            found.append(
                Finding(
                    rule="unverifiable_number",
                    severity="warn",
                    subject=raw,
                    detail="这个测量数字在证据里找不到，也无法由证据里的数字（含单位换算）算出",
                    evidence=(trace.pointer(claim.step),),
                )
            )
    return found


#: 上线的硬规则（顺序即报告顺序）—— 加规则只需在这里登记
RULES = (rule_unsupported_claim, rule_unverifiable_number)

#: **诊断量，不计入判定**（2026-09-25 实测后被降级的规则）
#:
#: `unbased_metric`（"被当作根因的指标必须有参照"）**曾经是** M1 的旗舰规则 ——
#: 它冲着 harness-log #44 那个真实缺陷去的。实测结果（21 次归档尝试）：
#:
#:     F4:3（真坏答案）：报出了 stock_level ✅        ← 它确实抓到了那次真缺陷
#:     F8:1（正确答案）：也报 leak_bytes_total ❌      ← 但正确结论同样会点名单点值的指标
#:     …… 19 个正确结论里报了 14 个
#:
#:     精确率 1/15 ≈ 7% ⇒ **这是一条"狼来了"规则，不能上线。**
#:
#: 为什么修不好：区分"该不该拿这个指标当主根因"本质上是**配权判断**，
#: 需要知道哪个原因才是真的（= 需要标准答案）⇒ 超出护栏的表达力。
#: 这正是 ADR-0007 决定 1 说的那个代价，只不过这次是**用测量**证实了它，而不是靠推理。
#:
#: ⇒ 与 #44 的处置保持一致：**保留为诊断量**（它报出来的东西值得人看一眼），
#:   但不参与 `judge()`。要恢复它必须先把误伤降到可接受，并给出新的测量。
DIAGNOSTIC_RULES = (rule_unbased_metric,)


def run_rules(trace: Trace) -> list[Finding]:
    """跑**上线的**硬规则。

    ⚠️ 没有结论事件时**必须报"无法判定"**，不能返回空列表 ——
       "没看见问题"与"没有问题"必须分开（#26 的空绿教训）。
    """
    if not trace.final_claims:
        return [
            Finding(
                rule="missing_conclusion",
                severity="block",
                subject="(无结论事件)",
                detail="轨迹里没有 Claim(kind=final) —— 无法判定。"
                       "接入方必须在结束时回传结论（submit_conclusion 或由适配器读 stdout）",
            )
        ]
    out: list[Finding] = []
    for rule in RULES:
        out.extend(rule(trace))
    return out


def run_diagnostics(trace: Trace) -> list[Finding]:
    """跑**诊断量**（不计入判定）—— 给人看，不给 `judge()` 看。"""
    out: list[Finding] = []
    for rule in DIAGNOSTIC_RULES:
        out.extend(rule(trace))
    return out


def inject_form_defect(trace: Trace, kind: str = "both") -> Trace:
    """**制造**一个形式缺陷：把结论改写成"点名了不存在的指标 + 报了一个查无此数的值"。

    这是 M1 里"必须触发"那一侧的验收入口 —— 真实归档里没有这种缺陷
    （真的坏答案坏在**配权**上，形式规则看不见，见 `DIAGNOSTIC_RULES` 那段），
    所以用一个**确定性注入**来证明规则不是摆设。
    外部 Agent 的验证同理（ADR-0007 决定 3）：注入已知错误，才有 ground truth。
    """
    from .events import Claim as _Claim

    if not trace.final_claims:
        raise ValueError("这条轨迹没有结论事件，无法注入")
    fake_name = "fabricated_metric_total"
    fake_number = "987654"
    suffix = ""
    if kind in ("identifier", "both"):
        suffix += f" 另外，{fake_name} 也解释了这次异常。"
    if kind in ("number", "both"):
        suffix += f" 该指标的峰值为 {fake_number}。"
    out = Trace(label=f"{trace.label}(已注入形式缺陷)", stop_reason=trace.stop_reason)
    for event in trace.events:
        if isinstance(event, _Claim) and event.kind == "final":
            out.add(_Claim(step=event.step, text=event.text + suffix, kind="final",
                           phase=event.phase, role=event.role))
        else:
            out.add(event)
    return out


__all__ = [
    "DIAGNOSTIC_RULES",
    "Finding",
    "RULES",
    "inject_form_defect",
    "is_specific_identifier",
    "metric_series",
    "metric_values",
    "rule_unbased_metric",
    "rule_unverifiable_number",
    "rule_unsupported_claim",
    "run_diagnostics",
    "run_rules",
    "specific_identifiers",
]
