"""
专职 Agent —— 每个只看一路数据，并必须说明"我需要别人提供什么"。

============================ 与 baseline 的关系 ============================

D5 的 `baseline.py` 是**对照基准线**，它的数字（准确率 61.1%）已经写进
`docs/05-单Agent基线结果.md`。

⚠️ **因此 baseline.py 不允许再改动行为** ——
   改了它，D5 的数字就失效，"提升了多少"也就无从谈起。

所以本模块是**另起一份**实现，而不是把 baseline 重构成通用类。
重复一点代码，换对照实验的有效性 —— 这笔账划得来。

============================ 它针对 D5 的哪几个靶子 ============================

   靶子 2（漏掉安静信号，F6=33%）
       → MetricsAgent 只能调 query_metrics，**没有别的地方可看**。
         被响亮的 ERROR 日志吸走这件事，在结构上不可能发生。

   靶子 4（工具调用低效，平均 24 次）
       → 每个 Agent 的工具面只有 1 个，搜索空间小得多。

   靶子 3（不收敛 / 不产出结论）
       → 职责更窄 + 输出契约更简单，更容易收敛到结构化结论。

   靶子 1（归因错误）**不在本模块解决** —— 那需要 D7 的 Coordinator
      做交叉举证。本模块只负责产出**可被质证的**假设。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..llm.provider import DeepSeekClient, LlmResult
from ..tools import RestrictedToolBox, RunContext
from .baseline import extract_json
from .roles import ALL_ROLES, Role

TASK_PROMPT = """\
被诊断系统在刚才一段时间内出现了故障。系统调用链为：
order → inventory → payment → 外部风控。

请用你唯一的工具收集证据，基于**你能看到的那一路数据**给出你的判断。
记住：你看不到的部分由其他同事负责，请在 conclusion 里说明你需要什么。
请开始。"""


@dataclass
class Hypothesis:
    """一个专职 Agent 产出的假设。

    `needs_from_others` 是 D7 交叉举证机制的**输入** ——
    没有它，隔离只会让每个 Agent 变成残废。
    """

    role: str
    name: str
    claim: str = ""
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    needs_from_others: list[str] = field(default_factory=list)

    parse_ok: bool = False
    steps: int = 0
    tool_calls: int = 0
    denied_tool_calls: int = 0      # 越权尝试次数（应当为 0）
    repeat_calls: int = 0           # 参数完全相同的重复工具调用次数（D9 打转检测）
    cost_yuan: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_s: float = 0.0
    finished: bool = False
    raw_text: str = ""
    trace: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "name": self.name,
            "claim": self.claim,
            "confidence": round(self.confidence, 2),
            "evidence": self.evidence,
            "needs_from_others": self.needs_from_others,
            "parse_ok": self.parse_ok,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "denied_tool_calls": self.denied_tool_calls,
            # ⚠️ 与 #18 同一个教训：**存档少一个字段，事后就少一条线索**。
            #    打转统计如果不进存档，报告里"步数偏高"这个现象就永远说不清
            #    是"在深挖"还是"在打转"。
            "repeat_calls": self.repeat_calls,
            "cost_yuan": round(self.cost_yuan, 6),
            "elapsed_s": round(self.elapsed_s, 1),
            "finished": self.finished,
        }


class SpecialistAgent:
    """一个专职 Agent：一个角色、一个工具、一份结构化结论。"""

    def __init__(
        self,
        client: DeepSeekClient,
        role: Role,
        *,
        model: str | None = None,
        max_steps: int = 6,
    ) -> None:
        self.client = client
        self.role = role
        self.model = model
        self.max_steps = max_steps

    def investigate(self, ctx: RunContext) -> Hypothesis:
        # ⚠️ 用 fork() 拿独立计数器 —— 三个 Agent 并发跑时统计不能互相污染
        local_ctx = ctx.fork()
        box = RestrictedToolBox(
            local_ctx, frozenset({self.role.tool}), role=self.role.key
        )

        messages: list[dict] = [
            {"role": "system", "content": self.role.system_prompt},
            {"role": "user", "content": TASK_PROMPT},
        ]
        h = Hypothesis(role=self.role.key, name=self.role.name)
        started = time.perf_counter()
        diag_cost = 0.0

        for step in range(1, self.max_steps + 1):
            h.steps = step
            result: LlmResult = self.client.chat(
                messages=messages,
                model=self.model,
                tools=box.specs(),
                tag=f"specialist/{self.role.key}/{local_ctx.run_id}",
            )
            diag_cost += result.cost_yuan
            h.input_tokens += int(result.usage.get("prompt_tokens", 0) or 0)
            h.output_tokens += int(result.usage.get("completion_tokens", 0) or 0)

            if result.tool_calls:
                messages.append(result.raw["choices"][0]["message"])
                for tc in result.tool_calls:
                    out = box.call(tc.name, tc.arguments)
                    h.trace.append(
                        {"step": step, "tool": tc.name, "args": tc.arguments,
                         "result": out, "ok": tc.name == self.role.tool}
                    )
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
                continue

            # 最终结论
            h.raw_text = result.text
            parsed = extract_json(result.text)
            if parsed:
                h.claim = str(parsed.get("claim", "")).strip()
                ev = parsed.get("evidence") or []
                h.evidence = [str(e) for e in ev] if isinstance(ev, list) else [str(ev)]
                nd = parsed.get("needs_from_others") or []
                h.needs_from_others = (
                    [str(x) for x in nd] if isinstance(nd, list) else [str(nd)]
                )
                try:
                    h.confidence = float(parsed.get("confidence", 0.0))
                except (TypeError, ValueError):
                    h.confidence = 0.0
                h.parse_ok = True
            else:
                h.claim = result.text.strip()
                h.parse_ok = False
            h.finished = True
            break

        h.tool_calls = local_ctx.tool_calls
        h.denied_tool_calls = len(box.denials)
        h.repeat_calls = local_ctx.repeat_calls      # ★ 打转统计（D9）
        h.cost_yuan = diag_cost
        h.elapsed_s = time.perf_counter() - started
        return h


def investigate_all(
    client: DeepSeekClient,
    ctx: RunContext,
    *,
    model: str | None = None,
    max_steps: int = 6,
    parallel: bool = True,
    roles: tuple[Role, ...] = ALL_ROLES,
) -> list[Hypothesis]:
    """让三个专职 Agent 各自调查，返回三份假设。

    默认**并发**：它们是三个独立的观察者，没有理由互相等待。
    并发也是多 Agent 相对单 Agent 的天然优势之一（延迟 ≈ 最慢的那个，
    而不是三者之和）。
    """
    if not parallel:
        return [
            SpecialistAgent(client, r, model=model, max_steps=max_steps).investigate(ctx)
            for r in roles
        ]

    with ThreadPoolExecutor(max_workers=len(roles)) as pool:
        futures = {
            pool.submit(
                SpecialistAgent(client, r, model=model, max_steps=max_steps).investigate,
                ctx,
            ): r
            for r in roles
        }
        # 保持 roles 的原始顺序，便于稳定输出
        by_role: dict[str, Hypothesis] = {}
        for fut, r in futures.items():
            by_role[r.key] = fut.result()
    return [by_role[r.key] for r in roles]
