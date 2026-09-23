"""
D0 Spike 补充实验：thinking 与 reasoning_content 的完整对照矩阵。

============================ 为什么需要这个补充 ============================

第一个 spike（scripts/spike_deepseek.py）测出「不回传 reasoning_content 也能成功」，
但那次实验有一个设计漏洞：

    它在第 2 轮【关闭了 thinking】，而"必须回传 reasoning_content"这条规则
    很可能只在 thinking【开启】时才成立。

所以那个结论不完整。本脚本把四种组合全部跑一遍，补上缺失的格子。

    第2轮 thinking   是否回传 rc   意义
    --------------  ------------  ----------------------------------------
    A  关闭          否           已知成功（第一个 spike 已测）
    B  关闭          是           已知成功
    C  开启          否           ★ 关键：规则若成立，这里应该 400
    D  开启          是           ★ 关键：若 C 失败而 D 成功，规则即被证实

另外本脚本会完整打印 usage（不截断），用于建立成本模型：
我们想知道每一次调用的 token 究竟花在哪里。

运行方式（项目根目录）：

    python scripts/spike_deepseek_thinking_matrix.py

只新建文件，不删除、不修改任何已有文件。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from openai import OpenAI  # noqa: E402

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL = os.environ.get("RCA_MODEL_CHEAP", "deepseek-flash")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RECORD_DIR = PROJECT_ROOT / "recordings"
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")
RECORD_FILE = RECORD_DIR / f"thinking-matrix-{RUN_ID}.ndjson"

# 这是实验 3 验证出来的、唯一真正生效的关闭方式
DISABLE_THINKING = {"thinking": {"type": "disabled"}}

TOOLS = [{
    "type": "function",
    "function": {
        "name": "query_logs",
        "description": "查询指定服务的日志。",
        "parameters": {
            "type": "object",
            "properties": {"service": {"type": "string", "description": "服务名"}},
            "required": ["service"],
        },
    },
}]

USER_Q = "请查一下 order 服务的日志，看看有没有报错。"
TOOL_RESULT = "order.log 最近 5 分钟共 3 条 ERROR：Connection pool exhausted (等待 12000ms)"


def save(name: str, payload: dict) -> None:
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    with RECORD_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"name": name, "ts": time.time(), "payload": payload},
                            ensure_ascii=False) + "\n")


def section(t: str) -> None:
    print("\n" + "=" * 74)
    print(f"  {t}")
    print("=" * 74)


def run_case(client: OpenAI, label: str,
             turn1_disable: bool, turn2_disable: bool, echo_rc: bool) -> dict:
    """跑一次两轮工具调用，返回结果摘要。

    参数说明：
      turn1_disable : 第 1 轮是否关闭 thinking
      turn2_disable : 第 2 轮是否关闭 thinking
      echo_rc       : 第 2 轮是否把第 1 轮返回的 reasoning_content 原样带回去
    """
    out: dict = {"label": label, "turn1": None, "turn2": None,
                 "rc_in_turn1": None, "rc_len": 0}

    # ---------- 第 1 轮：用户提问 ----------
    kw1 = {"extra_body": DISABLE_THINKING} if turn1_disable else {}
    r1 = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": USER_Q}],
        tools=TOOLS, max_tokens=512, **kw1,
    )
    d1 = r1.model_dump()
    save(f"{label}_turn1", d1)
    msg1 = d1["choices"][0]["message"]
    out["turn1"] = "ok"
    out["rc_in_turn1"] = msg1.get("reasoning_content") is not None
    out["rc_len"] = len(msg1.get("reasoning_content") or "")
    out["usage_turn1"] = d1.get("usage")

    tool_calls = msg1.get("tool_calls") or []
    if not tool_calls:
        out["turn1"] = "no_tool_call"
        return out
    tc = tool_calls[0]

    # ---------- 组装第 2 轮的对话历史 ----------
    assistant_msg: dict = {
        "role": "assistant",
        "content": msg1.get("content"),
        "tool_calls": msg1.get("tool_calls"),
    }
    if echo_rc and msg1.get("reasoning_content") is not None:
        assistant_msg["reasoning_content"] = msg1["reasoning_content"]

    messages = [
        {"role": "user", "content": USER_Q},
        assistant_msg,
        {"role": "tool", "tool_call_id": tc.get("id"), "content": TOOL_RESULT},
    ]

    # ---------- 第 2 轮 ----------
    kw2 = {"extra_body": DISABLE_THINKING} if turn2_disable else {}
    try:
        r2 = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, max_tokens=512, **kw2,
        )
        d2 = r2.model_dump()
        save(f"{label}_turn2", d2)
        out["turn2"] = "ok"
        out["usage_turn2"] = d2.get("usage")
    except Exception as e:
        out["turn2"] = f"{type(e).__name__}"
        out["turn2_error"] = str(e)[:300]

    return out


def fmt_usage(u: dict | None) -> str:
    """把 usage 压成一行，重点看 reasoning_tokens 和缓存字段。"""
    if not u:
        return "（无）"
    ctd = (u.get("completion_tokens_details") or {})
    ptd = (u.get("prompt_tokens_details") or {})
    parts = [
        f"in={u.get('prompt_tokens')}",
        f"out={u.get('completion_tokens')}",
        f"reasoning={ctd.get('reasoning_tokens')}",
    ]
    hit = u.get("prompt_cache_hit_tokens")
    miss = u.get("prompt_cache_miss_tokens")
    if hit is not None or miss is not None:
        parts.append(f"cache_hit={hit}")
        parts.append(f"cache_miss={miss}")
    # 其余非空字段也带上，帮助建立成本模型
    for k, v in ptd.items():
        if v not in (None, 0):
            parts.append(f"pd.{k}={v}")
    return "  ".join(parts)


# ==================================================================

section("预检")
if not API_KEY:
    print("  ❌ 没读到 DEEPSEEK_API_KEY，重开终端或从注册表读取")
    sys.exit(2)
print(f"  ✅ Key 长度 {len(API_KEY)}")
print(f"  ✅ 模型 {MODEL}")
print(f"  ✅ 录制 {RECORD_FILE.relative_to(PROJECT_ROOT)}")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

CASES = [
    # label                      turn1关闭  turn2关闭  回传rc
    ("A 两轮都关thinking + 不回传",  True,   True,    False),
    ("B 两轮都关thinking + 回传",    True,   True,    True),
    ("C ★第2轮开thinking + 不回传", False,  False,   False),
    ("D ★第2轮开thinking + 回传",   False,  False,   True),
]

results = []
for label, t1d, t2d, echo in CASES:
    section(f"用例 {label}")
    try:
        res = run_case(client, label.split()[0], t1d, t2d, echo)
        results.append(res)
        print(f"  第1轮：{res['turn1']}"
              f"   | 第1轮有 reasoning_content: {res['rc_in_turn1']}"
              f"（长度 {res['rc_len']}）")
        print(f"  第1轮 usage: {fmt_usage(res.get('usage_turn1'))}")
        print(f"  第2轮：{res['turn2']}")
        if res.get("turn2_error"):
            print(f"  第2轮错误：{res['turn2_error']}")
        else:
            print(f"  第2轮 usage: {fmt_usage(res.get('usage_turn2'))}")
    except Exception as e:
        print(f"  ❌ 用例异常：{type(e).__name__}: {str(e)[:200]}")
        results.append({"label": label.split()[0], "turn1": "exception",
                        "turn2": f"{type(e).__name__}", "turn2_error": str(e)[:300]})


# ==================================================================

section("对照矩阵")

print(f"  {'用例':<28} {'第1轮rc':<8} {'第2轮':<22} 结论")
print("  " + "-" * 78)
for r in results:
    t2 = r.get("turn2", "?")
    rc = "有" if r.get("rc_in_turn1") else "无"
    verdict = ""
    if t2 == "ok":
        verdict = "成功"
    elif t2 and t2 != "ok":
        verdict = "❌ 失败 —— 规则被证实"
    print(f"  {r.get('label',''):<28} {rc:<8} {str(t2):<22} {verdict}")

print()

# ---- 逻辑判定 ----
by = {r.get("label"): r for r in results}
c_ok = by.get("C", {}).get("turn2") == "ok"
d_ok = by.get("D", {}).get("turn2") == "ok"

print("  🎯 判定：")
if not c_ok and d_ok:
    print("     ✅ 规则成立 —— 「thinking 开启时，第 2 轮必须回传 reasoning_content」。")
    print("        影响：消息适配层必须显式保留该字段，否则多 Agent 打通时会大面积 400。")
    print("        对策：把「保留 reasoning_content」做成消息类型的一部分，不靠调用方记得。")
elif c_ok:
    print("     ⚠️  规则不成立 —— 即使 thinking 开启、不回传 rc，也能成功。")
    print("        影响：D7 不会因此返工。但仍应保留该字段用于成本核算与调试。")
else:
    print("     ⚠️  判定不明确，需要看上面的错误信息。")

print()
print(f"  原始响应已录到 {RECORD_FILE.relative_to(PROJECT_ROOT)}")
print()
