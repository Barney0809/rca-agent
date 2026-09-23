"""
LLM 层：模型访问、成本核算、录制回放。

============================ 为什么成本要算这么细 ============================

D5 的验收标准是三个数字：**准确率 / 步数 / 成本**。

成本之所以要单独算清楚：
  1) 它是"多 Agent 是否值得"的一个关键论据（三路并行 + 模型路由能省钱吗？）
  2) DeepSeek 的计费有两个容易算错的点：
     · **峰谷价差 2 倍** —— 高峰时段仅为工作日 9:00-12:00、14:00-18:00
     · **缓存命中与未命中差 50 倍**（0.02 vs 1 元/百万 token）
  3) 还有一个陷阱：**响应里的模型名可能与请求的不同**（可能被路由改派），
     所以单价必须按**响应**里的模型名去查，按请求名查会算错账。
"""

from .provider import (
    DeepSeekClient,
    LlmConfig,
    LlmResult,
    ToolCall,
    estimate_cost_yuan,
    is_peak_hour,
)
from .recording import Recorder

__all__ = [
    "DeepSeekClient",
    "LlmConfig",
    "LlmResult",
    "Recorder",
    "ToolCall",
    "estimate_cost_yuan",
    "is_peak_hour",
]
