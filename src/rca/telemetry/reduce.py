"""
降维流水线：结构化记录 → 少量上下文。

============================ 核心规则（再次强调）============================

    ERROR / WARNING 模板  →  无条件全部保留
    INFO 模板             →  按频次保留前 N 个

**按严重级别保留，而不是按频次保留** —— 这是"关键信号不丢"的机制保证。

反例：一条 ERROR 出现 1 次，一条 INFO 出现 5 万次。
      按频次排序，那条 ERROR 会被挤出上下文 ——
      而它恰好是根因的唯一证据。

============================ 时间线为什么必须留 ============================

故障有**先后顺序**：

    「先看到连接池等待，后看到超时」  ≠  「先超时，后等待」

前者是"池被慢慢抽干"，后者是"一开始就崩了"。只给聚合计数会把这两种
完全不同的故事压成一个数字。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .models import LogRecord, ReducedView, TemplateStat, TimelineBucket
from .parse import template_of

# uvicorn 自带日志没有时间戳，解析时用这个哨兵值占位
_SENTINEL = datetime(1970, 1, 1)
_SENTINEL_YEAR = 1970


def reduce_logs(
    records: list[LogRecord],
    *,
    min_info_count: int = 3,
    max_info_templates: int = 12,
    samples_per_template: int = 2,
) -> ReducedView:
    """把日志记录降维成 ReducedView。

    参数：
        min_info_count      INFO 模板至少出现几次才保留（低于此值且超出上限就裁掉）
        max_info_templates  INFO 模板最多保留多少个
        samples_per_template 每个模板保留几条原始样本（保留具体数值，便于核对）
    """
    view = ReducedView(window_start=None, window_end=None, total_lines=len(records))

    if not records:
        return view

    # ---------------- 1. 总量统计 ----------------
    by_service: dict[str, int] = defaultdict(int)
    by_level: dict[str, int] = defaultdict(int)
    for r in records:
        by_service[r.service] += 1
        by_level[r.level] += 1

    view.by_service = dict(by_service)
    view.by_level = dict(by_level)

    # ---------------- 2. 时间窗口 ----------------
    real_ts = [r.ts for r in records if r.ts.year != _SENTINEL_YEAR]
    if real_ts:
        view.window_start = min(real_ts)
        view.window_end = max(real_ts)

    # ---------------- 3. 模板聚合 ----------------
    # key = (service, level, template) -> 聚合累加器
    agg: dict[tuple[str, str, str], dict] = {}
    for r in records:
        tpl = template_of(r.message)
        key = (r.service, r.level, tpl)
        slot = agg.get(key)
        if slot is None:
            agg[key] = {
                "count": 1,
                "first": r.ts,
                "last": r.ts,
                "samples": [r.raw] if samples_per_template else [],
            }
        else:
            slot["count"] += 1
            if r.ts < slot["first"]:
                slot["first"] = r.ts
            if r.ts > slot["last"]:
                slot["last"] = r.ts
            if len(slot["samples"]) < samples_per_template:
                slot["samples"].append(r.raw)

    stats = [
        TemplateStat(
            template=tpl,
            service=svc,
            level=lvl,
            count=slot["count"],
            first_ts=slot["first"],
            last_ts=slot["last"],
            samples=slot["samples"],
        )
        for (svc, lvl, tpl), slot in agg.items()
    ]

    # ---------------- 4. 按严重级别筛选（本层的核心规则）----------------
    # ⚠️ ERROR / WARNING **不做任何截断**
    errors = [s for s in stats if s.level in ("ERROR", "CRITICAL")]
    warnings = [s for s in stats if s.level == "WARNING"]
    infos = [s for s in stats if s.level not in ("ERROR", "CRITICAL", "WARNING")]

    # 排序：先按出现次数降序，再按出现时间升序（次数相同则"更早出现"的排前）
    for group in (errors, warnings, infos):
        group.sort(key=lambda s: (-s.count, s.first_ts))

    kept_infos = [s for s in infos if s.count >= min_info_count][:max_info_templates]
    dropped = [s for s in infos if s not in kept_infos]
    view.dropped_info_lines = sum(s.count for s in dropped)

    view.templates = errors + warnings + kept_infos

    # ---------------- 5. 时间线（按分钟）----------------
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in records:
        if r.ts.year == _SENTINEL_YEAR:
            continue
        minute = f"{r.ts:%H:%M}"
        buckets[minute][r.level] += 1

    view.timeline = [
        TimelineBucket(minute=m, counts=dict(c)) for m, c in sorted(buckets.items())
    ]

    return view
