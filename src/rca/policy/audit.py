"""
审计：每一次策略判定都记账，**拒绝也要记**。

============================ 为什么"拒绝也要记" ============================

需求 FR-2.7。三个理由：

  1) **拒绝是安全事件**：有人（或某个 Agent）试图做越界的事，这本身就是要被看见的。
     只记成功的操作，等于把"入侵尝试"从日志里抹掉。

  2) **拒绝必须可解释**（FR-2.8）：事后要能回答"为什么当时不放行"。

  3) **静默失败是最危险的**：如果拒绝不记账，上层会以为"什么都没发生"，
     而不是"我被拦了"。这两者的处置完全不同。

============================ 复用事件流协议，不另起一套 ============================

记录的形状直接采用 `docs/03-事件流协议.md` 里冻结的 `policy.*` 事件：
`policy.allowed` / `policy.denied` / `policy.quarantined` / `policy.restored`。

为什么不另定一套审计格式：**否则"审计"和"事件流"会各长一份，
字段迟早对不上** —— 而那时候你已经不知道以哪个为准了。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from .verbs import Decision


class AuditLog:
    """只追加（append-only）的 NDJSON 审计日志。

    为什么用 NDJSON（一行一个 JSON）而不是一个大 JSON 数组：
      追加时只需在末尾写一行，不用读回整个文件 ——
      大数组要改一个字符也得重写全文，而且中途崩溃会毁掉整个文件。
      这是日志类数据的标准做法。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 策略执行点可能被并发调用（多个 Agent 同时调工具），所以写入要加锁。
        # 对应 Java：synchronized 块。
        self._lock = threading.Lock()

    def write(
        self,
        decision: Decision,
        *,
        tool: str,
        actor: str = "agent",
        trace_id: str = "",
    ) -> dict:
        """写一条审计记录，返回实际落盘的字典。"""
        data = decision.to_event_data()
        data["op"] = tool or data.get("op", "")

        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "type": decision.event_type,
            "actor": actor,
            "trace_id": trace_id,
            "tool": tool,
            "allowed": decision.allowed,
            "data": data,
        }

        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
        return record

    def read_all(self) -> list[dict]:
        """读回全部审计记录（供测试与人工排查）。"""
        if not self.path.exists():
            return []
        out: list[dict] = []
        for raw in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return out

    def denials(self) -> list[dict]:
        """只取被拒绝的那些 —— 排查时最常看的就是这类。"""
        return [r for r in self.read_all() if not r.get("allowed")]
