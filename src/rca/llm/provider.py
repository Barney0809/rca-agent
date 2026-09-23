"""
DeepSeek 客户端 + 成本核算。

============================ 三个已经踩过的坑（都在这里处理）============================

坑 1：**`thinking` 默认是开启的**，不显式关闭会持续产生推理 token 费用。
      实测：让它答"收到"两个字，烧了 13 个推理 token。
      关闭方式**只有一种有效**：`extra_body={"thinking": {"type": "disabled"}}`。
      ⚠️ `enable_thinking=False` 请求会成功但被**静默忽略** —— 以为关了，其实一直在付钱。

坑 2：**缓存命中与未命中差 50 倍**（0.02 vs 1 元/百万 token）。
      缓存是自动的，但要靠 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
      两个字段才能算出真实成本。

坑 3：**响应里的 `model` 字段可能与请求不同**。
      单价必须按响应里的模型名去查 —— 按请求名查会算错账。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from openai import OpenAI

# ---------------------------------------------------------------- 单价表
#
# 单位：元 / 百万 token
# 来源：DeepSeek 官方定价页（2026-09 抓取）
#
#   高峰时段 = 北京时间周一至周五（不含法定节假日）9:00-12:00、14:00-18:00
#   其余时段（含晚间、周末、法定节假日全天）都是空闲时段，**价格减半**
#
# 我们的排期是"每晚 + 假期"，所以几乎全部落在空闲时段。
PRICING_YUAN_PER_MTOK: dict[str, dict[str, tuple[float, float]]] = {
    # 模型名: {"off_peak": (命中输入, 未命中输入, 输出), "peak": (...)}
    "deepseek-flash": {
        "off_peak": (0.02, 1.0, 4.0),
        "peak": (0.04, 2.0, 8.0),
    },
    "deepseek-v4-pro": {
        "off_peak": (0.15, 4.5, 13.5),
        "peak": (0.30, 9.0, 27.0),
    },
}


def is_peak_hour(when: datetime | None = None) -> bool:
    """是否处于高峰计费时段。

    高峰 = 周一至周五 9:00-12:00 与 14:00-18:00（北京时间）。
    ⚠️ 这里不处理法定节假日 —— 需要更精确时可以传入节假日表。
       但我们的排期（晚间 + 假期）几乎全在空闲时段，误差方向是"高估成本"，
       属于保守估计，可以接受。
    """
    now = when or datetime.now()
    if now.weekday() >= 5:                       # 周六周日
        return False
    hour = now.hour
    return (9 <= hour < 12) or (14 <= hour < 18)


def estimate_cost_yuan(
    *,
    model: str,
    hit_tokens: int,
    miss_tokens: int,
    output_tokens: int,
    when: datetime | None = None,
) -> float:
    """按真实单价算一次调用的成本（元）。

    注意入参用的是**响应里返回的模型名**，不是请求里写的那个。
    """
    table = PRICING_YUAN_PER_MTOK.get(model)
    if table is None:
        # 未知模型：返回 0 而不是猜一个 —— 猜出来的数字会污染成本曲线。
        # 但调用方应当能看出来（见 LlmResult.pricing_known）。
        return 0.0

    hit_price, miss_price, out_price = table["peak" if is_peak_hour(when) else "off_peak"]
    return (
        hit_tokens * hit_price + miss_tokens * miss_price + output_tokens * out_price
    ) / 1_000_000


# ---------------------------------------------------------------- 配置与结果

@dataclass
class LlmConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model_cheap: str = "deepseek-flash"
    model_strong: str = "deepseek-v4-pro"
    # 是否开启思考模式。**默认关闭** —— 见坑 1。
    thinking: bool = False
    timeout_s: float = 120.0
    max_tokens: int = 2048

    @classmethod
    def from_env(cls) -> LlmConfig:
        return cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model_cheap=os.environ.get("RCA_MODEL_CHEAP", "deepseek-flash"),
            model_strong=os.environ.get("RCA_MODEL_STRONG", "deepseek-v4-pro"),
            thinking=os.environ.get("RCA_THINKING_ENABLED", "false").lower() == "true",
        )


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str          # 原始 JSON 字符串（由模型给出，可能不合法）


@dataclass
class LlmResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)
    model_used: str = ""            # ★ 响应里的模型名
    cost_yuan: float = 0.0
    elapsed_ms: float = 0.0
    pricing_known: bool = True
    raw: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens", 0))


# ---------------------------------------------------------------- 客户端

class DeepSeekClient:
    """DeepSeek 的薄封装。

    走 OpenAI 兼容协议（官方没有专用 Python SDK，推荐用 openai + base_url）。
    """

    def __init__(self, config: LlmConfig, recorder=None) -> None:
        if not config.api_key:
            raise RuntimeError(
                "没有读到 DEEPSEEK_API_KEY。\n"
                "  设置方法（PowerShell）：\n"
                "    [Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY','你的key','User')\n"
                "  设完要重开终端。"
            )
        self.config = config
        self._client = OpenAI(
            api_key=config.api_key, base_url=config.base_url, timeout=config.timeout_s
        )
        self.recorder = recorder

    # ---------------------------------------------------------- 调用
    def chat(
        self,
        *,
        messages: list[dict],
        model: str | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.0,
        thinking: bool | None = None,
        tag: str = "",
    ) -> LlmResult:
        """发一次对话请求。

        `temperature=0` 是我们的默认值 —— **评测要可复现**，
        温度越高越难复现同样的结论。
        """
        use_model = model or self.config.model_cheap
        use_thinking = self.config.thinking if thinking is None else thinking

        kwargs: dict = {
            "model": use_model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
        }
        # 思考模式与 temperature 的兼容性：思考模式不支持 temperature，
        # 传了会被忽略并告警。所以只在非思考模式下传。
        if not use_thinking:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = tools

        # ★ 关闭 thinking 的唯一有效写法
        if not use_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        # ---- 回放优先：如果这个请求被录过，直接用录制结果 ----
        if self.recorder is not None:
            cached = self.recorder.lookup(tag=tag, model=use_model, messages=messages)
            if cached is not None:
                return self._build_result(cached, from_replay=True)

        started = time.perf_counter()
        resp = self._client.chat.completions.create(**kwargs)
        elapsed_ms = (time.perf_counter() - started) * 1000
        raw = resp.model_dump()

        if self.recorder is not None:
            self.recorder.save(tag=tag, model=use_model, messages=messages, response=raw)

        return self._build_result(raw, elapsed_ms=elapsed_ms, from_replay=False)

    # ---------------------------------------------------------- 结果构造
    def _build_result(
        self, raw: dict, *, elapsed_ms: float = 0.0, from_replay: bool = False
    ) -> LlmResult:
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = raw.get("usage") or {}

        calls = [
            ToolCall(
                id=tc.get("id", ""),
                name=(tc.get("function") or {}).get("name", ""),
                arguments=(tc.get("function") or {}).get("arguments", "") or "",
            )
            for tc in (msg.get("tool_calls") or [])
        ]

        # ★ 用响应里的模型名去查单价（坑 3）
        model_used = raw.get("model") or ""
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        # 有些响应只给 prompt_tokens 而不拆缓存 —— 那就全部按未命中算（保守）
        if hit == 0 and miss == 0:
            miss = int(usage.get("prompt_tokens") or 0)

        cost = estimate_cost_yuan(
            model=model_used,
            hit_tokens=hit,
            miss_tokens=miss,
            output_tokens=int(usage.get("completion_tokens") or 0),
        )

        result = LlmResult(
            text=msg.get("content") or "",
            tool_calls=calls,
            finish_reason=choice.get("finish_reason") or "",
            usage=usage,
            model_used=model_used,
            cost_yuan=cost,
            elapsed_ms=elapsed_ms,
            pricing_known=model_used in PRICING_YUAN_PER_MTOK,
            raw=raw,
        )
        if from_replay:
            result.usage = {**result.usage, "__replayed__": 1}
        return result
