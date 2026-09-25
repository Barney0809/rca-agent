"""文档本身的守卫：链接必须存在、每份文档必须进文档地图。

为什么要有这个文件：

    这个项目有 8 份 docs + README + AGENTS + demo 页面，**互相引用很多**。
    而"文档地图"是新会话/新代理进场的第一步（`AGENTS.md` 第 5 条），
    所以地图一旦漏项或链接失效，后果不是"少了个链接"，而是**新人找不到东西**
    ——#41 就是这么来的：README 里写着的东西没人去代码里对。

    这两条守卫都是**结构性**的、零成本、不需要网络：

      · 所有相对链接必须指向真实存在的文件；
      · `docs/` 下每一份文档都必须出现在 `AGENTS.md` 的文档地图里。

    它们**没有**对应的 harness-log 条目 —— 因为这不是"修一个已经发生的错误"，
    而是把一件手工检查过的事情（我每次加文档都手动确认链接可用）改成机器检查。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# 只扫仓库里"人写的"文档；跳过依赖目录与变异副本
SKIP_PARTS = (".venv", "rca-mutants", "__pycache__", "node_modules", "runs")

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)#\s]+)")


def _docs() -> list[Path]:
    out = []
    for p in ROOT.rglob("*.md"):
        # ⚠️ 必须比对**相对于项目根**的路径，不能比对绝对路径的每一段。
        #
        #    绝对路径在变异副本里是 `D:\...\rca-mutants\control-xxx\docs\...`，
        #    里面正好含有被跳过的名字 `rca-mutants` ⇒ **每一份文档都被跳过**，
        #    扫描结果为 0；而"扫到 0 个文档"在别处看起来仍然像"通过"。
        #
        #    这个坑 `tests/test_offline.py::_executable_scripts()` **早就用注释记过**，
        #    我在这里又犯了一次 —— 是变异检查的**控制组**当场抓出来的
        #    （它报的是那句话："the copy itself is broken"）。
        if any(part in p.relative_to(ROOT).parts for part in SKIP_PARTS):
            continue
        out.append(p)
    return sorted(out)


def test_all_relative_links_in_the_docs_resolve() -> None:
    """文档里的相对链接必须指向真实存在的文件。

    坏链接的代价不是"点不开"，而是**读者以为那份东西不存在**
    —— 在面试场景里，等于把一个已经做完的东西藏起来了。
    """
    broken: list[str] = []
    checked = 0

    for f in _docs():
        text = f.read_text(encoding="utf-8", errors="replace")
        for target in LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue  # 纯锚点
            checked += 1
            if not (f.parent / path_part).resolve().exists():
                broken.append(f"{f.relative_to(ROOT).as_posix()} → {target}")

    assert checked >= 20, f"只检查到 {checked} 个相对链接 —— 扫描规则可能坏了"
    assert not broken, "断链：\n  " + "\n  ".join(broken)


def _norm_commands(text: str) -> str:
    """把命令文本规范化后再比：去引号、反斜杠→/、逗号→空格、压空白、转小写。

    ⚠️ 为什么需要它：`scripts/ci.ps1` 里命令是**参数数组**写法
    （`"-m", "ruff", "check", "--select", "F821,ASYNC,F811", …`），
    而 workflow 里是一整行 shell 命令。逐字比对必然对不上，
    但"两边跑的是不是同一组检查"这件事仍然是可以机械核对的。
    """
    t = text.replace("\\", "/").replace('"', " ").replace("'", " ")
    t = t.replace(",", " ")
    return re.sub(r"\s+", " ", t).lower()


def test_the_ci_workflow_and_the_local_gate_agree_on_the_core_commands() -> None:
    """CI 与本地门禁**必须跑同一组命令** —— 否则"本地绿"就说明不了"云端会绿"。

    背景：本仓库**没有 git 远程**，所以 `.github/workflows/ci.yml` 从未在 runner 上跑过，
    能验证的只有 `scripts/ci.ps1`（本地实测 GATE PASSED）。既然"本地门禁"是唯一的
    证据来源，那它和 workflow 就必须是**同一组命令** —— 两处各写一份、慢慢走散，
    正是本项目 #43 记过的那个毛病（同一种知识写在两个地方）。

    ⚠️ 这条检查**不能**证明 workflow 在 Linux runner 上会通过（那需要真跑一次）。
       它能证明的是：**两边的检查项一致**。
    """
    raw_workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    raw_local = (ROOT / "scripts" / "ci.ps1").read_text(encoding="utf-8")
    workflow, local = _norm_commands(raw_workflow), _norm_commands(raw_local)

    core_commands = (
        "ruff check --select f821 async f811 src eval scripts tests world",
        "pytest",
        "mutate_check.py --verify-only",
        "seal_report.py --skip-mutations",
    )
    for cmd in core_commands:
        assert cmd in workflow, f"workflow 里少了这条门禁命令：{cmd}"
        assert cmd in local, f"本地门禁里少了这条命令：{cmd}"

    # workflow 必须把"离线"与"需要 Docker"分成两个作业 ——
    # 否则 Docker 一抖动就会把 Agent 代码的回归掩盖掉（注释里也是这么写的）
    assert "offline:" in raw_workflow and "world:" in raw_workflow
    assert "--ignore=tests/test_world.py" in raw_workflow, "离线作业必须排除需要 Docker 的用例"
    assert "--ignore=tests/test_world.py" in raw_local, "本地门禁也必须能排除它们（世界没起时）"


def test_the_local_gate_is_pure_ascii() -> None:
    """第 1 条守则：被解析的脚本必须纯 ASCII（Windows 上非 ASCII 会炸）。

    `scripts/ci.ps1` 是新加的、又是**入口脚本**（`make ci` 调它），
    所以这条得单独钉一下 —— 已有的 ASCII 守卫虽然覆盖 `*.ps1`，
    但那是"扫到才算"，这里明确点名，免得将来路径规则变动把它漏掉。
    """
    data = (ROOT / "scripts" / "ci.ps1").read_bytes()
    bad = [i for i, b in enumerate(data) if b > 0x7F]

    assert not bad, f"scripts/ci.ps1 里有非 ASCII 字节（第 {data[:bad[0]].count(chr(10).encode()) + 1} 行起）"


def test_every_doc_appears_in_the_agents_map() -> None:
    """`docs/` 下的每份文档都必须在 `AGENTS.md` 的文档地图里登记。

    地图是"新代理进场"的入口（`AGENTS.md` 第 5 条）。漏登一份文档，
    等于这份文档对新来的代理**不存在** —— 而它可能正是最该被读的那一份。
    """
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    docs = sorted(p.name for p in (ROOT / "docs").glob("*.md"))

    assert docs, "docs/ 下没有 md？路径规则可能错了"
    missing = [n for n in docs if n not in agents]

    assert not missing, (
        f"这些文档没登记进 AGENTS.md 的文档地图：{missing}\n"
        f"（登记一行就行 —— 目的是让新会话知道它存在）"
    )


def test_the_readme_points_at_the_overview_and_every_top_level_doc() -> None:
    """README 的文档地图必须覆盖 `docs/` 下的每一份文档。

    README 是面试官/陌生人的第一入口，它漏掉的文档在对方眼里就是没有的。
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    docs = sorted(p.name for p in (ROOT / "docs").glob("*.md"))

    missing = [n for n in docs if n not in readme]

    assert not missing, f"README 的文档地图里缺：{missing}"
    assert "docs/00-状态.md" in readme, "README 必须指到唯一权威那份状态文档"


