"""
单 Agent baseline —— 对照组的基准线。

============================ 它为什么是整个项目的灵魂 ============================

D6/D7 要做多 Agent 协作。但如果**没有这条基准线**，
"多 Agent 让准确率从 X 提到 Y"这句话里的 X 就是编的。

所以顺序不能反：

    D5  先做单 Agent baseline  →  拿到 准确率 / 步数 / 成本 三个数字
    D7  再做多 Agent           →  才有"提升"可谈

============================ 一个必须提前说清楚的诚实判断 ============================

我们的降维层（D3）把 1.67M tokens 的原始遥测压到了约 3k tokens。
这意味着 **单 Agent 根本不会遇到上下文压力** ——

    需求假设 A1 说的是"原始遥测装不下"，
    但降维层在 Agent 之前就替它解决了这个问题。

所以单 Agent 的 baseline **可能相当好**。而这正是 D5 要测出来的东西。

**如果 baseline 已经很强，那"多 Agent 提升多少"就是个诚实的零或负数。**
如实报告这一点，比编一个漂亮的提升数字有价值得多 ——
而"我先建了基准线，然后发现……"本身就是最有说服力的工程叙事。

============================ 公平性：prompt 必须中立 ============================

baseline 与将来的多 Agent 用**完全相同的工具面**和**同等质量的 prompt**。
唯一变量只能是**协作结构**。

所以这里的 prompt 里：
    ✅ 有通用的良好实践（先取证再下结论；有变更不等于变更是根因）
    ❌ 没有任何指向具体故障的提示
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from ..llm.provider import DeepSeekClient, LlmResult
from ..tools import RunContext, ToolBox

# ================================================================
# Prompt
# ================================================================

SYSTEM_PROMPT = """\
你是一名资深 SRE，负责对线上系统做故障根因分析（RCA）。

你可以调用工具获取三类证据：日志、运行指标、配置变更记录。

工作原则：
1. **先取证，再下结论。** 任何结论都必须能指出支撑它的具体证据。
2. **区分"症状"与"根因"。** 你最先看到的现象，往往是被放大后的结果，
   而不是原因本身。遇到非常显眼的异常时，问自己：它能不能解释其他现象？
3. **变更记录只是线索之一。** 这段时间里有配置被改过，不代表它就是根因；
   反过来，根因也可能根本不是一次配置变更。
4. **注意耗时断层出现在哪一层。** 如果上游慢而下游正常，问题在上游；
   如果逐层都慢，问题在最下游。
5. ★ **同一段时间里可能同时存在多件互不相干的事。**
   找到第一个异常**不等于**任务完成 —— 它只是完成了排查的一部分。
   交卷前必须主动回答一遍：**"还有没有别的异常，是我改了它也不会消失的？"**
   两个独立问题必须分别列出；**只报一个就是不完整的结论。**
6. 证据不足时，宁可降低置信度，也不要猜。

最后一次回复必须是**纯 JSON**（不要包在代码块里，不要有其他文字）：
{
  "root_causes": ["一句话说清一个根本原因", "如果存在第二个独立原因，写在这里"],
  "evidence": ["支撑上述结论的具体证据", "..."],
  "confidence": 0.0
}

⚠️ `root_causes` 是一个**列表**，不是一句话。
  只有一个原因时就写一条；**有多条独立原因时必须全部列出**（见原则 5）。
"""

TASK_PROMPT = """\
被诊断系统在刚才一段时间内出现了故障。
请用工具收集证据，找出这段时间内**所有**异常现象及其根本原因。

⚠️ 这段时间里**可能同时有好几个互不相干的问题**，请把它们**分别列出**；
   只报出一个就交卷会被判为不完整。

系统由三个服务组成，调用链为：order → inventory → payment → 外部风控。
可用工具：query_logs / query_metrics / get_changes。

