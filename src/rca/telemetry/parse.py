"""
解析层：原始文本 → 结构化记录。

============================ 为什么日志是"文本"而不是 JSON ============================

被诊断系统的日志刻意用**文本格式**（`docs/adr/0004`）。原因：

  真实世界的服务日志绝大多数是文本（Logback 默认就是）。
  如果直接给 JSON，这一层的"解析"就没有存在意义了 ——
  而这个项目要证明的能力里，恰好包含"能把噪声变成信号"。

所以这里的解析**必须容忍两种格式**（我们自己的格式 + uvicorn 自带的），
并且**明确统计有多少行没解析出来**（不许静默丢弃）。
"""

from __future__ import annotations

import re
from datetime import datetime

from .models import LogRecord

# ---------------------------------------------------------------- 格式 1：带时间戳的日志
#
# 这个格式实际有三种变体，`[service]` 段**可能有也可能没有**：
#
#   a) 我们自己写的（有 service、有 trace）
#      2026-09-24 03:52:19.348 INFO  [order] trace=smoke1f1a486c | 收到下单请求 ...
#   b) 我们自己写的（service 有，trace 为 -）
#      2026-09-24 03:52:19.348 INFO  [order] trace=- | 服务启动 ...
#   c) **第三方库写的**（httpx 等 —— 没有 service，也没有 trace）
#      2026-09-24 04:59:22.064 INFO  HTTP Request: POST http://inventory:8000/reserve "HTTP/1.1 502 Bad Gateway"
#
# ⚠️ 变体 (c) 曾经被整类漏掉，占了全部日志的 21%，而且它承载关键证据
#    （"下游 502"）。所以 `[service]` 必须做成**可选**。
#    见 docs/harness-log.md #6。
APP_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<level>[A-Z]{4,8})\s+"
    r"(?:\[(?P<service>[^\]]+)\]\s+)?"          # ← 可选，不可省略
    r"(?:trace=(?P<trace>[^|\s]+)\s*\|\s*)?"
    r"(?P<message>.*)$"
)

# ---------------------------------------------------------------- 格式 2：uvicorn 自带
# 例：
#   INFO:     Started server process [1]
#   INFO:     HTTP Request: POST http://inventory:8000/reserve "HTTP/1.1 200 OK"
UVICORN_RE = re.compile(r"^(?P<level>[A-Z]{4,8}):\s+(?P<message>.*)$")

_TS_FMT = "%Y-%m-%d %H:%M:%S.%f"

# ---------------------------------------------------------------- 模板归一化
# 把消息里的可变部分替换成占位符，让"同一个事件"的多次出现收敛成同一个模板。
#
# 顺序很重要：先替换长十六进制串（订单号、请求 id），再替换普通数字。
# 否则 "ORD-54AAD4C266" 会先被数字规则破坏成 "ORD-<N>AAD<N>C<N>"。
_HEXISH = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_NUMERIC = re.compile(r"\d+(?:\.\d+)?")
_WS = re.compile(r"\s+")


def template_of(message: str) -> str:
    """把一个消息归一化成"模板"。

    例：
        连接池等待 350ms（池大小=2，当前在途=2）
        连接池等待 812ms（池大小=2，当前在途=2）
    都变成：
        连接池等待 <N>ms（池大小=<N>，当前在途=<N>）

    这就是降维能成立的原因：几万行日志其实只有几十个"事件类型"。
    """
    text = _HEXISH.sub("<ID>", message)
    text = _NUMERIC.sub("<N>", text)
    return _WS.sub(" ", text).strip()


def parse_log_line(line: str, default_service: str) -> LogRecord | None:
    """解析一行日志。解析不了就返回 None（由调用方统计，不许静默丢）。"""
    line = line.rstrip("\r\n")
    if not line.strip():
        return None

    m = APP_LOG_RE.match(line)
    if m:
        try:
            ts = datetime.strptime(m.group("ts"), _TS_FMT)
        except ValueError:
            return None
        trace = m.group("trace")
        return LogRecord(
            ts=ts,
            level=m.group("level").upper(),
            service=m.group("service") or default_service,
            trace_id=None if trace in (None, "-", "") else trace,
            message=m.group("message").strip(),
            raw=line,
        )

    m = UVICORN_RE.match(line)
    if m:
        # uvicorn 自带日志没有时间戳 —— 用"当前时间"会撒谎，所以置为 None 不可行
        # （LogRecord.ts 是必填）。这里用 sentinel 时间 1970，并在降维时排除出时间线。
        return LogRecord(
            ts=datetime(1970, 1, 1),
            level=m.group("level").upper(),
            service=default_service,
            trace_id=None,
            message=m.group("message").strip(),
            raw=line,
        )

    return None


# ---------------------------------------------------------------- Prometheus 文本格式
# 例：
#   # HELP requests_total ...
#   # TYPE requests_total counter
#   requests_total{endpoint="create_order"} 1529
#   pool_in_flight 64
def parse_prometheus(text: str) -> dict[str, float]:
    """把 Prometheus 文本解析成 {序列名: 值}。

    序列名保留标签，例如 `requests_total{endpoint="create_order"}`。
    这样 Agent 才能区分"下单接口的请求数"和"扣款接口的请求数"。
    """
    out: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # 从右边切一次，取最后一个空格后的数值；左边整段都是序列名
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        name, value = parts[0].strip(), parts[1].strip()
        try:
            out[name] = float(value)
        except ValueError:
            continue
    return out
