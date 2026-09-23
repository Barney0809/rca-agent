"""
隔离区：把"删除"变成"挪走"，于是不可逆变成可逆。

============================ 为什么这是 D4 最重要的东西 ============================

2026-09-22 那次事故之所以无法挽回，**根因不是守门没拦住**，而是
**物理层面根本没有留下退路**：

    -Force 绕过了回收站
    磁盘又没有卷影副本
    文件系统层面直接往下走 → NVMe + TRIM → 数据块被回收

所以只做"更严格地守删除"是不够的 —— 只要有一次守门失手，结果一样。
**正解是让"删除"这个动作在系统里不存在。**

============================ 设计决定：隔离区是终点，不是中转站 ============================

一个自然的想法是给隔离项加 TTL，到期自动清理。**本项目刻意不这么做。**

    原因一：一旦存在"自动删除"，就存在"删错了"的可能 —— 只是把风险推后了。
    原因二：Agent 根本不需要删除能力。它需要的是"不再看到这个文件"，
            而"挪到隔离区"已经满足了这个需求。

所以：

    TTL 只用于**标记**"可以清理了"，由 `sweep_report()` **报告**给人看。
    真正的清除由人/运维执行，不在 Agent 的能力范围内。

这带来一个可以直接验证的性质（见回归用例）：
**`src/rca/policy/` 全目录不含任何硬删除调用。** —— 不是"我们小心不调用"，
而是"想调用也找不到"。
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

# 隔离项 id 的格式。⚠️ 必须严格校验：
# restore/describe 会用它拼路径，不校验的话 "q-../.." 就能穿越出去。
_QID_RE = re.compile(r"^q-\d{8}-\d{6}-[0-9a-f]{6}$")


class QuarantineError(RuntimeError):
    """隔离 / 还原过程中的问题。"""


@dataclass(frozen=True)
class QuarantineEntry:
    """一条隔离记录。存成 entry.json，与内容放在一起。"""

    quarantine_id: str
    original_path: str
    stored_name: str
    quarantined_at: str        # ISO8601
    ttl_s: int
    is_dir: bool
    note: str = ""

    @property
    def expires_at(self) -> datetime:
        return datetime.fromisoformat(self.quarantined_at) + timedelta(seconds=self.ttl_s)

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now().astimezone()) >= self.expires_at


def _new_id() -> str:
    now = datetime.now()
    return f"q-{now:%Y%m%d}-{now:%H%M%S}-{secrets.token_hex(3)}"


def _entry_dir(quarantine_root: Path, quarantine_id: str) -> Path:
    """由 id 得到条目目录。

    ⚠️ 先校验 id 格式，再拼路径 —— 顺序不能反。
       直接用未校验的 id 拼路径，就等于把路径穿越的入口留在这儿了。
    """
    if not _QID_RE.match(quarantine_id):
        raise QuarantineError(f"隔离项 id 格式非法：{quarantine_id!r}")
    return quarantine_root / quarantine_id


def quarantine(
    target: Path,
    quarantine_root: Path,
    ttl_s: int,
    note: str = "",
) -> QuarantineEntry:
    """把 target **移入**隔离区。

    ⚠️ 本函数只做 `shutil.move`，**不做任何删除**。
       target 不存在则抛 QuarantineError（而不是静默成功 ——
       静默成功会让上层以为"删掉了"，是最危险的一类错误）。
    """
    if not target.exists():
        raise QuarantineError(f"目标不存在，无法隔离：{target}")

    quarantine_root.mkdir(parents=True, exist_ok=True)
    qid = _new_id()
    entry_dir = quarantine_root / qid
    if entry_dir.exists():                      # 理论上不可能，防一手
        raise QuarantineError(f"隔离项 id 冲突：{qid}")

    payload_dir = entry_dir / "payload"
    payload_dir.mkdir(parents=True)

    entry = QuarantineEntry(
        quarantine_id=qid,
        original_path=str(target),
        stored_name=target.name,
        quarantined_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        ttl_s=ttl_s,
        is_dir=target.is_dir(),
        note=note,
    )

    # 移动（不是复制、更不是删除）。移动在同一磁盘上是原子的 rename。
    shutil.move(str(target), str(payload_dir / target.name))

    (entry_dir / "entry.json").write_text(
        json.dumps(asdict(entry), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return entry


def load_entry(quarantine_root: Path, quarantine_id: str) -> QuarantineEntry:
    entry_dir = _entry_dir(quarantine_root, quarantine_id)
    meta = entry_dir / "entry.json"
    if not meta.exists():
        raise QuarantineError(f"找不到隔离项：{quarantine_id}")
    return QuarantineEntry(**json.loads(meta.read_text(encoding="utf-8")))


def list_entries(quarantine_root: Path) -> list[QuarantineEntry]:
    """列出全部隔离项，按隔离时间排序。"""
    if not quarantine_root.exists():
        return []
    out: list[QuarantineEntry] = []
    for child in sorted(quarantine_root.iterdir()):
        if not child.is_dir() or not _QID_RE.match(child.name):
            continue
        meta = child / "entry.json"
        if not meta.exists():
            continue
        try:
            out.append(QuarantineEntry(**json.loads(meta.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def restore(quarantine_root: Path, quarantine_id: str, to: Path | None = None) -> Path:
    """把隔离项还原回去。返回还原后的路径。

    默认还原到原始位置；原始父目录若已不存在会自动重建。
    `to` 可以指定别的位置（用于"还原到别处"的场景）。
    """
    entry = load_entry(quarantine_root, quarantine_id)
    entry_dir = _entry_dir(quarantine_root, quarantine_id)
    payload = entry_dir / "payload" / entry.stored_name
    if not payload.exists():
        raise QuarantineError(f"隔离项内容已丢失：{quarantine_id}")

    dest = Path(to) if to is not None else Path(entry.original_path)
    if dest.exists():
        raise QuarantineError(f"还原目标已存在，拒绝覆盖：{dest}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(payload), str(dest))
    return dest


def sweep_report(
    quarantine_root: Path,
    now: datetime | None = None,
) -> list[QuarantineEntry]:
    """**报告**已过 TTL 的隔离项。**不删除任何东西。**

    这是刻意的设计（见模块头部）：Agent 连删除能力都不该有。
    本函数存在的意义是让"要不要清理"变成一个**人的决定**，
    而不是一个后台任务悄悄做的事。
    """
    now = now or datetime.now().astimezone()
    return [e for e in list_entries(quarantine_root) if e.is_expired(now)]


def summarize(quarantine_root: Path) -> dict:
    """隔离区概况（给审计与人工查看用）。"""
    entries = list_entries(quarantine_root)
    expired = sweep_report(quarantine_root)
    return {
        "count": len(entries),
        "bytes": sum(
            _dir_size(quarantine_root / e.quarantine_id / "payload") for e in entries
        ),
        "expired_count": len(expired),
        "oldest": entries[0].quarantined_at if entries else None,
    }


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def monotonic_ms() -> float:
    """留给调用方做耗时统计（这里只是避免 import time 被 lint 判为未使用）。"""
    return time.perf_counter() * 1000