@pytest.mark.parametrize("name", ["README.md", "docs/07-面试讲述稿.md", "docs/08-项目总览.md"])
def test_outward_docs_do_not_duplicate_counts(name: str) -> None:
    """对外文档不许复制计数（那是 `docs/00-状态.md` 的唯一职责）。

    项目开篇的原话：其他文档"不得复制进度、计数、完成状态" ——
    因为同一件事在几份文档里各有一份快照、互不可校验，
    最后连"测试到底有多少个"都说不清。
    """
    text = (ROOT / name).read_text(encoding="utf-8")

    for bad in ("190 passed", "193 passed", "66 个变异体", "29 组", "51 条封堵"):
        assert bad not in text, f"{name} 里出现了计数「{bad}」—— 计数只该写在 docs/00-状态.md"

def test_the_eval_path_still_uses_the_hand_written_loop() -> None:
    """**"评测仍走手写循环"这句话必须为真** —— 它保护的是"两侧可比"。

    ADR-0001 里的取舍：LangGraph 只接在**断点续跑**那条路上，评测路径保持手写循环，
    因为两侧必须同驱动才可比。这条不变量有两个方向，都要守：

      · 代码方向：`eval/runner.py` **不许**把驱动换成图（否则 D12/D15 的数字立刻不可比）；
      · 文档方向：README 必须**写明**这件事（否则读者会以为评测跑在 LangGraph 上）。

    ⚠️ 这条是替掉旧守卫的：原来的 `readme1` 变异体针对的是
    「README 把 LangGraph 画成已实现」—— 而 D16 之后 LangGraph **真的接线了**，
    那条不变量的**主题消失了**（守卫自动跳过、变异体随之过期）。
    与其留着一条空转的守卫，不如换成这条**现在还活着**的。
    """
    runner_src = (ROOT / "eval" / "runner.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "graph_loop" not in runner_src, (
        "评测路径换成了图驱动 —— 那 D12/D15 的定稿数字就与手写循环那一侧不可比了。\n"
        "若确实要换，必须两侧同时换并**重跑定稿**，同时更新文档里的口径说明。"
    )
    assert "评测数字仍出自手写循环" in readme, (
        "README 没有写明「评测仍走手写循环」—— 读者会以为评测跑在 LangGraph 上"
    )

def test_doc_scan_survives_a_project_path_that_looks_skippable(tmp_path, monkeypatch) -> None:
    """#52：**项目自己被放在一个"看起来该跳过"的目录下**时，文档扫描不能静默扫到 0 个。

    现场：我给这条守卫写 `_docs()` 时，用**绝对路径的每一段**去比对跳过目录
    （`if any(part in p.parts ...)`）。在本仓库里它"碰巧"没问题 ——
    但变异检查把项目复制到 `rca-mutants/...` 之后，**每一份文档都被跳过**，
    扫描结果为 0，控制组当场变红（"the copy itself is broken"）。

    ⇒ 这条用例把那种目录结构**在本地造出来**（路径里放 `runs/`），
       于是"绝对路径 vs 相对路径"这个差别不用等副本就能验。
    """
    td = __import__("tests.test_docs", fromlist=["_docs"])

    fake_root = tmp_path / "runs" / "rca-agent"      # ← 路径里含被跳过的名字
    (fake_root / "docs").mkdir(parents=True)
    (fake_root / "docs" / "a.md").write_text("没有链接", encoding="utf-8")
    (fake_root / "README.md").write_text("没有链接", encoding="utf-8")
    monkeypatch.setattr(td, "ROOT", fake_root)

    found = {p.name for p in td._docs()}

    assert found == {"a.md", "README.md"}, (
        f"扫描只找到 {found} —— 项目路径里出现「runs」这类名字时，整个扫描被静默跳过了"
    )
