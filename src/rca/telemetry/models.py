"""
遥测层的数据结构。

对应 Java：一堆 DTO / record。用 @dataclass 让 Python 自动生成
__init__ / __repr__ / __eq__，不用手写样板代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class LogRecord:
    """一条解析后的日志。

    对应 Java：一个不可变 record。
    保留 `raw` 是为了**可追溯** —— 降维之后若对某条结论有疑问，
    还能回到原始行核对。
    """

    ts: datetime
    level: str          # INFO / WARNING / ERROR / DEBUG
    service: str        # order / inventory / payment
    trace_id: str | None
    message: str        # 去掉时间戳/级别/服务/trace 之后的正文本
    raw: str            # 完整原始行

    @property
    def is_error(self) -> bool:
        return self.level in ("ERROR", "CRITICAL")


@dataclass
class TemplateStat:
    """一个「消息模板」的聚合统计。

    模板 = 把消息里的可变部分（数字、ID、耗时）归一化之后剩下的骨架。
    例如这三条：

        连接池等待 350ms（池大小=2，当前在途=2）
        连接池等待 812ms（池大小=2，当前在途=2）
        连接池等待 1204ms（池大小=2，当前在途=2）

    都属于同一个模板：

        连接池等待 <N>ms（池大小=<N>，当前在途=<N>）

    这样 3 万行日志才能收敛成几十个模板 —— 这就是降维的核心。
    """

    template: str
    service: str
    level: str
    count: int
    first_ts: datetime
    last_ts: datetime
    samples: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.last_ts - self.first_ts).total_seconds()


@dataclass
class TimelineBucket:
    """时间线上的一个桶（默认按分钟）。

    为什么必须保留时间维度：
        故障有**先后顺序**。"连接池等待"先出现、"超时"后出现，
        和反过来，是完全不同的两个故事。只给聚合计数会丢掉这个顺序。
    """

    minute: str                       # "04:12"
    counts: dict[str, int] = field(default_factory=dict)   # level -> 次数

    @property
    def errors(self) -> int:
        return self.counts.get("ERROR", 0)

    @property
    def warnings(self) -> int:
        return self.counts.get("WARNING", 0)


@dataclass
class ReducedView:
    """降维结果 —— 这就是最终交给 Agent 看的东西。"""

    window_start: datetime | None
    window_end: datetime | None
    total_lines: int

    by_service: dict[str, int] = field(default_factory=dict)
    by_level: dict[str, int] = field(default_factory=dict)

    timeline: list[TimelineBucket] = field(default_factory=list)
    templates: list[TemplateStat] = field(default_factory=list)

    # 降维过程中被丢弃的行数（INFO 类，按频次裁剪掉的）。
    # ⚠️ 必须显式记录：Agent 有权知道"你看到的不是全部"。
    dropped_info_lines: int = 0
    unparsed_lines: int = 0

    def render(self) -> str:
        """渲染成给 Agent 读的文本。

        刻意用紧凑的文本而非 JSON —— token 更省，而且模型读得更好。
        """
        out: list[str] = []
        if self.window_start and self.window_end:
            span = (self.window_end - self.window_start).total_seconds()
            out.append(
                f"# 日志降维视图  窗口 {self.window_start:%H:%M:%S}–"
                f"{self.window_end:%H:%M:%S}（{span:.0f}s）  原始 {self.total_lines} 行"
            )
        else:
            out.append(f"# 日志降维视图  原始 {self.total_lines} 行")

        out.append(
            "按服务：" + "  ".join(f"{k}={v}" for k, v in sorted(self.by_service.items()))
        )
        out.append(
            "按级别：" + "  ".join(f"{k}={v}" for k, v in sorted(self.by_level.items()))
        )
        if self.dropped_info_lines:
            out.append(
                f"（已按频次裁剪 {self.dropped_info_lines} 行 INFO；"
                f"ERROR/WARNING **一条未裁**）"
            )
        if self.unparsed_lines:
            out.append(f"（{self.unparsed_lines} 行未能解析格式，已计入总数）")

        # ---- 时间线：只画有错误的那些分钟，省 token ----
        hot = [b for b in self.timeline if b.errors or b.warnings]
        if hot:
            out.append("")
            out.append("## 异常时间线（出现 WARNING/ERROR 的分钟）")
            for b in hot:
                parts = []
                if b.errors:
                    parts.append(f"E{b.errors}")
                if b.warnings:
                    parts.append(f"W{b.warnings}")
                out.append(f"  {b.minute}  " + " ".join(parts))

        # ---- 模板：按严重级别聚合 ----
        out.append("")
        out.append("## 消息模板（按严重级别列出）")
        for level in ("ERROR", "WARNING", "INFO"):
            group = [t for t in self.templates if t.level == level]
            if not group:
                continue
            out.append(f"### {level}（{len(group)} 个模板）")
            for t in group:
                span = f"{t.first_ts:%H:%M:%S}→{t.last_ts:%H:%M:%S}"
                out.append(f"  [{t.service}] ×{t.count}  {span}  {t.template}")
                for s in t.samples[:2]:
                    out.append(f"      例：{s}")
        return "\n".join(out)

    def token_estimate(self) -> int:
        """用真实分词器估算 token 数（不是按字符数猜）。

        为什么要真算：本层的验收标准就是"降维后能装进上下文"，
        用估算数字会误导决策。tiktoken 是 OpenAI 的分词器，
        对中文的切分与主流模型接近，够用作量级判断。
        """
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(self.render()))