请开始。"""


# ================================================================
# 结果
# ================================================================

@dataclass
class Diagnosis:
    """一次诊断的完整结果 —— 三个验收数字都在这里。"""

    root_cause: str = ""
    # ★ 2026-09-25 起，任务改成"列出**所有**异常及其根因"，
    #   所以结论是一个**列表**。`root_cause` 保留为"列表拼起来的文本" ——
    #   评分与存档一直用它，改名会让所有历史数据对不上。
    root_causes: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    parse_ok: bool = False          # 模型是否给出了合法的 JSON 结论

    steps: int = 0                  # LLM 调用轮次
    tool_calls: int = 0             # 工具调用次数
    cost_yuan: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_s: float = 0.0
    finished: bool = False          # 是否正常收敛（而不是撞上步数上限）
    replay_hits: int = 0

    raw_text: str = ""              # 模型最后一次的原文
    trace: list[dict] = field(default_factory=list)   # 完整轨迹（用于回放与审阅）

    def to_dict(self) -> dict:
        return {
            "root_cause": self.root_cause,
            "root_causes": self.root_causes,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "parse_ok": self.parse_ok,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "cost_yuan": round(self.cost_yuan, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "elapsed_s": round(self.elapsed_s, 1),
            "finished": self.finished,
            "replay_hits": self.replay_hits,
        }


# ================================================================
# Agent
# ================================================================

_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _parse_root_causes(parsed: dict) -> list[str]:
    """从模型给出的 JSON 里取出"根本原因"列表。

    ⚠️ 必须**两种格式都认**：

      `root_causes: [...]` —— 2026-09-25 之后的任务契约（可以列出多个原因）
      `root_cause: "..."`  —— 旧契约（单数）

    为什么两种都认：模型经常"记得旧格式"，而且**回放（replay）存档里存的是旧格式**。
    只认新格式会让所有历史录像解析失败，变成一堆 `parse_ok=False` ——
    那会让"改了任务"看起来像"模型变差了"。

    字符串形式的 `root_causes`（模型没写数组而是写了一句话）也要容忍：
    按分号/换行切成多条，切不开就当成一条。
    """
    raw = parsed.get("root_causes")
    if raw is None:
        raw = parsed.get("root_cause")

    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        parts = [p.strip() for p in re.split(r"[；;\n]", text) if p.strip()]
        return parts or [text]
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [str(raw).strip()]


def extract_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON。

    为什么要这么宽容：即便 prompt 里说了"必须是纯 JSON"，
    模型仍可能加一句"好的，结论如下："或者包 ```json 代码块。
    抠不出来就退化成"整段文本当作 root_cause"，而不是整轮失败。
    """
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


class BaselineAgent:
    """单 Agent：一个模型、一套工具、一口气把根因定下来。"""

    def __init__(
        self,
        client: DeepSeekClient,
        *,
        model: str | None = None,
        max_steps: int = 8,
    ) -> None:
        self.client = client
        self.model = model
        self.max_steps = max_steps

    def diagnose(self, ctx: RunContext) -> Diagnosis:
        # ⚠️ 延迟导入：`contract` 反向依赖本模块的 `extract_json`，
        #    模块级导入会形成循环。
        from .contract import repair_messages

        box = ToolBox(ctx)
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": TASK_PROMPT},
        ]

        diag = Diagnosis()
        nudged = False      # 「JSON 催促」只做一次
        started = time.perf_counter()

        for step in range(1, self.max_steps + 1):
            diag.steps = step
            result: LlmResult = self.client.chat(
                messages=messages,
                model=self.model,
                tools=box.specs(),
                tag=f"baseline/{ctx.run_id}",
            )

            self._accumulate(diag, result)

            if result.usage.get("__replayed__"):
                diag.replay_hits += 1

            # ---- 有工具调用 → 执行并继续 ----
            if result.tool_calls:
                # 直接把响应里的 assistant 消息塞回去，保留全部字段
                # （比手工构造更稳：不会漏掉某些协议要求的字段）
                messages.append(result.raw["choices"][0]["message"])
                for tc in result.tool_calls:
                    out = box.call(tc.name, tc.arguments)
                    diag.trace.append(
                        {"step": step, "tool": tc.name, "args": tc.arguments, "result": out}
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": out}
                    )
                continue

            # ---- 没有工具调用 → 这是最终结论 ----
            diag.raw_text = result.text
            parsed = extract_json(result.text)
            if parsed:
                diag.root_causes = _parse_root_causes(parsed)
                # 评分与存档用的是拼起来的文本 —— 一个字段名都不改，
                # 否则历史数据全部对不上（详见 Diagnosis 里的注释）
                diag.root_cause = "；".join(diag.root_causes)
                ev = parsed.get("evidence") or []
                diag.evidence = [str(e) for e in ev] if isinstance(ev, list) else [str(ev)]
                try:
                    diag.confidence = float(parsed.get("confidence", 0.0))
                except (TypeError, ValueError):
                    diag.confidence = 0.0
                diag.parse_ok = True
            else:
                # ---- 抠不出 JSON：**先催一次**，不要直接就收 ----
                #
                # 为什么值得多花一次调用：解析失败时，评分只能拿
                # **未经约束的原始文本**去判 —— 那里面混着模型的**推理过程**，
                # 而不是它的**结论**。而"推理里提过某个词"与"结论里主张某件事"
                # 是两件事（#16/#21 两次假阳性的根源）。
                # ⇒ 解析失败会**让测量口径悄悄变松**，不只是"丢一次数据"。
                #
                # 最多催一次（`nudged`），之后按原样接受，避免在格式上无限纠缠。
                if not nudged:
                    nudged = True
                    messages = repair_messages(messages, result.text)
                    continue

                diag.root_cause = result.text.strip()
                diag.parse_ok = False
            diag.finished = True
            break

        diag.tool_calls = ctx.tool_calls
        diag.elapsed_s = time.perf_counter() - started
        return diag

    @staticmethod
    def _accumulate(diag: Diagnosis, result: LlmResult) -> None:
        diag.cost_yuan += result.cost_yuan
        diag.input_tokens += int(result.usage.get("prompt_tokens", 0) or 0)
        diag.output_tokens += int(result.usage.get("completion_tokens", 0) or 0)
