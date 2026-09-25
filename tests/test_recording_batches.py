"""录制批次（D19 补完「一批录制 = 一次运行」）的用例。

============================ 修的是什么 ============================

录制文件是**追加**的，同一个 key 会被后来的运行反复写入，而旧代码是
"后写者胜"（`self._index[key] = rec`）。后果实测过（harness-log #53）：

    同一批响应下，归档判 19/21、回放判 21/21；21 次尝试里 **13 次步骤数不同**、
    F4 的第 1、3 轮判定直接翻转 —— 原因是录制里混了 **4 次 record 运行**，
    回放只能挑到"录制内部自洽的那条轨迹"，**不保证是你想复现的那一次**。

修法：录的时候标批次，回放时指定同一批次；**指定了就只认它**。

⚠️ 最关键的一条不变量：**不许静默串批**。
   指定的批次里没有某条 ⇒ 那是"未命中"（replay 模式下直接报错），
   而不是"退而用别的批次的响应" —— 后者会把"复现另一次运行"伪装成"复现这一次"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from rca.llm.recording import Recorder  # noqa: E402

MSG = [{"role": "user", "content": "同一个输入"}]


def _record(path: Path, batch: str, reply: str) -> None:
    rec = Recorder(path, mode="record", batch=batch)
    rec.save(tag="t", model="m", messages=MSG, response={"reply": reply})


def _replay(path: Path, batch: str) -> Recorder:
    return Recorder(path, mode="replay", batch=batch)


# ---------------------------------------------------------------- 每一批各归各

def test_each_batch_replays_its_own_response(tmp_path: Path) -> None:
    """同一个 key 在两批里各录一次 —— 回放哪一批就得到哪一批的响应。

    这正是修复前的毛病：后写的那批会把先写的**盖掉**。
    """
    p = tmp_path / "rec.ndjson"
    _record(p, "run-a", "A 的响应")
    _record(p, "run-b", "B 的响应")

    assert _replay(p, "run-a").lookup(tag="t", model="m", messages=MSG) == {"reply": "A 的响应"}
    assert _replay(p, "run-b").lookup(tag="t", model="m", messages=MSG) == {"reply": "B 的响应"}


def test_no_batch_keeps_the_old_behaviour(tmp_path: Path) -> None:
    """**不指定批次时行为完全不变**（后写者胜）—— 旧录制与旧命令照常能用。"""
    p = tmp_path / "rec.ndjson"
    _record(p, "run-a", "A 的响应")
    _record(p, "run-b", "B 的响应")
    assert Recorder(p, mode="replay").lookup(tag="t", model="m", messages=MSG) == {"reply": "B 的响应"}


def test_batch_is_written_into_the_recording(tmp_path: Path) -> None:
    p = tmp_path / "rec.ndjson"
    _record(p, "run-a", "A 的响应")
    first = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert first["batch"] == "run-a"
    assert first["tag"] == "t" and first["model"] == "m"


# ---------------------------------------------------------------- 绝不串批

def test_unknown_batch_misses_instead_of_borrowing_another(tmp_path: Path) -> None:
    """指定的批次里没有这条 ⇒ **未命中报错**，而不是拿别批的响应顶上。

    这是本功能的核心不变量：串批会把"复现了另一次运行"伪装成"复现了这一次"，
    而那种错误**没有任何报错**（#53 就是这么来的）。
    """
    p = tmp_path / "rec.ndjson"
    _record(p, "run-a", "A 的响应")
    with pytest.raises(RuntimeError, match="未命中"):
        _replay(p, "run-c").lookup(tag="t", model="m", messages=MSG)


def test_same_batch_does_not_write_the_same_key_twice(tmp_path: Path) -> None:
    """同一批内重复 save 是幂等的（否则文件会被重复条目撑大）。"""
    p = tmp_path / "rec.ndjson"
    _record(p, "run-a", "A 的响应")
    _record(p, "run-a", "A 的响应（重复）")
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, f"同批同 key 应只写一行，实际 {len(lines)} 行"
    assert _replay(p, "run-a").lookup(tag="t", model="m", messages=MSG) == {"reply": "A 的响应"}
