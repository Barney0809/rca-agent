"""
D0 Spike：验证 DeepSeek 的工具调用（Tool Calling）在多轮对话下能否正常工作。

============================ 为什么必须先做这个 ============================

我们的系统里，Agent 会反复调用工具（查日志、查指标、查变更），也就是说
「多轮对话 + 工具调用」是整条主链路的地基。

但我们从官方资料里查到一条警告：

    带 tools 的多轮对话，必须把上一轮返回的 reasoning_content 原样带回去，
    否则接口直接返回 400 错误。

如果这是真的，而它又只在"第二轮以后"才暴露，那么风险是：

    你会在 D7（多 Agent 打通）那天，才发现最底层的地基不成立 —— 那时
    已经写了七天的代码，改道成本极高。

所以今天先用一个 20 行就能写完的小脚本，把这个假设验证掉。
花 20 分钟，换掉"第七天返工"的风险。

============================ 给 Python 新手的说明 ============================

本脚本刻意写得很啰嗦，因为读它的人有 Java 背景但在学 Python。几个对应关系：

  Python                        Java 对应物
  ----------------------------  ----------------------------------------
  os.environ["KEY"]             系统属性 / 环境变量
  dict {"a": 1}                 HashMap / LinkedHashMap
  f"{x}" 字符串插值              String.format 或字符串拼接
  try / except                  try / catch
  def f(a: str) -> int:         方法签名（但类型注解只是"提示"，运行时不强制）
  import 模块                    import 包

运行方式（在项目根目录）：

    python scripts/spike_deepseek.py

它只会「新建」文件（把响应录下来），不会删除或修改任何已有文件。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Windows 控制台默认可能不是 UTF-8，中文输出会变成乱码。
# 这行强制把标准输出改成 UTF-8，避免我们看不懂自己的日志。
# （对应 AGENTS.md 第 1 条：编码问题必须先处理，不能等它咬人）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from openai import OpenAI  # noqa: E402

# ---------------------------------------------------------------- 基础配置

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL_CHEAP = os.environ.get("RCA_MODEL_CHEAP", "deepseek-flash")
MODEL_STRONG = os.environ.get("RCA_MODEL_STRONG", "deepseek-v4-pro")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RECORD_DIR = PROJECT_ROOT / "recordings"      # 已被 .gitignore 忽略
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")
RECORD_FILE = RECORD_DIR / f"spike-{RUN_ID}.ndjson"

# 用来记录每一个实验结果，最后统一打印总结表
RESULTS: list[tuple[str, str, str]] = []      # (实验名, 状态, 说明)


def record(label: str, status: str, detail: str = "") -> None:
    """把一个实验结果记下来。status 用符号做前缀，方便一眼扫过。"""
    RESULTS.append((label, status, detail))
    print(f"  {status} {label}" + (f"  —— {detail}" if detail else ""))


def save_raw(name: str, payload: dict) -> None:
    """把原始响应落盘（一行一个 JSON）。

    这是「录制」的雏形：录制下来之后，将来可以离线回放，
    让面试官不需要 API Key 也能复现我们的评测结果（需求 FR-C.3）。
    """
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    with RECORD_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"name": name, "ts": time.time(), "payload": payload},
                            ensure_ascii=False) + "\n")


def brief(obj, limit: int = 300) -> str:
    """把任意对象压成一行短字符串，方便打印。"""
    text = json.dumps(obj, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def section(title: str) -> None:
    print("\n" + "=" * 74)
    print(f"  {title}")
    print("=" * 74)


# ---------------------------------------------------------------- 预检

section("预检")

if not API_KEY:
    print("  ❌ 没有读到 DEEPSEEK_API_KEY")
    print()
    print("     如果你是刚设置的环境变量，需要【重开一个终端】才能生效。")
    print("     验证方法（PowerShell）：")
    print("       [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User').Length")
    sys.exit(2)

print(f"  ✅ API Key 已读到（长度 {len(API_KEY)}）")
print(f"  ✅ BASE_URL = {BASE_URL}")
print(f"  ✅ 录制文件 = {RECORD_FILE.relative_to(PROJECT_ROOT)}")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)


# ================================================================
# 实验 1：最小连通性 + 看看响应里的 model 字段是什么
# ================================================================
# 为什么要看 model 字段：
#   我们请求时写的是 deepseek-flash，但服务端有可能把请求路由到别的模型执行。
#   成本核算必须用【响应里返回的】模型名去查单价，用请求名会算错账。
# ----------------------------------------------------------------

section("实验 1：连通性 + 响应中的 model 字段")

resp1 = None
try:
    r = client.chat.completions.create(
        model=MODEL_CHEAP,
        messages=[{"role": "user", "content": "只回答两个字：收到"}],
        max_tokens=32,
    )
    resp1 = r.model_dump()
    save_raw("exp1_basic", resp1)

    used_model = resp1.get("model")
    choice = resp1["choices"][0]
    msg = choice["message"]
    usage = resp1.get("usage") or {}

    record("请求 deepseek-flash 成功", "✅")
    record("响应中的 model 字段", "📌", f"{used_model}（请求名={MODEL_CHEAP}）")
    record("回复内容", "📌", brief(msg.get("content")))
    record("finish_reason", "📌", str(choice.get("finish_reason")))
    record("usage 字段", "📌", brief(usage))
    record("message 里有哪些 key", "📌", ", ".join(sorted(msg.keys())))
except Exception as e:
    record("请求 deepseek-flash", "❌", f"{type(e).__name__}: {e}")


# ================================================================
# 实验 2：thinking 默认是不是开的？
# ================================================================
# 判断依据：如果响应 message 里出现了 reasoning_content 字段，
# 说明服务端默认走了「思考模式」——那就会产生推理 token，也就是要花钱。
# 我们的日志解析、摘要这类粗活不需要思考模式，必须能关掉。
# ----------------------------------------------------------------

section("实验 2：thinking 是否默认开启")

thinking_default = None
if resp1:
    msg = resp1["choices"][0]["message"]
    rc = msg.get("reasoning_content")
    thinking_default = rc is not None
    if thinking_default:
        record("默认是否返回 reasoning_content", "⚠️", f"返回了（长度 {len(rc or '')}）→ 默认开启思考模式")
    else:
        record("默认是否返回 reasoning_content", "✅", "未返回 → 默认未开启（与资料不符，以实测为准）")

    usage = resp1.get("usage") or {}
    record("usage 里的缓存字段", "📌",
           ", ".join(k for k in usage.keys() if "cache" in k.lower()) or "（无）")
else:
    record("实验 2", "⏭️", "跳过（实验 1 未成功）")


# ================================================================
# 实验 3：怎么关闭 thinking
# ================================================================
# OpenAI 官方 SDK 不认识 DeepSeek 的自定义参数，所以要塞进 extra_body
# （extra_body 就是「原样附加到请求体里」的意思）。
# 具体参数名我们不确定，所以逐个试，看哪个既成功又把 reasoning_content 去掉。
# ----------------------------------------------------------------

section("实验 3：关闭 thinking 的候选参数")

THINKING_CANDIDATES = [
    ("thinking={'type':'disabled'}", {"thinking": {"type": "disabled"}}),
    ("enable_thinking=False",        {"enable_thinking": False}),
    ("thinking=False",               {"thinking": False}),
]

working_disable: dict | None = None
working_label = ""

for label, body in THINKING_CANDIDATES:
    try:
        r = client.chat.completions.create(
            model=MODEL_CHEAP,
            messages=[{"role": "user", "content": "只回答两个字：收到"}],
            max_tokens=32,
            extra_body=body,
        )
        d = r.model_dump()
        save_raw(f"exp3_{label}", d)
        msg = d["choices"][0]["message"]
        has_rc = msg.get("reasoning_content") is not None
        status = "✅" if not has_rc else "⚠️"
        record(f"extra_body={label}", status,
               "请求成功且无 reasoning_content" if not has_rc
               else "请求成功但仍有 reasoning_content")
        if not has_rc and working_disable is None:
            working_disable, working_label = body, label
    except Exception as e:
        record(f"extra_body={label}", "❌", f"{type(e).__name__}: {str(e)[:160]}")

if working_disable is not None:
    record("可用的关闭方式", "🎯", working_label)
else:
    record("可用的关闭方式", "⚠️", "三个候选都不行 —— 需要进一步查文档")


# ================================================================
# 实验 4：单轮工具调用能不能用
# ================================================================
# 「工具调用」= 我们告诉模型「你有这些函数可以调」，模型不直接回答，
# 而是返回一个「我想调 query_logs，参数是 service='order'」的请求。
# 我们自己执行这个函数，把结果再喂回去，模型才给最终答案。
# ----------------------------------------------------------------

section("实验 4：单轮工具调用")

# 工具定义用 JSON Schema 描述参数。
# 对应 Java：有点像给方法写了参数校验注解，只不过这里是给模型看的。
TOOLS = [{
    "type": "function",
    "function": {
        "name": "query_logs",
        "description": "查询指定服务的日志。用于排查故障时获取该服务的日志片段。",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "服务名，例如 order / inventory / payment"},
                "keyword": {"type": "string", "description": "可选，按关键词过滤"},
            },
            "required": ["service"],
        },
    },
}]

USER_Q = "请查一下 order 服务的日志，看看有没有报错。"
messages: list[dict] = [{"role": "user", "content": USER_Q}]

tool_call_id = None
assistant_msg: dict | None = None

try:
    kwargs = {"extra_body": working_disable} if working_disable else {}
    r = client.chat.completions.create(
        model=MODEL_CHEAP, messages=messages, tools=TOOLS, max_tokens=256, **kwargs
    )
    d = r.model_dump()
    save_raw("exp4_single_turn_tool", d)
    assistant_msg = d["choices"][0]["message"]
    tool_calls = assistant_msg.get("tool_calls") or []

    if tool_calls:
        tc = tool_calls[0]
        tool_call_id = tc.get("id")
        record("模型返回了 tool_calls", "✅", f"函数={tc['function']['name']} 参数={tc['function']['arguments']}")
        record("tool_call_id", "📌", str(tool_call_id))
    else:
        record("模型返回了 tool_calls", "❌", "没有 —— 模型直接回答了，可能不支持或描述不清")
        record("模型实际回答", "📌", brief(assistant_msg.get("content")))
except Exception as e:
    record("单轮工具调用", "❌", f"{type(e).__name__}: {str(e)[:200]}")


# ================================================================
# 实验 5（关键）：多轮 —— 不回传 reasoning_content
# ================================================================
# 这是整个 spike 的核心。流程：
#   第 1 轮：用户提问 → 模型要求调工具
#   第 2 轮：我们把「工具的执行结果」追加进对话，再问一次
#
# 争议点在于：第 2 轮时，要不要把第 1 轮模型返回的 reasoning_content
# 也一起带回去？资料说必须带，不带就 400。
#
# 实验 5 = 故意不带，看看是不是真的报错。
# ================================================================

section("实验 5（关键）：多轮对话 —— 不回传 reasoning_content")

TOOL_RESULT = "order.log 最近 5 分钟共 3 条 ERROR：Connection pool exhausted (等待 12000ms)"
exp5_error = None

if assistant_msg and tool_call_id:
    # 组装第 2 轮的对话历史。
    # 注意这里【故意】不含 reasoning_content —— 只保留 role/content/tool_calls。
    assistant_without_rc = {
        "role": "assistant",
        "content": assistant_msg.get("content"),
        "tool_calls": assistant_msg.get("tool_calls"),
    }
    msgs_no_rc = [
        {"role": "user", "content": USER_Q},
        assistant_without_rc,
        {"role": "tool", "tool_call_id": tool_call_id, "content": TOOL_RESULT},
    ]
    try:
        kwargs = {"extra_body": working_disable} if working_disable else {}
        r = client.chat.completions.create(
            model=MODEL_CHEAP, messages=msgs_no_rc, tools=TOOLS, max_tokens=256, **kwargs
        )
        d = r.model_dump()
        save_raw("exp5_multi_no_rc", d)
        record("不回传 reasoning_content", "✅", "居然成功了 —— 资料说的 400 未复现")
        record("第 2 轮回答", "📌", brief(d["choices"][0]["message"].get("content")))
    except Exception as e:
        exp5_error = e
        record("不回传 reasoning_content", "⚠️", f"{type(e).__name__}: {str(e)[:220]}")
else:
    record("实验 5", "⏭️", "跳过（实验 4 没拿到 tool_call_id）")


# ================================================================
# 实验 6：多轮 —— 把 reasoning_content 一起回传
# ================================================================
# 如果实验 5 报错，这里应该能成功。两相对照就能确认那条规则。
# ================================================================

section("实验 6：多轮对话 —— 回传 reasoning_content")

if assistant_msg and tool_call_id:
    # 这次把 reasoning_content 原样带上（如果存在的话）
    assistant_with_rc = {
        "role": "assistant",
        "content": assistant_msg.get("content"),
        "tool_calls": assistant_msg.get("tool_calls"),
    }
    if assistant_msg.get("reasoning_content") is not None:
        assistant_with_rc["reasoning_content"] = assistant_msg["reasoning_content"]

    msgs_with_rc = [
        {"role": "user", "content": USER_Q},
        assistant_with_rc,
        {"role": "tool", "tool_call_id": tool_call_id, "content": TOOL_RESULT},
    ]
    try:
        kwargs = {"extra_body": working_disable} if working_disable else {}
        r = client.chat.completions.create(
            model=MODEL_CHEAP, messages=msgs_with_rc, tools=TOOLS, max_tokens=256, **kwargs
        )
        d = r.model_dump()
        save_raw("exp6_multi_with_rc", d)
        record("回传 reasoning_content", "✅", "成功")
        record("第 2 轮回答", "📌", brief(d["choices"][0]["message"].get("content")))
    except Exception as e:
        record("回传 reasoning_content", "❌", f"{type(e).__name__}: {str(e)[:220]}")
else:
    record("实验 6", "⏭️", "跳过（实验 4 没拿到 tool_call_id）")


# ================================================================
# 实验 7：强模型是否也可用
# ================================================================
# 我们的架构是「模型路由」：粗活用便宜模型，根因推理用强模型。
# 所以强模型的名字也必须验证能通。
# ================================================================

section("实验 7：强模型（根因推理用）是否可用")

try:
    r = client.chat.completions.create(
        model=MODEL_STRONG,
        messages=[{"role": "user", "content": "只回答两个字：收到"}],
        max_tokens=64,
    )
    d = r.model_dump()
    save_raw("exp7_strong_model", d)
    record(f"请求 {MODEL_STRONG}", "✅", f"响应 model={d.get('model')}")
except Exception as e:
    record(f"请求 {MODEL_STRONG}", "❌", f"{type(e).__name__}: {str(e)[:200]}")


# ================================================================
# 总结
# ================================================================

section("总结")

print(f"  {'实验':<34} {'状态':<4} 说明")
print("  " + "-" * 70)
for label, status, detail in RESULTS:
    print(f"  {label:<34} {status:<4} {detail}")

print()
print(f"  原始响应已录到：{RECORD_FILE.relative_to(PROJECT_ROOT)}")
print("  （该目录已被 .gitignore 忽略，不会进版本控制）")
print()

# 给出明确的下一步判断
if exp5_error is not None:
    print("  🎯 结论：资料属实 —— 多轮工具调用必须回传 reasoning_content。")
    print("     → 影响：消息适配层必须显式保留该字段，否则 D7 一定返工。")
elif assistant_msg and tool_call_id:
    print("  🎯 结论：不回传 reasoning_content 也能成功（资料未复现）。")
    print("     → 但仍建议保留该字段：成本核算与调试都需要看到推理内容。")
else:
    print("  ⚠️  结论未定：工具调用本身没跑通，先解决实验 4。")

print()
