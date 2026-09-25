"""变异检查：证明"回归用例真的能变红"。

================================ 为什么需要这个脚本 ================================

本项目的 Harness 纪律：

    一条错误，只有在**回归用例能变红之后**，才算封堵。

"我加了用例"和"用例能抓住这个错误"是两件事。
很多用例写出来就是常绿的 —— 它看起来在守护代码，实际上什么都守护不了。

这个脚本把"能变红"从**口头声明**变成**可执行证据**：
    它把缺陷**重新注入**一份代码副本，然后跑用例。
    用例必须变红，否则脚本报 `NOT SEALED` 并非零退出。

============================ 为什么是副本，而不是改真文件 ============================

改真文件再还原是常见做法，但本仓库第 0 条禁止删除任何东西，
而且"还原"依赖人记得还原干净 —— 我们已经被"靠人记得"坑过一次（见 harness-log #1）。

所以这里：**真实源码一个字节都不碰**。
每次变异都在 `<项目同级>/rca-mutants/<变异名>-<时间戳>/` 下建一份完整副本，
在副本里注入缺陷。副本本身也是证据留痕（第 0 条：不删）。
（为什么副本必须在仓库**外面**，见 `MUTANT_ROOT` 处的注释。）

============================ 对照组：脚本必须自证结果可信 ============================

只跑变异体是不够的：如果副本本身就是坏的（路径不对、依赖缺失），
用例也会"变红" —— 但那是**假红**，什么都证明不了。

所以脚本每次都先跑一次**对照组**：
    同样的副本、同样的用例、**不注入任何缺陷**，必须全绿。
对照组不绿 => 直接中止，不输出任何"已封堵"的结论。

同理，脚本还会自证**变异体确实被加载了**：
    在副本目录里 import rca，确认 `rca.__file__` 落在副本内。
否则（比如将来项目被 editable 安装，MetaPathFinder 优先于 pythonpath），
变异根本没生效 —— 脚本会得出虚假结论。
这种"脚本自己骗自己"的情况必须显式挡住。

============================ 控制台输出为什么仍然用纯 ASCII ============================

本仓库第 1 条（编码纪律）的立法理由是：
    PowerShell 按 GBK 读非 ASCII 就出事。

本脚本开发时正好又撞上这个机制：控制台打印 `✓` 直接抛了
`UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'`
—— 于是"检查封堵"这件事本身先崩了（见 harness-log #2 / #3 / P4）。

**真正的**修法是给 stdout 兜底（见文件里 `sys.stdout.reconfigure` 那段，
写法是实测出来的）。既然兜底已经有了，理论上就可以放心打印中文了。

那为什么这里还是坚持纯 ASCII？**纵深防御**：
    · 兜底是主防线，负责让中文和 emoji 都能正常输出；
    · 纯 ASCII 是第二道防线 —— 万一哪天兜底被绕过（比如异常被 try 吞掉、
      或者有人把这段删了），一个只输出 ASCII 的脚本**依然不可能因为输出而崩溃**。

对一个"负责给出可信结论"的工具来说，宁可输出朴素一点，也不能在最后一行崩掉 ——
它崩掉的时候，前面的检查其实已经跑完了，结论却一起没了。
（详细命令输出与证据仍然写进日志文件，那边是显式 UTF-8，中文一切正常。）

============================ 用法 ============================

    .\\.venv\\Scripts\\python.exe scripts\\mutate_check.py --group cross_exam_prompt_render
    .\\.venv\\Scripts\\python.exe scripts\\mutate_check.py --list

变异定义在 `scripts/mutations.json`：**数据而不是代码**，
这样 D9 的"封堵清单"可以直接遍历同一份文件。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "scripts" / "mutations.json"

# ⚠️ 变异副本**放在仓库之外**，这是踩过一次坑之后改的。
#
# 最初放在 `runs/_mutants/`（runs 是 gitignored 的，看起来正合适）。
# 结果对照组立刻变红：`tests/test_offline.py` 的 #3 编码检查用
# "路径里出现 runs 就跳过" 来排除目录，而副本的绝对路径里正好含有 `runs`，
# 于是它把副本里**所有文件**都跳过了，扫到 0 个脚本。
#
# 根因是那条目录匹配规则太粗（已改为按相对路径判断，见该测试的注释），
# 但更稳的做法是让副本根本不待在仓库里：
#   · 不会被任何"遍历仓库"的测试扫到（现在不会，将来也不会）；
#   · 不会和 git 状态、打包、静态检查互相干扰。
#
# 位置：与项目同级的 `rca-mutants/`（只新建，从不删除）。
MUTANT_ROOT = ROOT.parent / "rca-mutants"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

# 复制项目时排除的东西：要么太大（.venv），要么与本次检查无关（.git），
# 要么是被测代码自己会写的（runs / 各种缓存）。
COPY_IGNORE = shutil.ignore_patterns(
    ".venv",
    ".git",
    "runs",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
)

# 控制台输出兜底 —— 见模块 docstring 里"控制台输出为什么纯 ASCII"。
#
# 这里是**实测**出来的写法，不是推理出来的（本机 Windows，控制台代码页 936）：
#
#   stdout 接到管道/文件时，Python 会退回本地编码 GBK。此时：
#     · 完全不兜底          -> 打印 emoji 抛 UnicodeEncodeError，整段输出丢掉
#     · 只加 errors="replace" -> 不崩了，但输出仍是 GBK 字节，
#                                被按 UTF-8 读的地方（编辑器 / 上游采集）全是乱码
#     · encoding="utf-8" + errors="replace" -> 中文与 emoji 都正常 ✅
#
# 所以必须**同时**指定 encoding 和 errors。这也是仓库里其它 6 个脚本早就在用的写法。
#
# ⚠️ 只加 errors="replace" 是个很自然的误判 —— 本脚本第一版就是这么写的，
#    当时还配了一段"不要用 encoding=utf-8，那会让中文变乱码"的错误注释。
#    实测把这个论断直接推翻了。这也是为什么 `tests/test_offline.py` 的 P4 用例
#    会一路查到 `encoding=` 的取值上，而不是只查"有没有调用 reconfigure"。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class MutationNotApplicable(Exception):
    """变异定义与当前源码对不上了（查找串找不到 / 找到多处）。

    ⚠️ 这不是"用例没封堵"，而是"**这件事现在无法被校验**"。
    两者必须分开报：前者说明代码有问题，后者说明**变异定义过期了**。

    真实案例（2026-09-25）：重构 `judge()` 之后，
    `score-a-ignore-dismissal-context` 的查找串就再也匹配不到了 ——
    而在此之前它已经"SEALED"过好几轮。**一个过期的变异体会一直假装通过，**
    直到有人真的去跑它。这正是"封堵清单"要抓的东西。
    """


def load_spec() -> dict:
    if not SPEC_PATH.exists():
        sys.exit(f"找不到变异定义文件：{SPEC_PATH}")
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def make_copy(slug: str) -> Path:
    """把当前项目复制一份到 <项目同级>/rca-mutants/<slug>-<时间戳>/。

    只做**新建**，不删除任何既有副本（第 0 条）—— 副本本身就是证据留痕。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = MUTANT_ROOT / f"{slug}-{stamp}"
    shutil.copytree(ROOT, dest, ignore=COPY_IGNORE, dirs_exist_ok=True)
    # 被测代码会往 runs/ 写测试工作区，给它留个空目录
    (dest / "runs").mkdir(exist_ok=True)
    return dest


