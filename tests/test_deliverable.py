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
def test_the_demo_page_reports_both_scoring_calibers() -> None:
    """#48：页面必须**同时**给出「存档判定」与「按当前判据重算」的准确率。

    为什么值得锁住：存档里的 `correct` 是**当时那版判据**给的，而判据后来修过
    （关键词匹配漏了英文词形）。只显示一个都会误导：

      · 只给存档值 → 拿一个**已知有缺陷**的判定当结论；
      · 只给重算值 → 抹掉"存档当时是什么样"，也就丢掉了可追溯性。

    这条同时把两个具体数字钉住：那份存档当时是 19/21，按今天的判据是 20/21。
    """
    import json

    from scripts import make_demo

    p = ROOT / "runs" / "_eval" / "multi-20260925-131953" / "results.json"
    if not p.exists():
        pytest.skip("本机没有那份存档（runs/ 被 gitignore）")
    data = json.loads(p.read_text(encoding="utf-8"))

    stored = sum(1 for a in data["attempts"] if a["correct"])
    now = make_demo.rescore_acc(data)

    assert stored == 19, f"存档里的判定数变了：{stored}（这份存档是证据，不该被改写）"
    assert now is not None, "重算返回 None —— 那两个口径就只剩一个了"
    assert round(now * len(data["attempts"])) == 20, (
        f"按当前判据应当是 20/21，实际 {now * len(data['attempts']):.0f}/21"
    )


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


def test_evidence_paths_are_derived_from_the_gitignore_negations() -> None:
    """"哪份运行产物算证据"只能有**一个定义处**：`.gitignore` 的反选规则。

    变异检查工具要按这个清单把证据一起拷进副本 —— 否则副本不再是项目的忠实拷贝，
    控制组会因为「存档缺失」变红（#38 的现场：工具自己报 "the copy itself is broken"）。

    这里同时锁住那条容易写错的分支：**只反选父目录的规则要被跳过**
    （`!runs/_eval/` 是 `!runs/_eval/<某次运行>/` 的前缀，照抄会把整个 runs/ 又拷一遍）。
    """
    from scripts.mutate_check import evidence_paths

    got = evidence_paths()

    assert "runs/_eval/" not in got, "父目录级的反选被当成证据了（会把整个 runs/ 拷一份）"
    assert "runs/_eval/baseline-20260925-085629/" in got
    assert "runs/_eval/multi-20260925-112648/" in got
    assert "runs/_eval/multi-20260925-131953/" in got
    assert "runs/_recordings/record-deepseek-flash.ndjson" in got


def test_frozen_evidence_is_present_and_not_ignored() -> None:
    """**定稿数字的证据必须随仓库提交**（AC-12：陌生人克隆要能自己核对）。

    #38：`runs/` 整目录被 gitignore，于是"本页由存档生成"只在我这台机器上成立 ——
    陌生人克隆后生成器直接报"找不到存档"，而上面那条"可逐字节重放"的用例
    只会 `pytest.skip`（**skip 也是绿**，假绿）。

    这里守两个不变量：证据在磁盘上、且 `.gitignore` 里**显式反选**了它们。
    （只需读文件、不用调 git —— 与仓库里其它"离线结构守卫"同一路子。）
    """
    for name in ("baseline-20260925-085629", "multi-20260925-112648",
                 "multi-20260925-131953"):
        p = ROOT / "runs" / "_eval" / name / "results.json"
        assert p.exists(), f"定稿证据缺失：{p}（数字就没有可核对的来源了）"

    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in ignored.splitlines()]

    # 父目录整体被排除时 git 不会再看反选规则 ⇒ 必须是 `runs/*` 而不是 `runs/`
    assert "runs/*" in lines, "`runs/` 写成了整目录排除，下面的反选会失效"
    assert "runs/" not in lines, "`runs/` 会把证据一起排除掉"
    for name in ("baseline-20260925-085629", "multi-20260925-112648",
                 "multi-20260925-131953"):
        assert f"!runs/_eval/{name}/" in lines, f"{name} 的证据没有被反选进仓库"
    assert "!runs/_recordings/record-deepseek-flash.ndjson" in lines, "录制也没进仓库"

    # 反选必须排在排除规则**之后**（gitignore 是后者优先）
    assert lines.index("runs/*") < lines.index("!runs/_eval/"), "反选规则的位置不对"


UNBUILT_MARKERS = ("未接线", "未实现", "尚未", "计划中", "没有做", "没真用", "未做")


