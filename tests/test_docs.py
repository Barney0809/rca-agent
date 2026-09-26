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

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


# --------------------------------------------------------------------------- #
# #72：对外那一页的**说法**也必须被守（README 漂了四处，没人发现）
# --------------------------------------------------------------------------- #
#
# 现场（2026-09-27，收尾时"顺手"核了一下发现的）：
#   README 是对外第一页，而它的**六、诚实边界**那一节里有四处**已经过期**：
#     · "那 9.5 个百分点"——判据修过（#48）后是 **4.8**，而同一节上面的表格早已写成 95.2% vs 100%；
#     · "轮间波动是 ±7 个百分点"——#46 实测（按当前判据）是 **14.3 个百分点**；
#     · "**未测** deepseek-v4-pro"——其实做过一次小样本对照（两臂各 3 次，`max_steps=40`）；
#     · "**CI 从未在 GitHub runner 上跑过**（本仓库没有 git 远程）"——远程早就有，CI 已多次双绿。
#
# 为什么会漂：现有守卫只管"README 有没有列出 docs/ 下的每份文档"和"不许复制计数"，
# **没有任何守卫管 README 的断言是否过期**。
#
# ⚠️ 而且**不能**用"把这些过期字符串列成黑名单"的写法（那种守卫天然管不住下一次）——
#    下面两条都是**对账**：
#      ① README 的数字 ↔ **机器从存档生成的页面**（页面是唯一权威，README 是手写的）；
#      ② README 的说法 ↔ **权威文档里记着的事实**（事实在，则那句话必错）。
#    两侧任一改动都能让它变红（见变异组 `outward_doc_claims_do_not_drift`）。

BARE_PY_RE = re.compile(r"^(python3?|py)\b")


def _fenced_commands(text: str) -> list[tuple[int, str, str]]:
    """抽出 markdown fenced code block 里的**命令行**：`(行号, 命令, 它上面的注释)`。

    ⚠️ 四条排除/放行规则都是为了不误伤、也不留后门：
      · 空行、**有缩进的行**（缩进通常是被采集的**输出**，例如 `  python : D:\\...\\python.exe`）；
      · `#` 开头的注释行、提示符行（`>` / `PS>` / `$`）；
      · **反例放行**：命令**紧上面那条注释**里写了「反例」二字才放行 ——
        `AGENTS.md` 那条「错：裸 python」的示例必须能写出来，
        但放行是**显式标注**的（谁把标注删了，守卫就会拦住他自己写的反例）。
    """
    out: list[tuple[int, str, str]] = []
    inside = False
    last_comment = ""
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            inside = not inside
            last_comment = ""
            continue
        if not inside or not line.strip():
            continue
        if line.startswith((" ", "\t")):          # 缩进 = 输出，不是命令
            continue
        if line.lstrip().startswith(("#", ">", "$")):
            last_comment = line.lstrip()
            continue
        out.append((i, line.rstrip(), last_comment))
        last_comment = ""
    return out


def test_no_doc_shows_a_bare_python_command() -> None:
    """文档里的命令**不许用裸 `python`** —— 这是 P1 那个坑的文本面（#72）。

    为什么它值得一条守卫：本机 PATH 上的 `python` **不是**项目虚拟环境
    （实测指向 `...\\WindowsApps\\python.exe`），裸用会得到
    `ModuleNotFoundError: No module named 'rca'` —— 那看起来像"依赖没装"，
    而真相是"命令写错了"（AGENTS 第 1 条 P1）。

    ⇒ 陌生人照抄文档就会撞上这个假象，而**被骗的是读文档的人**，不是写文档的人。

    实测抓到 4 处（2026-09-27）：`AGENTS.md` 的反例（已显式标注放行）、
    `docs/00` 的「一次完整验证的最短路径」、`docs/10` 的三条 `guard_replay` 命令
    —— **其中 docs/00 那条最糟：它正是给陌生人照抄用的最短路径**。
    """
    allowed_prefixes = (".\\.venv\\Scripts\\python.exe", "./.venv/bin/python", "uv run python",
                        "uv run --", ".venv\\Scripts\\python.exe")
    docs = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md"))]

    checked = 0
    waived = 0
    bad: list[str] = []
    for p in docs:
        rel = p.relative_to(ROOT).as_posix()
        for n, cmd, comment in _fenced_commands(p.read_text(encoding="utf-8")):
            checked += 1
            if not BARE_PY_RE.match(cmd):
                continue
            if cmd.startswith(allowed_prefixes):
                continue
            if "反例" in comment:                 # 显式标注的反例，才放行
                waived += 1
                continue
            bad.append(f"{rel}:{n}: {cmd[:90]}")

    assert checked >= 40, f"只扫到 {checked} 条命令 —— 解析规则可能坏了（守卫会空转）"
    assert waived <= 3, (
        f"有 {waived} 条裸 python 走了「反例」放行 —— 放行口子被用得太宽，"
        f"它已经开始掩盖真实违规了（放行只该给 AGENTS 那条教学示例用）"
    )
    assert not bad, (
        "这些文档里的命令用了**裸 python**（P1）：\n  " + "\n  ".join(bad) + "\n"
        "⇒ 裸 `python` 会给出一个**错误的事实**（看起来像依赖没装），而读文档的人无从分辨。\n"
        "   改成 `.\\\\.venv\\\\Scripts\\\\python.exe <脚本>` 或 `uv run python <脚本>`。"
    )


