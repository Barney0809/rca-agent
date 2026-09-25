"""`docs/00-状态.md` 的「当前事实」必须与实测一致（D20：数字不许漂）。

============================ 为什么需要这一条 ============================

`docs/00-状态.md` 自称**唯一权威**，可它自己也漂过 —— 都是一小时前抓到的：

  · 加了 1 条用例之后，它还写着 **254 passed**（实为 255）；
  · 另一处还留着「当前全仓 **27 组 / 60 个**」（实为 39 组 / 91 个）。

这类错误**没有任何报错**：数字只是慢慢变成谎话。
而本项目对外卖的就是"每个数字都能被陌生人核对" ⇒ 权威文件漂了，全盘皆输。
（同族：#37 页面说了假话、#41 README 把设计画成实现、#53 证据只在本机。）

============================ 三条守卫，对应三种漂移 ============================

  1. 用例数   ← 实测收集数（子进程 `pytest --collect-only`）
  2. 变异数   ← `scripts/mutations.json` 的真实条目数
  3. 封堵条数 ← `docs/harness-log.md` 总览表（含"变异证明 / 靠人"的拆分）

⚠️ 每条都**先断言"我读到了东西"，再比大小**。
   否则解析器一坏，守卫就退化成"什么都没读到 ⇒ 一切正常" ——
   这正是 `#26` 里已经踩过一次的坑（空绿比错误更危险）。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS = ROOT / "docs" / "00-状态.md"
sys.path.insert(0, str(ROOT))


def _status_text() -> str:
    assert STATUS.exists(), f"唯一权威文件不见了：{STATUS}"
    text = STATUS.read_text(encoding="utf-8")
    assert "当前事实" in text, "docs/00 里找不到「当前事实」小节 —— 守卫不能空转"
    return text


# ---------------------------------------------------------------- 1) 用例数

def _parse_status_numbers(text: str) -> dict[str, int]:
    out: dict[str, int] = {}

    m = re.search(r"\*\*(\d+) passed / (\d+) skipped\*\*", text)
    assert m, "没读到 docs/00 的用例数 —— 守卫不能退化成空绿"
    out["passed"], out["skipped"] = int(m.group(1)), int(m.group(2))

    m = re.search(r"\*\*(\d+) 组 / (\d+) 个变异体", text)
    assert m, "没读到 docs/00 的变异数 —— 守卫不能退化成空绿"
    out["groups"], out["mutants"] = int(m.group(1)), int(m.group(2))

    m = re.search(r"封堵清单：\*\*(\d+) 条\*\*（其中 \*\*(\d+) 条\*\*由变异测试证明能变红，\*\*(\d+) 条\*\*", text)
    assert m, "没读到 docs/00 的封堵条数（含「变异证明 / 靠人」的拆分）—— 守卫不能退化成空绿"
    out["sealed"], out["machine"], out["human"] = (int(m.group(i)) for i in (1, 2, 3))
    return out


def _pytest_collection() -> tuple[int, dict[str, int]]:
    """实测收集数：总数 + 每个文件的条数。

    两个坑：
      1. **不加 `-q`**：`pyproject` 的 `addopts` 里已经有一个，再加一个会变成 `-qq`，
         pytest 会**连 "N tests collected" 那一行一起吞掉**（总览里那句提醒就是这个）。
      2. **每文件的条数从 node id 数出来**：单 `-q` 的收集输出里没有
         `path: N` 那种行（那是 `-qq` 的格式），但每个用例都会打印一行 node id。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600,
    )
    out = proc.stdout + proc.stderr

    counts: dict[str, int] = {}
    for m in re.finditer(r"^(tests[/\\][\w/\\.]+\.py)::", out, re.M):
        key = m.group(1).replace("\\", "/")
        counts[key] = counts.get(key, 0) + 1
    assert counts, (
        "没能从 pytest 的输出里数出任何 node id —— 守卫不能退化成空绿。\n"
        f"输出末 500 字：\n{out[-500:]}"
    )

    reported = None
    for m in re.finditer(r"(\d+) tests? collected", out):
        reported = int(m.group(1))
    assert reported is not None, (
        "没能读到 pytest 的收集总数那一行 —— 守卫不能退化成空绿。\n"
        f"输出末 500 字：\n{out[-500:]}"
    )
    assert reported == sum(counts.values()), (
        f"pytest 报 {reported} 条，但按 node id 数出来 {sum(counts.values())} 条 —— 解析口径不一致"
    )
    return reported, counts


def test_status_test_count_matches_reality() -> None:
    want = _parse_status_numbers(_status_text())
    collected, _ = _pytest_collection()
    assert want["passed"] + want["skipped"] == collected, (
        f"docs/00 写的是 {want['passed']} passed / {want['skipped']} skipped"
        f"（合计 {want['passed'] + want['skipped']}），实测收集 {collected} 条 —— "
        f"改完用例要同步「当前事实」"
    )


def test_status_deliverable_count_matches_reality() -> None:
    """复跑清单里那句「跑交付物用例 → ✅ N passed」也必须是真的。"""
    text = _status_text()
    m = re.search(r"跑交付物用例.*?✅ (\d+) passed", text, re.S)
    assert m, "没读到「跑交付物用例」那一行 —— 守卫不能退化成空绿"
    _, per_file = _pytest_collection()
    key = next((k for k in per_file if k.replace("\\", "/").endswith("tests/test_deliverable.py")), None)
    assert key is not None, "pytest 的收集输出里没有 test_deliverable.py 的条数"
    assert int(m.group(1)) == per_file[key], (
        f"docs/00 写交付物用例 {m.group(1)} passed，实测 {per_file[key]} 条"
    )


# ---------------------------------------------------------------- 2) 变异数

def test_status_mutation_counts_match_mutations_json() -> None:
    want = _parse_status_numbers(_status_text())
    data = json.loads((ROOT / "scripts" / "mutations.json").read_text(encoding="utf-8"))
    assert data, "mutations.json 里一个组都没有 —— 守卫不能退化成空绿"
    groups = len(data)
    mutants = sum(len(g.get("mutations", [])) for g in data.values())
    assert (want["groups"], want["mutants"]) == (groups, mutants), (
        f"docs/00 写 {want['groups']} 组 / {want['mutants']} 个，"
        f"mutations.json 实际 {groups} 组 / {mutants} 个"
    )


# ---------------------------------------------------------------- 3) 封堵条数

def test_status_seal_counts_match_harness_log() -> None:
    from scripts import make_demo

    want = _parse_status_numbers(_status_text())
    rows = make_demo.seal_table()
    assert rows, "总览表里一条都没读到 —— 守卫不能退化成空绿"
    machine = sum(1 for r in rows if "🟢" in r[2])
    human = len(rows) - machine
    assert (want["sealed"], want["machine"], want["human"]) == (len(rows), machine, human), (
        f"docs/00 写 {want['sealed']} 条（变异证明 {want['machine']} / 靠人 {want['human']}），"
        f"总览表实际 {len(rows)} 条（变异证明 {machine} / 靠人 {human}）"
    )