def test_outward_facing_docs_mark_unbuilt_layers_instead_of_presenting_them_as_built() -> None:
    """#41：**对外文档**（README / 总览 / 讲述稿）不许把"设计"写成"实现"。

    现场：架构图里写着 `Agent 层（LangGraph） │ MCP 策略执行点（独立进程）`，
    而代码里 **LangGraph 零命中、MCP 零命中、`rca.policy` 只被测试 import** ——
    三条一条都不成立。来源是我把 `docs/02` 的设计图抄进了 README，实现后来偏离了，
    而**同一个文档体系里"设计"与"实现"没有区分标记**。

    规则（可机械检查）：凡对外文档提到、而源码里确实没有的东西，
    它的**每一处出现**附近都必须带上"未接线/未实现"这类标记。
    这样"把设计写成实现"就变成了一条会变红的用例，而不是靠我下次记得。

    ⚠️ 只查对外文档：`docs/02` 是**设计文档**，它本来就有权写没做的东西；
    而 `docs/00-状态.md` 用另一种方式表达（「未接线」待办清单 + 当前事实）。
    """
    src = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in (ROOT / "src").rglob("*.py")
    )
    docs = ("README.md", "docs/08-项目总览.md", "docs/07-面试讲述稿.md", "docs/架构图.svg")

    for name in docs:
        path = ROOT / name
        assert path.exists(), f"对外文档不见了：{name}"
        readme = path.read_text(encoding="utf-8")
        lines = readme.splitlines()

        for label, needles in (("LangGraph", ("langgraph",)), ("MCP", ("import mcp", "from mcp"))):
            if any(n in src for n in needles):
                continue  # 代码里真的有，随便提
            hits = [i for i, ln in enumerate(lines) if label in ln]
            assert hits, f"{name} 里找不到 {label} 了？这条守卫的前提变了"
            for i in hits:
                window = "\n".join(lines[max(0, i - 2): i + 3])
                assert any(m in window for m in UNBUILT_MARKERS), (
                    f"{name} 第 {i + 1} 行提到了 {label}，但源码里没有它，"
                    f"附近又没标「未接线」：{lines[i].strip()}"
                )

        assert "独立进程" not in readme, (
            f"{name} 把策略执行点写成了独立进程 —— 它是 `src/rca/policy/` 里的"
            f"**进程内模块**（而且尚未接到 Agent 路径）（#41）"
        )


def test_readme_does_not_duplicate_progress_or_counts() -> None:
    """README 不复制进度/计数（那是 `docs/00-状态.md` 的唯一职责）。

    项目开篇的原话：其他文档"不得复制进度、计数、完成状态" ——
    因为同一件事在四份文档里各有一份快照、互不可校验，最后连测试有多少个都说不清。
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "docs/00-状态.md" in readme, "README 必须指到唯一权威那份状态文档"
    for bad in ("已封堵 3", "共 190", "190 条用例", "27 组"):
        assert bad not in readme, f"README 里出现了计数「{bad}」—— 计数只该写在 docs/00-状态.md"


def test_frozen_fingerprint_ignores_line_endings(tmp_path: Path) -> None:
    """#42：指纹只该守「**内容**变没变」，不该守「换行符是哪个平台的」。

    现场：`.gitattributes` 规定 `eol=lf` ⇒ 克隆出来是 LF，而作者的 Windows
    工作副本是 CRLF。第一版指纹直接 `sha256(read_bytes())`，
    于是 `test_baseline_module_is_frozen` 在**每一个干净克隆里都是红的**：

        expected: ba2fe0cb321bb615
        actual  : 5a5e5f1978330d11

    ⚠️ 一个在干净克隆里必红的守卫，和一个永远绿的守卫，结果一样：**没人再信它**。
    """
    from scripts.frozen_fingerprint import fingerprint

    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    changed = tmp_path / "changed.py"
    lf.write_bytes(b"a = 1\nb = 2\n")
    crlf.write_bytes(b"a = 1\r\nb = 2\r\n")
    changed.write_bytes(b"a = 1\nb = 3\n")

    assert fingerprint(lf) == fingerprint(crlf), "换行不同就算出不同指纹 —— 克隆里会误报「被改动了」"
    assert fingerprint(lf) != fingerprint(changed), "内容真的变了却算不出差别 —— 这个守卫就废了"


def test_demo_generator_writes_lf_so_a_fresh_clone_stays_clean(fake_root: Path) -> None:
    """生成器必须写 **LF**：#39。

    `.gitattributes` 写着 `* text=auto eol=lf`，而 Python 文本模式在 Windows 上
    默认写 CRLF ⇒ **每跑一次生成器，克隆里就多一次"假修改"**
    （`git status` 报 M、`git diff` 却是空的）。陌生人第一次克隆就会看到仓库"脏了"。

    ⚠️ 这一条是 **Windows 专属**的：在 Linux 上默认换行本来就是 `\\n`，
       所以对应的变异体在 Linux 上是空操作（本项目只在 Windows 上跑，见 AGENTS.md）。
    """
    at = "2026-09-25T09:00:00+08:00"
    for name in ("baseline-20260925-090000", "multi-20260925-090000"):
        _write_archive(fake_root, name, _archive(at, [_attempt("F1", 1)]))

    assert make_demo.main() == 0
    raw = (fake_root / "demo" / "rca-demo.html").read_bytes()

    assert b"\r\n" not in raw, "生成器写了 CRLF —— 克隆里会出现一次「假修改」"


def test_committed_demo_page_has_no_crlf() -> None:
    """提交进仓库的那份页面也必须是 LF（同上，#39）。"""
    if not (ROOT / "demo" / "rca-demo.html").exists():
        pytest.skip("页面还没生成")

    raw = (ROOT / "demo" / "rca-demo.html").read_bytes()

    assert b"\r\n" not in raw, "提交的页面里有 CRLF —— 与 .gitattributes 的 eol=lf 冲突"


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