def test_readme_numbers_agree_with_the_generated_page() -> None:
    """README 的对外数字必须与**机器从存档生成的页面**一致（#72）。

    页面（`demo/rca-demo.html`）由 `scripts/make_demo.py` 从归档算出、且逐字节可重放
    （`test_committed_demo_page_is_reproducible_from_the_archives` 守着这件事），
    所以它是对外数字的**唯一权威**；README 是手写的 ⇒ 手写的那个会漂。

    方向是**单向**的：页面上的数必须在 README 里出现。页面变了而 README 没跟着改 ——
    这正是 #72 的四处漂移里最典型的一类（判据修过、页面重算过，README 还是旧的）。
    """
    page = (ROOT / "demo" / "rca-demo.html").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    acc = re.search(r"<span class=\"num\">([\d.]+)%<span class='dim'>（按当前判据）", page)
    delta = re.search(r"准确率(?:反而)?<strong>(?:低了|高了) ([\d.]+) 个百分点</strong>", page)
    band = re.search(r"轮间波动 ([\d.]+) 个百分点之内", page)
    for label, m in (("准确率（按当前判据）", acc), ("准确率差", delta), ("轮间波动带", band)):
        assert m, f"页面上找不到机器算出来的『{label}』—— 模板变了？守卫不能退化成空绿"

    for label, m, suffix in (("准确率（按当前判据）", acc, "%"),
                             ("准确率差", delta, " 个百分点"),
                             ("轮间波动带", band, " 个百分点")):
        value = m.group(1) + suffix
        assert value in readme, (
            f"README 里没有出现页面上的{label} **{value}** —— "
            f"要么 README 过期了（#72：它漂过 9.5 / ±7 两个旧数），"
            f"要么页面重新生成后忘了同步 README"
        )

    # ⚠️ 两处**已被取代**的旧数：它们确实在 README 里公开发表过，所以额外地钉一下 ——
    #    这不是"过期字符串黑名单"那种写法（那种天然管不住下一次），
    #    它只是保证**这两个具体数字**不会再回来；真正通用的是上面那段与页面的对账。
    for retired in ("9.5 个百分点", "±7 个百分点"):
        assert retired not in readme, (
            f"README 里又出现了已被取代的旧数「{retired}」—— "
            f"判据修正（#48）与噪声带实测（#46）之后，正确的数在生成页面上"
        )