def run_pytest(copy_dir: Path, tests: list[str], log_path: Path) -> tuple[int, str]:
    """在副本目录里跑 pytest，输出落盘到 log_path（留痕）。

    刻意**不用管道**捕获输出：在受限沙箱下管道可能不可用，
    而且管道输出会随进程结束丢掉。落盘最稳，也方便事后翻证据。

    ⚠️ 必须显式设 `PYTHONIOENCODING=utf-8`。

    子进程默认按本机编码（GBK）写 stdout。我这边用 `encoding="utf-8"` 打开日志文件，
    于是写进去的其实是 GBK 字节 —— 文件名说 UTF-8，内容是 GBK，
    用编辑器打开就是一堆乱码，证据等于没留。
    这个坑和 harness-log #2 / #3 / P4 是同一个机制，第四次露面。
    """
    cmd = [
        str(VENV_PYTHON),
        "-m",
        "pytest",
        *tests,
        "-q",
        "-rf",
        "-p",
        "no:cacheprovider",
    ]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    with log_path.open("w", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(cmd) + "\n")
        fh.write(f"# cwd = {copy_dir}\n\n")
        fh.flush()
        proc = subprocess.run(
            cmd, cwd=copy_dir, stdout=fh, stderr=subprocess.STDOUT, env=env
        )
    return proc.returncode, log_path.read_text(encoding="utf-8", errors="replace")


def failed_tests(log_text: str) -> list[str]:
    """从 pytest 短摘要里抽出失败的用例 id。"""
    out: list[str] = []
    for line in log_text.splitlines():
        line = line.strip()
        if line.startswith("FAILED "):
            out.append(line[len("FAILED ") :].split(" - ")[0].strip())
    return out