# --------------------------------------------------------------------------- #
# 六、#40：文档引用的那一次运行必须能**指名道姓地取回来**
# --------------------------------------------------------------------------- #
def _recording3(tmp_path: Path) -> Path:
    """三次运行追加在一个文件里，每次的输出量都不同（便于断言取到的是哪一次）。"""
    runs = [
        [_rec("specialist:x", 100, hit=100, miss=10), _rec("specialist:y", 200, hit=200, miss=20),
         _rec("coordinator", 300, hit=300, miss=30)],                      # 第 1 次：输出 600
        [_rec("specialist:x", 5, hit=50, miss=5), _rec("coordinator", 7, hit=70, miss=7)],    # 第 2 次：12
        [_rec("specialist:x", 1, hit=10, miss=1), _rec("coordinator", 2, hit=20, miss=2)],    # 第 3 次：3
    ]
    p = tmp_path / "record3.ndjson"
    p.write_text("\n".join(json.dumps(o, ensure_ascii=False) for r in runs for o in r) + "\n",
                 encoding="utf-8")
    return p


def test_cost_breakdown_can_select_an_earlier_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    """`--run 1` 取回第 1 次运行（#40：文档里的构成数字指向**那一次**，不是"最近一次"）。

    ⚠️ 没有这个开关时，"文档说输出占 72.3%"是一句**复现不出来**的话：
       录制是追加的，文件一变，"最近一次"就不再是它了。
    """
    p = _recording3(tmp_path)
    monkeypatch.setattr(sys, "argv", ["cost_breakdown.py", str(p), "--run", "1"])

    assert cost_breakdown.main() == 0
    out = capsys.readouterr().out

    assert "3 次调用" in out, "没有取到第 1 次运行"
    assert "输出：600 tok" in out, f"取错运行了：\n{out}"
    assert "第 1 次运行（文件里共 3 次）" in out, "得说清当前统计的是哪一次"


def test_cost_breakdown_list_shows_every_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    """`--list`：一次看全每次运行的调用数与构成（陌生人该先用它）。"""
    p = _recording3(tmp_path)
    monkeypatch.setattr(sys, "argv", ["cost_breakdown.py", str(p), "--list"])

    assert cost_breakdown.main() == 0
    out = capsys.readouterr().out

    assert "共 3 次" in out
    for k in (1, 2, 3):
        assert f"第 {k:>2} 次" in out, f"第 {k} 次没列出来"


def test_cost_breakdown_rejects_an_unknown_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    """越界要**明确报错**，不能悄悄退化成"最近一次"（那是最坏的一种假绿）。"""
    p = _recording3(tmp_path)
    monkeypatch.setattr(sys, "argv", ["cost_breakdown.py", str(p), "--run", "9"])

    assert cost_breakdown.main() == 2
    assert "取不到第 9 次" in capsys.readouterr().out


def test_cost_breakdown_rejects_a_non_integer_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    p = _recording3(tmp_path)
    monkeypatch.setattr(sys, "argv", ["cost_breakdown.py", str(p), "--run", "abc"])

    assert cost_breakdown.main() == 2
    assert "整数" in capsys.readouterr().out
