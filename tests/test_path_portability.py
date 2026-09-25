"""归档里的路径必须**跨平台可解析**（D21）。

真实事故（2026-09-26，第一次把 CI 搬到 Linux 上复现时抓到）：

    `save_report` 用 `str(Path)` 写 `trace_path` ⇒ 在 Windows 上产出**反斜杠**；
    而消费方直接 `ROOT / tp` ⇒ **Linux 上找不到文件** ⇒
    交付页**静默丢掉整段 trace**（重生成的页面少 429 行），
    "本页由存档生成"当场变成假话。

为什么这件事值得单独一条用例：本机是 Windows、CI 是 Linux，
**这类缺陷在本机永远看不见**（这也是它一直没被发现的原因）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from scripts import make_demo  # noqa: E402


def _fake_archive(root: Path) -> tuple[Path, dict]:
    """造一个归档：trace 文件真的存在，但 `trace_path` 里写的是**反斜杠**。"""
    trace_dir = root / "runs" / "_eval" / "multi-x" / "traces"
    trace_dir.mkdir(parents=True)
    blocks = [{"phase": "investigate", "role": "logs",
               "steps": [{"step": 1, "tool": "query_logs", "args": "{}", "result": "证据"}]}]
    (trace_dir / "F1-r1.json").write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
    return trace_dir, {
        "fault_id": "F1", "round_no": 1,
        "trace_path": "runs\\_eval\\multi-x\\traces\\F1-r1.json",   # ← Windows 分隔符
        "root_cause": "根因：外部风控变慢。", "correct": True, "cost_yuan": 0.01,
        "steps": 1, "tool_calls": 1, "explanation": "", "finished": True, "parse_ok": True,
    }


def test_page_can_load_a_trace_whose_path_used_windows_separators(tmp_path, monkeypatch) -> None:
    """交付页的 `load_trace` 必须能吃下**反斜杠**路径（老归档就是这样写的）。"""
    _, attempt = _fake_archive(tmp_path)
    monkeypatch.setattr(make_demo, "ROOT", tmp_path)
    assert make_demo.load_trace({"attempts": [attempt]}, "F1", 1), \
        "反斜杠路径在 Linux 上解析不到 ⇒ 页面会静默丢掉整段 trace"


def test_guard_adapter_can_read_a_backslash_trace_path(tmp_path, monkeypatch) -> None:
    """护栏的归档适配器同样要跨平台（否则 M1 的 CLI 在 Linux 上也是空的）。"""
    from rca.guard import adapters

    _, attempt = _fake_archive(tmp_path)
    run_dir = tmp_path / "runs" / "_eval" / "multi-x"
    (run_dir / "results.json").write_text(
        json.dumps({"attempts": [attempt]}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(adapters, "repo_root", lambda: tmp_path)

    pairs = list(adapters.iter_archived_attempts(run_dir))
    assert len(pairs) == 1
    _, trace_path = pairs[0]
    assert trace_path is not None, "反斜杠路径没被归一化 ⇒ Linux 上读不到轨迹"


def test_new_archives_write_posix_separators() -> None:
    """修的另一半：**新写的**归档一律用 POSIX 分隔符（不再产出反斜杠）。

    结构性检查（只看源码里那一处赋值）—— 因为真正写归档要跑一整轮评测，那要花钱。
    """
    src = (Path(__file__).resolve().parent.parent / "eval" / "runner.py").read_text(encoding="utf-8")
    line = next((ln for ln in src.splitlines() if "a.trace_path = str(fp.relative_to(ROOT))" in ln), "")
    assert line, "找不到写 trace_path 的那一行（改名了？这条检查要跟着改）"
    assert '.replace("\\\\", "/")' in line, \
        f"写 trace_path 时没有归一化分隔符 ⇒ Linux 上的归档会读不到轨迹：{line.strip()}"