def verify_mutant_src_is_loaded(copy_dir: Path) -> str:
    """自证前提：副本目录里 import 到的 rca 必须来自副本自己。

    如果这条不成立，变异体没被加载，整个检查的结论都是假的。
    """
    code = "import rca.agents.coordinator as c; print(c.__file__)"
    proc = subprocess.run(
        [str(VENV_PYTHON), "-c", code],
        cwd=copy_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(copy_dir / "src")},
    )
    lines = (proc.stdout or "").strip().splitlines()
    loaded_path = lines[-1] if lines else ""
    if not loaded_path:
        sys.exit(
            "无法确认副本里的 rca 是否被加载：\n"
            f"  stdout={proc.stdout!r}\n  stderr={proc.stderr!r}"
        )
    if copy_dir.resolve() not in Path(loaded_path).resolve().parents:
        # 这条分支一旦触发，说明"变异副本被加载"这个前提不成立。
        # 此时**不能**继续，否则会得出虚假结论。
        print("  [FAIL] mutated copy was NOT loaded; aborting before any verdict.")
        print(f"         copy   : {copy_dir}")
        print(f"         loaded : {loaded_path}")
        print("         likely cause: editable install puts a MetaPathFinder ahead of pythonpath.")
        sys.exit(2)
    return loaded_path



# ================================================================
# 秒级检查：变异定义**是否还适用于当前源码**
# ================================================================
#
# 为什么需要它（#25 已经三次咬人：变异体被重构弄过期，却一直"假装通过"）：
#
#   完整对账（`seal_report.py`）要建 24 次副本、跑 53 次 pytest —— **几分钟到十几分钟**。
#   太贵 ⇒ 没人会在每次改源码之后跑它 ⇒ 那条"改完就要对账"的纪律**注定失效**。
#
#   而"过期"这件事**根本不需要跑测试就能发现**：
#   只要看看每条变异的 `find` 串**在不在当前源码里、且只出现一次**。
#   那是**毫秒级**的纯文本检查，可以放进普通测试里 ——
#   于是**每一次跑测试都自动检查一遍**，而不是靠人记得。
#
# ⇒ 原则：**把贵的检查拆出一个便宜的近似版，让便宜的那个天天跑。**


def verify_spec_applies(spec: dict | None = None, root: Path | None = None) -> list[str]:
    """逐条检查变异定义是否还适用。返回问题清单（空 = 全部适用）。

    ⚠️ 只做**静态检查**，不建副本、不跑测试、不花时间。
       它能抓住的是"find 串找不到 / 找到多处"这一类过期；
       **抓不到**"变异还能套用、但用例已经抓不住它"（那仍需完整对账）。
    """
    spec = spec if spec is not None else load_spec()
    root = root or ROOT
    problems: list[str] = []

    for group, g in spec.items():
        if not g.get("harness_log"):
            problems.append(f"{group}: 没有标注 harness_log（对账时无法归属到条目）")
        for mut in g.get("mutations", []):
            target = root / mut["file"]
            if not target.exists():
                problems.append(f"{group}/{mut['id']}: 文件不存在 {mut['file']}")
                continue
            text = target.read_text(encoding="utf-8")
            n = text.count(mut["find"])
            if n != 1:
                problems.append(
                    f"{group}/{mut['id']}: 在 {mut['file']} 里找到 {n} 处匹配（期望 1）"
                    f" —— 变异定义已过期，需重新对准"
                )
    return problems

def apply_mutation(copy_dir: Path, mut: dict) -> None:
    target = copy_dir / mut["file"]
    text = target.read_text(encoding="utf-8")
    n = text.count(mut["find"])
    if n != 1:
        # ⚠️ 这里**抛异常**而不是 sys.exit：
        #    一个过期的变异体不该让整份对账中止 ——
        #    否则"清单跑不完"会被误读成"清单坏了"，
        #    而真实情况是"这一条现在无法被校验"。
        raise MutationNotApplicable(
            f"在 {mut['file']} 里找到 {n} 处匹配（期望恰好 1 处）。"
            "多半是源码改了，而这条变异定义没跟着更新。"
        )
    target.write_text(text.replace(mut["find"], mut["replace"]), encoding="utf-8")


