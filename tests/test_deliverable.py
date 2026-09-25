"""L3 产物（公开页面）与 `cost_breakdown.py` 的回归测试。

================================ 为什么会有这个文件 ================================

D13 交付了 `demo/rca-demo.html`，上面写着**"本页由存档生成"** —— 这是一句
**可核对的主张**，那就必须真的能被核对。而在此之前：

  · `scripts/make_demo.py` 一个用例都没有
  · `scripts/cost_breakdown.py` 的切段逻辑（#33）也没有

补测试的过程直接抓到三个**真缺陷**（已记入 harness-log #37）：
  1. 存档里没有 `pricing_tier` 字段时（该字段是后来才加的），页面渲染出
     `baseline <code></code>` —— 一个**空白格**，而那一行正是"只与同时段比较"
     这句话的落点。空白看起来像"没这回事"，实际是"当时没记"。
  2. 样本口径"7 场景 × 3 轮"是**手打的**，不是从存档推的 —— 换一次运行就会对不上。
  3. 第三节的故障描述写死成 F8 的说明，而那个场景是**可能退回 F1** 的。

结论照旧：**这类缺陷从来看不出来，只有用例能看出来。**
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import cost_breakdown, make_demo  # noqa: E402


# --------------------------------------------------------------------------- #
# 造存档
# --------------------------------------------------------------------------- #
def _attempt(
    fault: str,
    rnd: int,
    *,
    cost: float = 0.01,
    steps: int = 5,
    correct: bool = True,
    finished: bool = True,
    parse_ok: bool = True,
    out_tokens: int = 0,
) -> dict:
    return {
        "fault_id": fault,
        "round_no": rnd,
        "correct": correct,
        "steps": steps,
        "cost_yuan": cost,
        "finished": finished,
        "parse_ok": parse_ok,
        "root_cause": f"{fault} 第 {rnd} 轮的结论",
        "out_tokens": out_tokens,
    }


def _archive(started_at: str, attempts: list[dict], tier: str | None = None) -> dict:
    data = {
        "agent": "rca",
        "model": "deepseek-flash",
        "mode": "multi",
        "rounds": 1,
        "started_at": started_at,
        "attempts": attempts,
    }
    if tier is not None:
        data["pricing_tier"] = tier
    return data


def _write_archive(root: Path, name: str, data: dict) -> Path:
    d = root / "runs" / "_eval" / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "results.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture()
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把生成器的 ROOT 换成一个**独立沙盒**，里面自己造存档。

    ⚠️ 不能依赖真实存档：`runs/` 是被 gitignore 的（`.gitignore:42`），
       依赖它等于让用例在"刚克隆下来"时必红。
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "harness-log.md").write_text(
        "## 📊 封堵状态总览\n\n"
        "| 编号 | 缺陷 | 回归用例 | 变异测试 | 状态 |\n|---|---|---|---|---|\n"
        "| #1 | 误删目录 | ✅ | ❌ | 🟡 部分 |\n"
        "| #33 | 追加录制按整文件统计 | ✅ | ✅ | 🟢 已封堵 |\n"
        "| #33 | 追加录制按整文件统计 | ✅ | ✅ | 🟢 已封堵 |\n"   # 总览表里的重复行（P2 就踩过）
        "| P9 | 提交不按测试结果设闸 | ✅ | ❌ | ❌ 靠人 |\n\n"
        "## P5~P9　我自己的工作过程缺陷（明细表，**不该被当成清单**）\n\n"
        "| 编号 | 现象 | 状态 |\n|---|---|---|\n"
        "| P5 | 只出现在明细表里（总览表没有） | 🔴 靠人 |\n"
        "| P8 | 只出现在明细表里（总览表没有） | 🔴 靠人 |\n"
        "| #33 | 明细表里重复出现的行 | 🟡 部分 |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(make_demo, "ROOT", tmp_path)
    monkeypatch.setattr(make_demo, "EVAL_DIR", tmp_path / "runs" / "_eval")
    monkeypatch.setattr(make_demo, "OUT", tmp_path / "demo" / "rca-demo.html")
    return tmp_path


# --------------------------------------------------------------------------- #
# 一、选哪一次运行（D13 时真错过一次：展示成了 2 次尝试的冒烟）
# --------------------------------------------------------------------------- #
def test_demo_picks_the_most_complete_run_not_the_newest_name(fake_root: Path) -> None:
    """按**尝试数**取，不能按文件名取最后一个。

    回归的是真实事件：`--judge` 冒烟跑只跑 2 个场景，文件名却更靠后，
    页面上于是展示冒烟数据，而不是 21 次尝试的正式 baseline。
    """
    smoke = [_attempt("F1", 1), _attempt("F1", 2)]
    full = [_attempt("F1", r) for r in (1, 2, 3)] + [_attempt("F3", r) for r in (1, 2, 3)]
    _write_archive(fake_root, "baseline-20260925-120000", _archive("2026-09-25T12:00:00+08:00", smoke))
    _write_archive(fake_root, "baseline-20260925-090000", _archive("2026-09-25T09:00:00+08:00", full))

    got = make_demo.latest("baseline-*/results.json")

    assert got is not None
    assert got.parent.name == "baseline-20260925-090000", (
        f"挑到了 {got.parent.name} —— 按文件名取最后一个就会挑到冒烟那次"
    )


def test_demo_latest_survives_a_broken_archive(fake_root: Path) -> None:
    """坏档案不能把整页生成搞崩，只是**不参与**挑选。"""
    d = fake_root / "runs" / "_eval" / "baseline-20260925-130000"
    d.mkdir(parents=True)
    (d / "results.json").write_text("{ 这不是 json", encoding="utf-8")
    _write_archive(fake_root, "baseline-20260925-090000", _archive("2026-09-25T09:00:00+08:00", [_attempt("F1", 1)]))

    got = make_demo.latest("baseline-*/results.json")

    assert got is not None and got.parent.name == "baseline-20260925-090000"


# --------------------------------------------------------------------------- #
# 二、计价时段（#37 的空白格）
# --------------------------------------------------------------------------- #
def test_demo_recomputes_the_tariff_when_the_archive_has_no_field(fake_root: Path) -> None:
    """存档没记 `pricing_tier` 时要**按 started_at 重算**，并标明是重算。

    2026-09-25 是中秋（法定节假日）—— 11:23 落在"工作日峰时"的钟点上，
    但节假日**全天**按谷时计费。这正是 #31 的教训，也是这次重算要照顾的点。
    """
    at = "2026-09-25T11:23:29+08:00"
    _write_archive(fake_root, "baseline-20260925-112329", _archive(at, [_attempt("F8", 1)]))
    _write_archive(fake_root, "multi-20260925-112329", _archive(at, [_attempt("F8", 1)]))

    page = make_demo.build()

    assert "baseline <code></code>" not in page, "计价时段又变成空白格了（#37）"
    assert "<code>off_peak</code>（按开始时刻重算）" in page
    assert "<code>2026-09-25T11:23:29+08:00</code>" in page, "起跑时刻要露出来，读者才能自己核对"


def test_demo_recomputed_tariff_is_peak_on_a_working_day(fake_root: Path) -> None:
    """非节假日的周四上午 10 点 → 重算结果必须是峰值（否则重算就是假的兜底）。"""
    at = "2026-09-24T10:00:00+08:00"
    _write_archive(fake_root, "baseline-20260924-100000", _archive(at, [_attempt("F1", 1)]))
    _write_archive(fake_root, "multi-20260924-100000", _archive(at, [_attempt("F1", 1)]))

    page = make_demo.build()

    assert "<code>peak</code>（按开始时刻重算）" in page


def test_demo_uses_the_recorded_tariff_when_it_exists(fake_root: Path) -> None:
    """有存档记录时以存档为准，并标成"存档记录"（重算不能覆盖实测）。"""
    at = "2026-09-24T10:00:00+08:00"  # 按重算会是 peak
    _write_archive(fake_root, "baseline-20260924-100000", _archive(at, [_attempt("F1", 1)], tier="off_peak"))
    _write_archive(fake_root, "multi-20260924-100000", _archive(at, [_attempt("F1", 1)], tier="off_peak"))

    page = make_demo.build()

    assert "<code>off_peak</code>（存档记录）" in page
    assert "按开始时刻重算" not in page


# --------------------------------------------------------------------------- #
# 三、样本口径与故障描述：必须由存档推出
# --------------------------------------------------------------------------- #
def test_demo_sample_text_is_derived_from_the_archive(fake_root: Path) -> None:
    base = [_attempt("F1", 1), _attempt("F1", 2), _attempt("F3", 1), _attempt("F3", 2)]
    multi = [_attempt("F3", 1), _attempt("F5", 1)]
    _write_archive(fake_root, "baseline-20260925-090000", _archive("2026-09-25T09:00:00+08:00", base))
    _write_archive(fake_root, "multi-20260925-090000", _archive("2026-09-25T09:00:00+08:00", multi))

    page = make_demo.build()

    assert "4 次尝试（2 场景 × 2 轮）" in page, "baseline 的样本口径没从存档推出来"
    assert "2 次尝试（F3/F5 × 1 轮）" in page, "multi 的样本口径没从存档推出来"


def test_demo_does_not_apply_another_faults_note(fake_root: Path) -> None:
    """挑中的场景没有描述时宁可不说 —— 不能拿 F8 的说明去描述 F3。

    （F8 的说明里有"内存泄漏"；若写死成 F8 那句，页面就会替一次 F3 的诊断
     作出它没作过的结论。）
    """
    _write_archive(fake_root, "baseline-20260925-090000", _archive("2026-09-25T09:00:00+08:00", [_attempt("F3", 1)]))
    _write_archive(fake_root, "multi-20260925-090000", _archive("2026-09-25T09:00:00+08:00", [_attempt("F3", 1)]))

    page = make_demo.build()

    assert "内存泄漏" not in page, "把 F8 的描述安到了别的故障上"
    assert "场景 <code>F3</code>" in page


def test_demo_prefers_a_multi_fault_scenario_for_the_trace(fake_root: Path) -> None:
    """有 F8 就展示 F8（分工的价值与失效都在那里），没有才退。"""
    _write_archive(fake_root, "baseline-20260925-090000", _archive("2026-09-25T09:00:00+08:00", [_attempt("F1", 1)]))
    _write_archive(
        fake_root,
        "multi-20260925-090000",
        _archive("2026-09-25T09:00:00+08:00", [_attempt("F1", 1), _attempt("F8", 1)]),
    )

    page = make_demo.build()

    assert "场景 <code>F8</code>" in page
    assert "多故障叠加" in page


def test_demo_ignores_an_unparseable_archive_without_started_at(fake_root: Path) -> None:
    """没有 started_at 也不能崩，且必须显式写"未记录"而不是留白（#37 的推广形式）。"""
    data = _archive("", [_attempt("F1", 1)])
    _write_archive(fake_root, "baseline-20260925-090000", data)
    _write_archive(fake_root, "multi-20260925-090000", data)

    page = make_demo.build()

    assert "<code>未记录</code>" in page
    assert "<code></code>" not in page, "页面上出现了空白单元格 —— 空白看起来像「没这回事」"


