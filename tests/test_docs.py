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
        if any(part in p.parts for part in SKIP_PARTS):
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
