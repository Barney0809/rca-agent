"""
可观测性三件套：日志、指标、trace id。

============================ 给 Python 新手的说明 ============================

这个模块解决三个问题，每个都对应你在 Java 里熟悉的东西：

  1) 日志（logging）
     Python 标准库自带 logging，用法和 Log4j / Logback 很像：
       在别处写  log = logging.getLogger("order")
       然后       log.info("下单成功")
     不需要自己实现 appender/handler 的层级，只要一次 basicConfig 配好格式。

  2) 指标（metrics）
     Python 没有内置的 Micrometer，所以这里手写一个极简版：
       计数器只增（counter），用在"发生了多少次"
       仪表可增可减（gauge），用在"现在是多少"
     输出成 Prometheus 的文本格式——那只是个纯文本约定，手写完全够用。

  3) trace id（跨服务串联）
     ⚠️ 关键设计：trace id 一律【显式传参】，不用全局变量 / 上下文变量。
     原因见下面的注释——这不是洁癖，是踩过坑的结论。

=============================================================================
"""

from __future__ import annotations

import logging
import sys
import time
from collections import defaultdict
from typing import Any

# ---------------------------------------------------------------- trace id
#
# ⚠️ 为什么不用 ContextVar / threading.local 存 trace id？
#
# 因为服务里存在"请求处理之外"的执行者：后台任务、重试、连接池回调。
# 这些执行者不继承请求的上下文，全局变量在它们那里要么是空的、要么是
# 【上一个请求的值】——于是日志被串到别的请求上，事后完全无法对账。
#
# 真实教训：另一个项目里用 ThreadLocal 传 traceId，在线程池上出现
# "静默丢账 / 串账"，排查了两天才定位。改用显式传参后问题消失。
#
# 所以本项目的规矩是：
#     trace id 从 HTTP 头进来 → 作为函数参数一路往下传 → 写进每条日志
# 绝不放进任何"隐式上下文"。

TRACE_HEADER = "X-Trace-Id"
REQUEST_HEADER = "X-Request-Id"


def new_trace_id() -> str:
    """生成一个短 trace id。用时间戳 + 随机数，够用且便于人眼排序。"""
    import secrets
    return f"{int(time.time() * 1000) % 100000:05d}{secrets.token_hex(3)}"


class TraceLogger(logging.LoggerAdapter):
    """给日志自动带上 [service] trace=xxx 前缀的适配器。

    用法：
        log = get_logger("order")
        log.info("收到下单请求", extra={"trace": tid})

    输出形如：
        2026-09-24 03:50:12.123 INFO  [order] trace=8f3a1c | 收到下单请求
    """

    def process(self, msg: Any, kwargs: dict) -> tuple[str, dict]:
        extra = kwargs.get("extra") or {}
        trace = extra.get("trace") or "-"
        svc = self.extra.get("service", "?")
        return f"[{svc}] trace={trace} | {msg}", kwargs


def setup_logging(service: str, level: str = "INFO") -> None:
    """配置根 logger 的输出格式。

    ⚠️ 这里刻意输出【文本】而不是 JSON。
    真实世界的服务日志大多是文本格式（Logback 默认就是），
    我们的"日志降维"环节需要真的去解析它——如果直接给 JSON，
    那个环节就没有存在意义了。
    """
    fmt = "%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 把 uvicorn 自带的 access log 关掉，避免和我们自己的日志重复
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(service: str) -> TraceLogger:
    """取一个带服务名前缀的 logger。"""
    return TraceLogger(logging.getLogger(service), {"service": service})


# ---------------------------------------------------------------- metrics
#
# 只实现两种最常用的指标：
#   counter —— 只增不减，语义是"累计发生了多少次"
#   gauge   —— 可增可减，语义是"当前值是多少"
#
# 标签（labels）用 dict 表示，和 Prometheus 的概念一致。

class Metrics:
    def __init__(self, service: str) -> None:
        self.service = service
        self._counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple], float] = {}
        self._hist_sum: dict[tuple[str, tuple], float] = defaultdict(float)
        self._hist_count: dict[tuple[str, tuple], int] = defaultdict(int)
        self._help: dict[str, str] = {}

    # ---- 内部：把 labels 这个 dict 变成可做 key 的元组（dict 不能当 key） ----
    @staticmethod
    def _key(labels: dict[str, str] | None) -> tuple:
        if not labels:
            return ()
        return tuple(sorted(labels.items()))

    @staticmethod
    def _render_labels(labels: tuple) -> str:
        if not labels:
            return ""
        inner = ",".join(f'{k}="{v}"' for k, v in labels)
        return "{" + inner + "}"

    # ---- 写 ----
    def inc(self, name: str, value: float = 1, **labels: str) -> None:
        """计数器 +value。"""
        self._counters[(name, self._key(labels))] += value

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        """设置仪表值。"""
        self._gauges[(name, self._key(labels))] = value

    def observe(self, name: str, value: float, **labels: str) -> None:
        """记录一次观测（用于算平均值，例如单次请求耗时）。"""
        k = (name, self._key(labels))
        self._hist_sum[k] += value
        self._hist_count[k] += 1

    def describe(self, name: str, text: str) -> None:
        """给指标写一行 HELP 说明。"""
        self._help[name] = text

    # ---- 读：渲染成 Prometheus 文本格式 ----
    def render(self) -> str:
        lines: list[str] = []
        seen: set[str] = set()

        def head(name: str, mtype: str) -> None:
            if name in seen:
                return
            seen.add(name)
            if name in self._help:
                lines.append(f"# HELP {name} {self._help[name]}")
            lines.append(f"# TYPE {name} {mtype}")

        for (name, labels), val in sorted(self._counters.items()):
            head(name, "counter")
            lines.append(f"{name}{self._render_labels(labels)} {val:g}")

        for (name, labels), val in sorted(self._gauges.items()):
            head(name, "gauge")
            lines.append(f"{name}{self._render_labels(labels)} {val:g}")

        for (name, labels), total in sorted(self._hist_sum.items()):
            cnt = self._hist_count[(name, labels)]
            head(name + "_sum", "counter")
            lines.append(f"{name}_sum{self._render_labels(labels)} {total:.3f}")
            head(name + "_count", "counter")
            lines.append(f"{name}_count{self._render_labels(labels)} {cnt}")

        return "\n".join(lines) + "\n"
