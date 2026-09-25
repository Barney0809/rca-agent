"""封堵清单：把"声称已封堵"变成**机器可校验**的东西。

================================= 为什么需要它 =================================

本项目的规则是「**一条错误只有在回归用例能变红之后，才算封堵**」。
`docs/harness-log.md` 的总览表逐条声明了封堵状态。

但**声明和一个可执行检查是两件事**：
表格里写"🟢 已封堵"是**人的断言**，而断言会过期 ——
代码改了、用例被删了、变异组失效了，表格不会自己变红。

所以这个脚本做一件事：**把表格里的断言和"变异到底红不红"对上**。

================================= 判定语义（很重要，别简化）=================================

对每一组变异，跑一遍（先对照组，再逐个变异体）：

    变异组 **NOT SEALED**  →  该条目**不许**声明 🟢 已封堵     （不一致 ⇒ 失败）
    变异组 SEALED          →  与任何声明都一致
                              （🟡/🔴 可能是在说"机制本身有已知弱点"，
                               那是变异测试**证明不了**的东西，不能反过来否定它）

还有第三类，也是这个脚本最有用的输出：

    **声明了 🟢，却没有任何变异组**  →  ⚠️ 一条**没有可执行证据**的封堵声明

这一类不是失败，但它必须被**列出来**：
它意味着那条"已封堵"目前只靠人的记忆维持 —— 而 #1 已经证明"靠人记得"会失效。

================================= 用法 =================================

    # 跑全部变异组并核对表格（慢，几分钟）
    .\\.venv\\Scripts\\python.exe scripts\\seal_report.py

    # 只看"声明有没有证据"，不跑变异（快，秒级）
    .\\.venv\\Scripts\\python.exe scripts\\seal_report.py --skip-mutations

退出码：0 = 全部一致；非 0 = 有不一致（声称已封堵但变异没能变红）。

⚠️ 它**不重复** `docs/harness-log.md` 的状态表（那会违反"一个领域只有一个权威"）。
   它只做校验与对账，权威仍然是 `docs/harness-log.md`。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "scripts" / "mutations.json"
LOG_PATH = ROOT / "docs" / "harness-log.md"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# 总览表里"封堵"那一列的标记。
# ⚠️ 用标记而不是文字匹配：表格里同一行还有"代码已修 ✅ / 用例 ✅"，
#    按文字匹配会把它们混进来。
SEALED_MARK = "🟢"
PARTIAL_MARK = "🟡"
OPEN_MARK = "🔴"

# 一行：| **#14** | 描述 | ... | 状态 |
ROW_RE = re.compile(r"^\|\s*\*{0,2}(#?\d+|P\d+)\*{0,2}\s*\|")


def declared_statuses() -> dict[str, str]:
    """从 `docs/harness-log.md` 的表格里读出**声明的**封堵状态。

    只解析行首是编号（`#14` / `P4`）且带状态的表格行。
    """
    text = LOG_PATH.read_text(encoding="utf-8")
    out: dict[str, str] = {}

    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue

        head = cells[0].replace("*", "").strip()
        if not re.fullmatch(r"#?\d+|P\d+", head):
            continue

        # 状态是最后一个非空单元格
        joined = " ".join(cells)
        if SEALED_MARK in joined and "**已封堵**" in joined.replace(" ", ""):
            status = SEALED_MARK
        elif SEALED_MARK in joined:
            status = SEALED_MARK
        elif PARTIAL_MARK in joined:
            status = PARTIAL_MARK
        elif OPEN_MARK in joined:
            status = OPEN_MARK
        else:
            continue

        key = head if head.startswith("#") else head
        # 同一编号可能出现两次（总览表 + P 表），取更"差"的那个更保守
        prev = out.get(key)
        if prev is None or _rank(status) < _rank(prev):
            out[key] = status

    return out


def _rank(mark: str) -> int:
    """🟢 > 🟡 > 🔴 —— 取最小（最保守）的那个。"""
    return {SEALED_MARK: 2, PARTIAL_MARK: 1, OPEN_MARK: 0}.get(mark, 0)


def main() -> int:
    ap = argparse.ArgumentParser(description="封堵清单对账")
    ap.add_argument("--skip-mutations", action="store_true",
                    help="不跑变异，只检查「声明有没有可执行证据」")
    args = ap.parse_args()

    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    declared = declared_statuses()

    print("=" * 92)
    print("  封堵清单对账")
    print("=" * 92)
    print(f"  harness-log 里读到的条目：{len(declared)} 条")
    print(f"  mutations.json 里的变异组：{len(spec)} 组，"
          f"共 {sum(len(g['mutations']) for g in spec.values())} 个变异体")
    print()

    # ---------- 1. 有变异组的条目：跑一遍，核对声明 ----------
    results: dict[str, bool] = {}
    if not args.skip_mutations:
        sys.path.insert(0, str(ROOT / "scripts"))
        from mutate_check import check_group, load_spec  # noqa: PLC0415

        full_spec = load_spec()
        for group, group_spec in full_spec.items():
            results[group] = check_group(group, group_spec)

    covered: dict[str, list[str]] = {}
    for group, g in spec.items():
        for item in g.get("harness_log", []):
            covered.setdefault(item, []).append(group)

    problems: list[str] = []
    broken: list[str] = []
    if not args.skip_mutations:
        broken = [g for g, ok in results.items() if not ok]

    print("── 有变异组背书的条目 ──")
    print(f"  {'条目':<6} {'声明':<4} {'变异组':<38} {'变异结果':<12} 一致?")
    print("  " + "-" * 88)
    for item in sorted(covered, key=lambda s: (s[0] != "#", s)):
        groups = covered[item]
        mark = declared.get(item, "?")
        if args.skip_mutations:
            cells = "  ".join(f"{g}(未跑)" for g in groups)
            verdict = "—"
        else:
            ok = all(results.get(g, False) for g in groups)
            cells = "  ".join(f"{g}:{'SEALED' if results.get(g) else 'NOT'}" for g in groups)
            verdict = "✅" if (ok or mark != SEALED_MARK) else "❌"
            if mark == SEALED_MARK and not ok:
                problems.append(
                    f"{item} 声明为已封堵，但变异组未能变红：{groups}"
                )
        print(f"  {item:<6} {mark:<4} {cells:<38} {'':<12} {verdict}")

    # ---------- 2. 没跑通的变异组（不许被淹没在表格里）----------
    if not args.skip_mutations:
        print()
        print("── ❌/🟡 没跑通的变异组（这些条目目前**无法被完整校验**）──")
        if not broken:
            print("  （无 —— 所有变异组都 SEALED）")
        else:
            for g in broken:
                items = spec[g].get("harness_log", [])
                print(f"  {g}  →  {items}")
            print()
            print("  ⇒ 可能原因有两种，必须分开看：")
            print("     · STALE（变异定义过期）→ 修 mutations.json 的查找串")
            print("     · NOT SEALED（用例抓不住）→ 补或改用例，这是真的缺口")

    # ---------- 3. 声明了已封堵、却没有任何变异组的条目 ----------
    print()
    print("── ⚠️ 没有可执行证据的'已封堵'声明（只靠人的记忆维持）──")
    unbacked = [
        item for item, mark in sorted(declared.items())
        if mark == SEALED_MARK and item not in covered
    ]
    if not unbacked:
        print("  （无 —— 每一条'已封堵'都有变异组背书）")
    else:
        for item in unbacked:
            print(f"  {item}")
        print()
        print("  ⇒ 这些条目**不是**错误，但它们目前无法被机器校验。")
        print("     要么补一个变异组，要么把声明降级为 🟡（如实标注'靠人'）。")

    # ---------- 4. 结论 ----------
    print()
    print("=" * 92)
    if problems:
        print("结论：❌ 有声明与变异结果不一致：")
        for p in problems:
            print(f"     - {p}")
    else:
        print("结论：✅ 所有'已封堵'声明都与变异结果一致。")
    if unbacked:
        print(f"      另有 {len(unbacked)} 条'已封堵'没有变异组背书（见上）。")
    print("=" * 92)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