def test_demo_seal_table_ignores_other_tables_in_the_log(fake_root: Path) -> None:
    """只认总览表，且按编号去重。

    日志里**别的表也用编号开头**（`P5~P9` 那节的逐条表就是），所以：
      · 明细表里独有的 `P5`/`P8` **不许**进清单（离开"只扫总览小节"就会进来）
      · 总览表里的重复行 `#33` 只许出现一次（去掉去重就会变成两条）
    这两条各自都能单独变红，否则「只扫总览 + 去重」就只是没法验证的防御代码。
    """
    rows = make_demo.seal_table()

    assert [i for i, _d, _s in rows] == ["#1", "#33", "P9"], f"抽错了：{rows}"


# --------------------------------------------------------------------------- #
# 四、页面与文档必须对得上（"由存档生成"这句主张的守卫）
# --------------------------------------------------------------------------- #
def test_demo_seal_table_matches_the_harness_log() -> None:
    """页面上的"封堵清单（N 条）"必须等于 harness-log **总览表**的实际条数，
    而且**不许有重复编号**。

    这正是我每次审计都要**手工**做一遍的对账 —— 手工做的对账迟早会漏。
    事实上第一版页面就漏了：`seal_table()` 把 `P5~P9` 那节的**逐条表**也当成清单，
    于是 `P5`~`P9` 各出现两次，页面写"50 条"而真实是 45 条。
    """
    rows = make_demo.seal_table()
    ids = [i for i, _d, _s in rows]

    assert len(rows) >= 30, f"只抽到 {len(rows)} 条，harness-log 的表格规则可能变了"
    assert len(ids) == len(set(ids)), f"编号有重复：{[i for i in ids if ids.count(i) > 1]}"

    page = (ROOT / "demo" / "rca-demo.html").read_text(encoding="utf-8")
    if not page:
        pytest.skip("页面还没生成")
    assert f"封堵清单（{len(rows)} 条）" in page, (
        "页面上的条数和 harness-log 对不上 —— 改过日志后要重跑 scripts/make_demo.py"
    )
    missing = [i for i in ids if f"<code>{i}</code>" not in page]
    assert not missing, f"这些编号在页面上找不到：{missing}"


