"""实测 stdout 的编码行为：三种"兜底写法"到底哪种是对的。

================================ 为什么需要这个脚本 ================================

`AGENTS.md` 第 4 条"坑 4"和 `docs/harness-log.md` P4 里有一张对照表，
结论是"必须同时给 `encoding` 和 `errors`"。

**那张表是量出来的，不是想出来的。** 这个脚本就是量它的工具 ——
所以它被留在仓库里，而不是当一次性脚本丢掉：面试官或未来的自己都能重跑一遍验证。

================================ 为什么会不一样 ================================

同一个 Python 程序，stdout 接到哪里，行为完全不同：

| stdout 接到 | Python 用的编码 | 打印 emoji |
|---|---|---|
| 真实控制台 | `_WindowsConsoleIO`，UTF-8，走 `WriteConsoleW` | ✅ 正常 |
| **管道 / 文件** | **本地编码 `cp936`（GBK）** | ❌ 抛 `UnicodeEncodeError` |

本项目的脚本经常被上层采集输出（`pwsh` 工具、CI、`| Out-File`），
**走的全是第二行那条路** —— 这才是问题所在。

================================ 怎么用 ================================

    .\\.venv\\Scripts\\python.exe scripts\\probe_stdout_encoding.py none
    .\\.venv\\Scripts\\python.exe scripts\\probe_stdout_encoding.py replace
    .\\.venv\\Scripts\\python.exe scripts\\probe_stdout_encoding.py utf8

三种模式分别模拟"完全不兜底 / 只加 errors / encoding+errors 都给"。

⚠️ 注意本脚本**自己**是带标准守卫的（要过 `test_regression_p4_*`），
   所以三个模式是靠**显式 reconfigure** 把状态改回去**模拟**出来的。
"""

from __future__ import annotations

import sys

# 标准守卫 —— 仓库里每个会 print 的脚本都有这一段，写法见 AGENTS.md 坑 4。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def simulate(mode: str) -> None:
    """把 stdout 调到指定的模拟状态。

    这里刻意用 `encoding="gbk"` 显式复现"退回本地编码"的条件，
    而不是靠"不调用 reconfigure" —— 因为本脚本开头已经有守卫了。
    """
    if mode == "none":
        # 模拟"完全不兜底"：GBK + strict（strict 就是默认值）
        sys.stdout.reconfigure(encoding="gbk", errors="strict")
    elif mode == "replace":
        # 模拟"只加 errors='replace'，没给 encoding"
        sys.stdout.reconfigure(encoding="gbk", errors="replace")
    elif mode == "utf8":
        # 模拟标准守卫，什么也不用做
        pass
    else:
        raise SystemExit(f"unknown mode: {mode} (use none / replace / utf8)")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "utf8"
    simulate(mode)

    print(f"mode = {mode}")
    print(f"  stdout.encoding = {sys.stdout.encoding}")
    print(f"  stdout.errors   = {sys.stdout.errors}")
    print(f"  isatty          = {sys.stdout.isatty()}")
    print("  [1] pure ascii line ..................... ok")

    try:
        print("  [2] 中文这一行能不能正常显示 / 会不会崩")
        print("  [3] emoji: \u2705 \u274c \u26a0")
    except UnicodeEncodeError as exc:
        print(f"  RESULT: CRASH -> {exc}")
        # 刻意返回非零：这一格就是表格里的 ❌，是**预期**的失败
        return 1

    print("  RESULT: no crash")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