def check_group(group_name: str, group: dict) -> bool:
    """跑一个变异组。返回 True 表示这一组全部封堵成功。"""
    tests = group["tests"]
    mutations = group["mutations"]

    print("=" * 72)
    print(f"mutation group : {group_name}")
    print(f"  why          : {group.get('why', '')}")
    print(f"  target tests : {', '.join(tests)}")
    print(f"  mutations    : {len(mutations)}")
    print("=" * 72)

    # ---------- 第 0 步：对照组（不注入缺陷，必须全绿） ----------
    print("")
    print("[control] copying project WITHOUT any mutation; tests must be ALL GREEN ...")
    control_dir = make_copy(f"control-{group_name}")
    loaded = verify_mutant_src_is_loaded(control_dir)
    print(f"          verified copy is the one being imported:")
    print(f"            {loaded}")
    rc, log = run_pytest(control_dir, tests, control_dir / "pytest.log")
    if rc != 0:
        print(f"  [FAIL] control run is RED (exit {rc}): {failed_tests(log)}")
        print("         the copy itself is broken, so no verdict about sealing is possible.")
        print(f"         evidence: {control_dir / 'pytest.log'}")
        return False
    print("  [OK]   control run is green -> copy is trustworthy")

    # ---------- 逐个变异 ----------
    all_sealed = True
    for mut in mutations:
        print("")
        print(f"[mutant] {mut['id']}")
        print(f"  {mut['desc']}")

        copy_dir = make_copy(mut["id"])
        verify_mutant_src_is_loaded(copy_dir)
        try:
            apply_mutation(copy_dir, mut)
        except MutationNotApplicable as exc:
            all_sealed = False
            print("  [STALE] 这条变异定义已经过期，无法应用 —— **这件事目前无法被校验**。")
            print(f"         {exc}")
            print(f"         定义：{SPEC_PATH.relative_to(ROOT)} 里的 {mut['id']}")
            continue

        rc, log = run_pytest(copy_dir, tests, copy_dir / "pytest.log")
        got = failed_tests(log)
        expect = mut.get("expect_red", [])

        if rc == 0:
            all_sealed = False
            print("  [FAIL] NOT SEALED -- defect injected, yet every test stayed green.")
            print("         this error is NOT sealed: current tests cannot catch it.")
            print(f"         evidence: {copy_dir / 'pytest.log'}")
            continue

        missing = [t for t in expect if t not in got]
        if expect and missing:
            all_sealed = False
            print("  [FAIL] went red, but NOT via the expected tests.")
            print(f"         expected red: {expect}")
            print(f"         actually red: {got}")
            print(f"         still green : {missing}")
            print(f"         evidence: {copy_dir / 'pytest.log'}")
            continue

        print(f"  [OK]   SEALED -- tests went red, {len(got)} case(s):")
        for t in got:
            print(f"           - {t}")
        print(f"         evidence: {copy_dir / 'pytest.log'}")

    return all_sealed


def main() -> int:
    parser = argparse.ArgumentParser(description="Mutation check: prove regression tests can go red")
    parser.add_argument("--group", help="mutation group name (see scripts/mutations.json)")
    parser.add_argument("--list", action="store_true", help="list every mutation group")
    parser.add_argument("--verify-only", action="store_true",
                        help="fast: only check that every mutation definition still applies")
    args = parser.parse_args()

    spec = load_spec()

    if args.verify_only:
        problems = verify_spec_applies(spec)
        if problems:
            print(f"{len(problems)} mutation definition(s) no longer apply:")
            for p_ in problems:
                print(f"  - {p_}")
            return 1
        n = sum(len(g['mutations']) for g in spec.values())
        print(f"OK: all {n} mutations across {len(spec)} groups still apply.")
        return 0

    if args.list or not args.group:
        print("mutation groups in scripts/mutations.json:")
        print("")
        for name, g in spec.items():
            print(f"  {name}")
            print(f"      why   : {g.get('why', '')}")
            print(f"      tests : {', '.join(g['tests'])}")
            print(f"      mutant: {len(g['mutations'])}")
        if not args.group:
            print("")
            print("run one group with --group <name>")
        return 0

    if args.group not in spec:
        sys.exit(f"no such mutation group: {args.group} (use --list)")

    if not VENV_PYTHON.exists():
        sys.exit(f"venv interpreter not found: {VENV_PYTHON}")

    ok = check_group(args.group, spec[args.group])

    print("")
    print("=" * 72)
    if ok:
        print("VERDICT: every mutant turned the tests red -> this group IS SEALED.")
    else:
        print("VERDICT: at least one mutant stayed green -> NOT SEALED.")
        print("         the code may be fixed, but the TESTS cannot catch it.")
    print("=" * 72)
    print("")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