def test_demo_seal_table_reports_status_so_human_only_items_are_not_oversold() -> None:
    """**状态列必须如实带上**：靠人的条目不能被说成"变异测试证明过"。

    `P1`/`P2`/`P3` 在总览表里是"⚠️/❌ 靠人"，而页面原有那句话把整张表都说成了
    机器证明过的。这里守两件事：表里有靠人的条目、页面把这件事写出来。
    """
    rows = make_demo.seal_table()
    human = [i for i, _d, s in rows if "🟢" not in s]
    sealed = [i for i, _d, s in rows if "🟢" in s]

    assert sealed, "一条机器证明的都没有？解析规则肯定错了"
    assert human, "总览表里的靠人条目（P1/P2/P3）没被解出来 —— 状态列解析坏了"

    page = (ROOT / "demo" / "rca-demo.html").read_text(encoding="utf-8")
    if not page:
        pytest.skip("页面还没生成")
    assert f"剩下的 <strong>{len(human)}</strong> 条" in page, "页面没写清有多少条是靠人的"
    for i in human:
        assert f"<code>{i}</code>" in page


def test_committed_demo_page_is_reproducible_from_the_archives() -> None:
    """**"本页由存档生成"必须真的可复现。**

    页面里不含任何随时变的东西（没有生成时间戳，日期都来自存档），
    所以同一个存档重跑一次必须字节一致。不一致只有两种可能：
    页面是手改的，或者存档换了而页面没跟着重生成。
    """
    if not list((ROOT / "runs" / "_eval").glob("baseline-*/results.json")):
        pytest.skip("本机没有运行存档（runs/ 被 gitignore），无法重算页面")

    fresh = make_demo.build()
    committed = (ROOT / "demo" / "rca-demo.html").read_text(encoding="utf-8")

    assert fresh == committed, (
        "页面与存档不一致 —— 重跑 scripts/make_demo.py 再提交。"
        "（也说明「由存档生成」这句话暂时是假的）"
    )


