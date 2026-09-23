"""
路径守卫：把任意输入路径规范化，再判断它是否在授权范围内。

============================ 这是整个 D4 的核心 ============================

2026-09-22 那次事故的直接成因就是**这一步缺失**：

    子代理要删的路径来自一个"被编码搞乱的名字"，
    它没有把路径规范化，也没有比对白名单 ——
    于是"它以为的临时目录"实际落到了 D:\\BarneyQ\\Docs，一个真实目录。

如果当时有这一层：路径一规范化就会落到授权范围之外，
**直接拒绝，不会有第二次机会造成损失。**

============================ 归一化必须在比对之前 ============================

顺序不能换。如果先比对再归一化，下面这些全部能绕过：

    /授权目录/../../etc/passwd          ← ".." 穿越
    /授权目录/软链接指向/etc            ← 符号链接穿越
    Ａｕｔｈｏｒｉｚｅｄ（全角）              ← Unicode 同形字
    C:\\Authorized（大小写不同）          ← Windows 大小写不敏感

所以流程是固定的：

    1. 拒绝明显非法的输入（空、NUL、控制字符、超长）
    2. Unicode NFC 归一化（把全角、组合字符收敛成标准形式）
    3. expanduser 展开 "~"
    4. 相对路径基于 base 拼接
    5. resolve() —— **同时解析 ".." 和符号链接**，得到真实绝对路径
    6. 用真实路径去比对白名单前缀

============================ 关于"禁止交给 shell 二次解析"（FR-2.3）============================

本模块**只返回 pathlib.Path 对象**，调用方不得把它拼进 shell 命令字符串。
一旦拼字符串，shell 会再解析一次路径 —— 那正是事故链条里
"路径被 PowerShell 重编码"的入口。

所以本仓库的规矩是：**路径永远以 Path 对象形式传递，
需要执行外部程序时用参数数组（列表），不用 shell=True。**
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# 单个路径分量的长度上限（Windows 经典限制是 255）
_MAX_COMPONENT = 255
_MAX_TOTAL = 4096


@dataclass(frozen=True)
class PathCheck:
    """一次路径判定的完整信息。

    **`original` 与 `resolved` 都要留档**：
    事故里如果留下了这两者的对比，一眼就能看出
    "输入是 A，解析出来却是 B" —— 而当时的记录里什么都没有。
    """

    ok: bool
    original: str
    resolved: Path | None
    reason: str = ""
    matched_root: Path | None = None


def normalize(raw: str, base: Path | None = None) -> Path:
    """把任意输入规范化成真实的绝对路径。

    抛 ValueError 表示"输入本身就不合法"（空、含 NUL、含控制字符、过长）。
    """
    if raw is None:
        raise ValueError("路径为空")
    if not isinstance(raw, str):
        raise ValueError(f"路径必须是字符串，收到 {type(raw).__name__}")

    # --- 1. 非法字符 ---
    if raw == "" or raw.strip() == "":
        raise ValueError("路径为空")
    if "\x00" in raw:
        raise ValueError("路径含 NUL 字节（典型的截断攻击载荷）")
    # 控制字符：正常路径里不会有。乱码里经常出现，所以单独拦一道。
    ctrl = [c for c in raw if ord(c) < 0x20 and c not in ("\t",)]
    if ctrl:
        raise ValueError(f"路径含控制字符 {ctrl!r}")

    # --- 2. Unicode 归一化 ---
    # NFC 把"全角 Ａ"和"半角 A"收敛成同一个码位，
    # 否则只看字符串的话，全角版本会被当成"另一个目录"而绕过白名单。
    text = unicodedata.normalize("NFC", raw)

    # --- 3. 长度限制 ---
    if len(text) > _MAX_TOTAL:
        raise ValueError(f"路径过长（{len(text)} > {_MAX_TOTAL}）")
    for part in text.replace("\\", "/").split("/"):
        if len(part) > _MAX_COMPONENT:
            raise ValueError(f"单个路径分量过长（{len(part)}）")

    # --- 4. ~ 展开 ---
    expanded = os.path.expanduser(text)

    # --- 5. 相对路径基于 base ---
    p = Path(expanded)
    if not p.is_absolute():
        p = (base or Path.cwd()) / p

    # --- 6. 解析 ".." 与符号链接 ---
    # strict=False：路径不存在也能解析（我们要判断的正是"能不能动它"）
    try:
        return p.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"路径无法解析：{exc}") from exc


def is_within(path: Path, roots: Sequence[Path]) -> Path | None:
    """判断 path 是否落在某个授权根之下。命中则返回那个根，否则 None。

    ⚠️ 两个容易写错的地方：

      1) **必须比对"规范化后的"两侧**。调用方传进来的 root 也要 resolve 过，
         否则"授权根本身带 .."就会导致前缀比对失效。

      2) **必须按平台规则比对大小写**。Windows 大小写不敏感，
         用字符串 startswith 会把 `C:\\Allowed` 和 `c:\\allowed` 当成不同路径
         —— 要么误拒（烦人），要么更糟：如果哪天比对方向反了，就会误放。

    这里用 os.path.normcase 统一处理（Windows 下会转小写并把 / 换成 \\）。
    """
    if path is None:
        return None
    target = os.path.normcase(str(path))

    for root in roots:
        r = os.path.normcase(str(root)).rstrip("\\/")
        if not r:
            continue
        # 相等，或以 "root + 分隔符" 开头
        # 必须带分隔符，否则 /allowed-evil 会被 /allowed 误判为在范围内
        if target == r or target.startswith(r + os.sep):
            return root
    return None


def check_path(raw: str, roots: Sequence[Path], base: Path | None = None) -> PathCheck:
    """规范化 + 白名单校验，一步到位。

    这是策略引擎唯一应该调用的入口 —— 不要自己拆开调用 normalize 再 is_within，
    那会把"顺序不能换"这条纪律重新交回给调用方（而纪律是守不住的）。
    """
    try:
        resolved = normalize(raw, base=base)
    except ValueError as exc:
        return PathCheck(ok=False, original=raw, resolved=None, reason=str(exc))

    matched = is_within(resolved, roots)
    if matched is None:
        allowed = "、".join(str(r) for r in roots) or "（未配置任何授权根）"
        return PathCheck(
            ok=False,
            original=raw,
            resolved=resolved,
            reason=f"路径不在任何授权范围内（授权范围：{allowed}）",
        )

    return PathCheck(ok=True, original=raw, resolved=resolved, matched_root=matched)
