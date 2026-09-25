"""参考适配器：**自家 multi 流程的归档轨迹 → 标准事件流**（D21 / M1）。

ADR-0007 决定 4：第一个适配器必须是自家轨迹，因为只有它同时具备
**标准答案**（可测误伤）与**已归档的真坏轨迹**（可零成本验证会触发）。

============================ 归档里有什么 ============================

    runs/_eval/multi-*/results.json
        每次尝试一条：fault_id / round_no / correct / steps / tool_calls /
                      root_cause / explanation / trace_path
    runs/_eval/multi-*/traces/<F>-r<N>.json
        [ {"phase": "investigate", "role": "logs", "steps": [{"step","tool","args","result"}, …]}, … ]
        共 6 块（3 个专员 + 3 次交叉质证）

⚠️ **结论文本不在 trace 里** —— 它在 `results.json` 的 `root_cause`。
   这不是缺陷，而是 ADR-0007 决定 3 说的那件事：行动面（工具调用）与
   判断面（结论）来自两条不同的通道，护栏必须把两者拼起来才能判。
   对第三方 Agent，判断面由 `submit_conclusion` 回传 —— 这里是同一个道理的本地版本。

⚠️ baseline 的归档**没有** trace（历史原因：早期没存），所以 M1 的参考适配器
   面向 multi。这是如实的边界，不假装 baseline 也能用。
"""

from __future__ import annotations

import json
from pathlib import Path

from .events import Claim, ToolCall, ToolResult, Trace


def trace_from_archived(attempt: dict, trace_path: Path) -> Trace:
    """把一次归档尝试（results.json 的一条 + 它的 trace 文件）翻译成标准事件流。"""
    trace = Trace(
        label=f"{attempt.get('fault_id', '?')} r{attempt.get('round_no', '?')}",
        stop_reason=str(attempt.get("stop_reason") or ""),
    )

    step_no = 0
    blocks = json.loads(trace_path.read_text(encoding="utf-8"))
    if not isinstance(blocks, list):      # pragma: no cover - 归档格式变了要显式报错
        raise ValueError(f"trace 格式不是列表：{trace_path}")
    for block in blocks:
        phase = str(block.get("phase") or "")
        role = str(block.get("role") or "")
        for raw in block.get("steps") or []:
            step_no += 1
            name = str(raw.get("tool") or "?")
            trace.add(ToolCall(step=step_no, name=name, args=str(raw.get("args") or ""),
                               phase=phase, role=role))
            trace.add(ToolResult(step=step_no, name=name, text=str(raw.get("result") or ""),
                                 ok=True, phase=phase, role=role))

    # 判断面：归档里的结论（第三方 Agent 走 submit_conclusion，语义相同）
    conclusion = str(attempt.get("root_cause") or "").strip()
    if conclusion:
        trace.add(Claim(step=step_no + 1, text=conclusion, kind="final", phase="conclude"))
    return trace


def repo_root() -> Path:
    """仓库根（含 pyproject.toml 的那一层）—— 归档里的 trace_path 是相对它的。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parents[3]      # pragma: no cover - 兜底


def iter_archived_attempts(run_dir: Path):
    """遍历一次归档运行里的所有尝试：(attempt 字典, trace 路径 or None)。"""
    results = run_dir / "results.json"
    if not results.exists():
        return
    data = json.loads(results.read_text(encoding="utf-8"))
    root = repo_root()
    for attempt in data.get("attempts", []):
        # ⚠️ 归一化分隔符：老归档存的是 Windows 反斜杠，Linux 上 `root / tp` 找不到文件
        rel = str(attempt.get("trace_path") or "").replace("\\", "/")
        path: Path | None = Path(rel) if rel else None
        if path is not None and not path.is_absolute():
            # ⚠️ 归档里存的是**相对仓库根**的路径，不是相对当前工作目录 ——
            #    用 cwd 解析会在别的目录下跑时静默变成"没有 trace"（#38 那族）。
            path = root / path
        yield attempt, (path if path is not None and path.exists() else None)