def test_readme_does_not_contradict_the_authority_docs() -> None:
    """README 的**说法**不许与权威文档里的事实矛盾（#72）。

    ⚠️ 这不是"过期字符串黑名单"：每一条都由**两半**组成 ——
        · `stale`：README 里**不许出现**的说法；
        · `evidence`：让那句话变错的**事实**，它必须能在权威文档里找到。
    若哪天事实不在了（比如 CI 作业被删），这条会先报"找不到证据"，
    **逼着人重新核对**，而不是继续假装 README 是错的或对的。

    README 是面试官/陌生人的第一入口：那里说过期的话，等于**把做过的事说成没做**，
    或者把没做的事说成做了 —— 两种都直接违背本项目的"诚实边界"那一节。
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    status = (ROOT / "docs" / "00-状态.md").read_text(encoding="utf-8")
    log = (ROOT / "docs" / "harness-log.md").read_text(encoding="utf-8")

    checks = [
        ("CI 从未在 GitHub runner 上跑过", "已在 GitHub Actions 上真跑过", status, "docs/00-状态.md"),
        ("本仓库没有 git 远程", "github.com/Barney0809/rca-agent", status, "docs/00-状态.md"),
        ("**未测** `deepseek-v4-pro`", "v4-pro @40", log, "docs/harness-log.md"),
    ]
    for stale, evidence, where_text, where_name in checks:
        assert evidence in where_text, (
            f"`{where_name}` 里找不到证据「{evidence}」—— 这条对账已过期，"
            f"请核对事实后更新本用例与 README（别让它变成一条永远为真的空话）"
        )
        assert stale not in readme, (
            f"README 里还写着「{stale}」，而 `{where_name}` 里的事实是「{evidence}」—— "
            f"README 说了过期的话（#72）"
        )

# --------------------------------------------------------------------------- #
# P11：封堵总览表的**行边界**必须由机器守着
# --------------------------------------------------------------------------- #
SEAL_ID_RE = re.compile(r"#\d+|P\d+")
STATUS_MARKS = "🟢🟡🔴⬜❌⚠️"


def _seal_tables() -> list[list[tuple[int, list[str]]]]:
    """`docs/harness-log.md` 总览小节里的**编号表**：每张表 = [(行号, 单元格), ...]。

    ⚠️ 三处刻意写细了：

    1. **只认"表头第一格是 `#`"的表。** 那个小节里还有别的表（`验收标准` / `项` 开头），
       它们不是封堵清单 —— 早期版本的解析器就是在这里把 `P5~P9` 数了两遍。
    2. **小节范围用 `make_demo.OVERVIEW_HEADING`**，不在这里再写一份标题字面量：
       页面生成器和守卫必须**看同一段**，否则两者可以各自"通过"却对不上（#43 那一类）。
    3. **带行号返回**：断言失败时只能说"某一行的列数不对"是没用的 ——
       报告里必须能直接跳到那一行。
    """
    from scripts import make_demo  # 延迟 import：ROOT 进 sys.path 是上面做的事

    lines = (ROOT / "docs" / "harness-log.md").read_text(encoding="utf-8").splitlines()
    start = next(
        i for i, ln in enumerate(lines)
        if ln.startswith("##") and make_demo.OVERVIEW_HEADING in ln
    )
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    section = lines[start:end]

    def cells(raw: str) -> list[str]:
        return [c.strip() for c in raw.strip().strip("|").split("|")]

    tables: list[list[tuple[int, list[str]]]] = []
    i = 0
    while i < len(section):
        if not section[i].startswith("|"):
            i += 1
            continue
        block: list[tuple[int, list[str]]] = []
        while i < len(section) and section[i].startswith("|"):
            # 行号按**原文件**算（+1 转成 1 起），这样报出来的行号能直接跳过去
            block.append((start + i + 1, cells(section[i])))
            i += 1
        if block and block[0][1][0].replace("*", "").strip() == "#":
            tables.append(block)
    return tables


def seal_table_problems(
    tables: list[list[tuple[int, list[str]]]],
    covered: set[str],
) -> list[str]:
    """五条不变量的**纯函数**版本：返回问题清单（空 = 表是健康的）。

    抽成纯函数只为一件事：**能用一张合成的小表当场证明每条检查都有牙齿**。
    真表里的编号随开发而变，靠真数据来证明"某条兜底是活的"是靠不住的 ——
    一旦真实数据里恰好没有那种形状，那条检查就退化成"永远通过"，
    而"永远通过"和"没有检查"在结果上完全一样（#47 记过这个坑）。
    """
    problems: list[str] = []
    ids: list[str] = []

    for block in tables:
        width = len(block[0][1])
        for line_no, row in block:
            if len(row) != width:
                problems.append(
                    f"L{line_no}: 有 {len(row)} 个单元格，而表头是 {width} 个 —— 两行被粘起来了？"
                )
        for line_no, row in block[2:]:          # 跳过表头与分隔行
            head = row[0].replace("*", "").strip()
            if not SEAL_ID_RE.fullmatch(head):
                problems.append(f"L{line_no}: 第一格不是编号，而是 {head[:28]!r} —— 编号被吃掉了？")
                continue
            if not any(m in row[-1] for m in STATUS_MARKS):
                problems.append(
                    f"L{line_no}（{head}）: 最后一格没有状态标记：{row[-1][:28]!r} —— 行尾被吃掉了？"
                )
            ids.append(head)

    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        problems.append(f"编号重复：{dup}（一行被复制了？）")

    for prefix, label in (("#", "#N 组"), ("P", "PN 组")):
        nums = sorted(int(i[1:]) for i in ids if i.startswith(prefix))
        if not nums:
            continue                            # 该组一条都没有：由调用方负责断言非空
        gaps = [n for n in range(1, nums[-1] + 1) if n not in nums]
        if gaps:
            problems.append(f"{label} 从 1 到 {nums[-1]} 缺了 {gaps} —— 总览表少了一行（丢行会断号）")

    missing = sorted(covered - set(ids))
    if missing:
        problems.append(
            f"这些编号有变异组背书，却在总览表里找不到行：{missing}"
            "（尾部丢行不会断号，只有这条交叉核对能抓到）"
        )
    return problems


def test_the_seal_overview_table_cannot_silently_lose_a_row() -> None:
    """P11：总览表"少了一行 / 粘了一行"必须变红，而不是等我自己看出来。

    现场（**六次**）：我给总览表追加或改写一行时，习惯把 `old_string` 锚在
    **某一行的开头**（`| **#68** | …`），于是替换把**上一行的尾巴**和这一行
    的头粘在了一起 —— 表里少一行、那一行多出几列，而：

      · Markdown 渲染出来只是"有点怪"，不报错；
      · `pytest` 一句话都不说；
      · `seal_report.py` 只按编号对账，**粘行不改变编号集合时它照样绿**。

    六次全靠我事后用脚本肉眼核对。这是 P5/P10 的同族：**为了省事绕开了正常工具**
    （P5 是手拼带引号的字符串，P10 是让 shell 往返改写文件，这里是锚错行）。

    ⇒ 把"表格还完整吗"变成五条可执行的不变量（实现见 `seal_table_problems`）：

      1. 每张编号表里所有行的**单元格数一致**（粘行会多出列）；
      2. 每个数据行的**第一格必须是编号**（粘行会吃掉编号）；
      3. 最后一格必须带**状态标记**（行尾被吃掉会露出来）；
      4. 编号**不重复**、`#`/`P` 两组**从 1 连续**（丢行会断号）；
      5. 有变异组背书的编号**必须在表里有行** —— 这条挡"丢掉编号最大那一行"：
         此时 1..max 仍然连续，断号检查看不见。

    本次运行**故意把真表改坏过两次**（见 `mutations.json` 的
    `seal_table_row_integrity`）：`sealtable-a-drop-a-row-boundary` 把分隔行和第一行
    粘起来、`sealtable-b-delete-the-last-row` 直接删掉表的最后一行 —— 两条都当场变红。
    """
    tables = _seal_tables()
    spec = json.loads((ROOT / "scripts" / "mutations.json").read_text(encoding="utf-8"))
    covered = {i for g in spec.values() for i in g.get("harness_log", [])}

    problems = seal_table_problems(tables, covered)
    assert not problems, (
        "封堵总览表的结构坏了：\n  " + "\n  ".join(problems) + "\n"
        "⇒ 这种损坏不会让任何别的测试报错，只能靠这条守卫 —— 修好行边界再提交"
    )

    # ---- 防空绿：守卫自己必须先证明"它确实看到了东西" -------------------- #
    assert len(tables) >= 2, (
        f"只认出 {len(tables)} 张编号表 —— 小节结构变了，这条守卫正在**空转**"
    )
    ids = [row[0].replace("*", "").strip() for block in tables for _n, row in block[2:]]
    assert len(ids) >= 60, f"只解析出 {len(ids)} 行 —— 解析规则可能坏了"
    assert {"#", "P"} <= {i[0] for i in ids}, f"`#` 与 `P` 两组必须都有，实际 {sorted(set(i[0] for i in ids))}"
    assert len(covered) >= 50, f"变异定义里只读到 {len(covered)} 个编号 —— 交叉核对在空转"


def test_the_seal_table_guard_sees_a_lost_tail_row() -> None:
    """**合成表**证明第 5 条不变量（交叉核对）是活的 —— 它专门管尾部丢行。

    为什么不用真表证明：真表里"哪个编号恰好有变异组"随开发而变，
    而这条兜底的价值恰好在**编号最大那一行被删掉**时体现（此时 1..max 仍然连续，
    断号检查看不见）。合成表把这个形状钉死，不依赖真实数据。
    """
    def row(n: int) -> tuple[int, list[str]]:
        return (n, [f"**#{n}**", "某个错误", "✅", "✅", "✅", "🟢 **已封堵**"])

    header = (1, ["#", "错误", "代码已修", "回归用例", "证明能变红", "**是否封堵**"])
    sep = (2, ["---"] * 6)
    healthy = [[header, sep, row(1), row(2)]]
    covered = {"#1", "#2"}

    assert seal_table_problems(healthy, covered) == [], "健康的小表被误判了 —— 守卫会误伤真表"

    lost_tail = [[header, sep, row(1)]]          # 尾部那一行没了
    problems = seal_table_problems(lost_tail, covered)

    assert problems, "尾部丢了一行，守卫却说没问题 —— 那条交叉核对是死的"
    assert any("#2" in p for p in problems), f"报的问题没点到丢掉的编号上：{problems}"

    # 顺带钉住另外两条：粘行（多出列）与行尾被吃掉（末格没有状态标记）
    glued = [[header, (2, ["---"] * 6 + ["**#1**"]), row(2)]]
    assert seal_table_problems(glued, covered), "粘行没被抓到"
    eaten_tail = [[header, sep, (3, ["**#1**", "某个错误", "✅", "✅", "✅", "（空）"]), row(2)]]
    assert seal_table_problems(eaten_tail, covered), "末格没有状态标记没被抓到"