# --------------------------------------------------------------------------- #
# 五、cost_breakdown：切段口径（#33 —— 它曾让我得出假的"输出 +77%"）
# --------------------------------------------------------------------------- #
def _rec(tag: str, out_tokens: int, *, hit: int, miss: int) -> dict:
    return {
        "tag": tag,
        "n_messages": 3,
        "response": {
            "usage": {
                "prompt_cache_hit_tokens": hit,
                "prompt_cache_miss_tokens": miss,
                "completion_tokens": out_tokens,
            }
        },
    }


def _recording(tmp_path: Path) -> Path:
    """两次运行**追加**在同一个文件里 —— 这正是 #33 的现场。

    一次运行的边界 = 它自己的 `coordinator` 调用（那是最后一个环节）。
    """
    run_a = [_rec("specialist:metrics", 100, hit=1000, miss=10), _rec("specialist:logs", 200, hit=2000, miss=20),
             _rec("coordinator", 300, hit=3000, miss=30)]
    run_b = [_rec("specialist:metrics", 5, hit=50, miss=5), _rec("coordinator", 7, hit=70, miss=7)]
    p = tmp_path / "record.ndjson"
    p.write_text(
        "\n".join(json.dumps(o, ensure_ascii=False) for o in run_a + run_b) + "\n",
        encoding="utf-8",
    )
    return p


def test_cost_breakdown_default_counts_only_the_last_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    p = _recording(tmp_path)
    monkeypatch.setattr(sys, "argv", ["cost_breakdown.py", str(p)])

    assert cost_breakdown.main() == 0
    out = capsys.readouterr().out

    assert "2 次调用" in out, "默认必须只统计最近一次运行（录制是追加的）"
    assert "输出：12 tok" in out, f"把两次运行混在一起了：\n{out}"
    assert "本文件累积了 2 次运行" in out, "必须把「混了多次运行」这件事说出来"


def test_cost_breakdown_all_flag_covers_the_whole_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    p = _recording(tmp_path)
    monkeypatch.setattr(sys, "argv", ["cost_breakdown.py", str(p), "--all"])

    assert cost_breakdown.main() == 0
    out = capsys.readouterr().out

    assert "5 次调用" in out
    assert "输出：612 tok" in out


def test_cost_breakdown_needs_a_path(monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["cost_breakdown.py"])
    assert cost_breakdown.main() == 2
    assert "用法" in capsys.readouterr().out
