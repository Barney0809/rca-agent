"""
D6 验收：三个专职 Agent 的职责边界与信息隔离。

需求 FR-1.3：**三个专职 Agent，每个只能访问自己的数据源。**

============================ 为什么隔离是"必须"而不是"最好" ============================

隔离有一个反面：如果每个 Agent 都能看到全部数据，那"多 Agent"就只是
**同一份数据问三遍** —— 成本三倍、信息零增益。

只有让每个 Agent **客观上存在盲区**，协作才产生真实价值：
    每个 Agent 的结论都是**不完整的**，所以必须互相质证。

所以本文件的用例盯住的是"**边界真的存在**"，而不只是"prompt 里写了"。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from rca.agents.coordinator import build_cross_exam_message
from rca.agents.roles import ALL_ROLES, CHANGE_AGENT, LOGS_AGENT, METRICS_AGENT
from rca.agents.specialist import Hypothesis
from rca.telemetry.models import ReducedView
from rca.tools import RestrictedToolBox, RunContext, ToolBox


@pytest.fixture
def empty_ctx() -> RunContext:
    """一个不需要真实数据的上下文（本文件只测工具边界，不测内容）。"""
    return RunContext(
        run_id="t-test",
        log_view=ReducedView(window_start=None, window_end=None, total_lines=0),
        metrics={},
        changes=[],
        scenario={},
    )


# ================================================================
# 角色定义本身的正确性
# ================================================================

def test_each_role_has_exactly_one_tool():
    """每个角色只能有一个工具 —— 这是"专职"的定义。"""
    for role in ALL_ROLES:
        assert role.tool, f"{role.key} 没有声明工具"
        assert role.duty, f"{role.key} 没有声明职责"
        assert len(role.system_prompt) > 200, f"{role.key} 的 prompt 太短，可能没写清楚职责"


def test_roles_cover_the_three_data_sources_exactly():
    """三个角色的工具合起来，恰好覆盖三路数据源 —— 不多也不少。

    **多了**：说明有 Agent 越界（那隔离就失效了）
    **少了**：说明有一路数据没人看（那结论必然片面）
    """
    tools = sorted(r.tool for r in ALL_ROLES)
    assert tools == ["get_changes", "query_logs", "query_metrics"], (
        f"三个角色的工具应当恰好是三路数据源各一个，实际：{tools}"
    )


def test_all_roles_require_needs_from_others():
    """每个角色的输出契约里都必须有 needs_from_others。

    隔离会让每个 Agent 证据不全；**如果不强制它说出自己缺什么**，
    隔离就只是把能力削掉了，而不是制造协作。
    """
    for role in ALL_ROLES:
        assert "needs_from_others" in role.system_prompt, (
            f"{role.key} 的输出契约里缺 needs_from_others —— "
            f"那它只会给一个不完整的结论，而不会说「我需要什么」"
        )


# ================================================================
# 隔离在代码层真的生效
# ================================================================

def test_specs_only_expose_the_allowed_tool(empty_ctx: RunContext):
    """**模型根本看不到别的工具** —— 这是第一道防线。"""
    box = RestrictedToolBox(empty_ctx, frozenset({"query_metrics"}), role="metrics")
    names = [s["function"]["name"] for s in box.specs()]
    assert names == ["query_metrics"], f"暴露了不该暴露的工具：{names}"


@pytest.mark.parametrize(
    "role,forbidden",
    [
        (METRICS_AGENT, ["query_logs", "get_changes"]),
        (LOGS_AGENT, ["query_metrics", "get_changes"]),
        (CHANGE_AGENT, ["query_logs", "query_metrics"]),
    ],
)
def test_cross_role_calls_are_denied(empty_ctx: RunContext, role, forbidden):
    """**第二道防线**：即便模型硬要调别的工具，也会被明确拒绝。

    为什么必须有第二道：prompt 是**建议**，不是约束。
    模型完全可能"顺手"调一个它看到的工具名 —— 而它不该能调通。

    这与项目的核心命题一致：**不靠自觉，靠机制。**
    """
    box = RestrictedToolBox(empty_ctx, frozenset({role.tool}), role=role.key)
    for bad in forbidden:
        out = box.call(bad, "{}")
        assert "拒绝" in out, f"{role.key} 调 {bad} 竟然没被拒绝：{out[:80]}"
        assert role.tool in out, "拒绝信息里应当告诉它可以用什么"
    assert len(box.denials) == len(forbidden)


def test_denial_is_recorded_not_silent(empty_ctx: RunContext):
    """越权尝试必须被记录 —— 不许静默失败。

    这是从 harness-log #4（4xx 被吞）和 #9（授权静默失效）学到的：
    **被拒绝这件事本身是信息，不能丢。**
    """
    box = RestrictedToolBox(empty_ctx, frozenset({"query_metrics"}), role="metrics")
    box.call("query_logs", '{"level": "ERROR"}')

    assert box.denials == ["query_logs"]
    assert empty_ctx.tool_calls == 1, "越权尝试也应当计入工具调用统计"
    assert empty_ctx.tool_log[-1]["ok"] is False, "越权尝试应当标记为失败"


def test_allowed_call_goes_through(empty_ctx: RunContext):
    """正向对照：允许的工具必须真的能调通。

    没有这条，上面的"拒绝"断言可能是"所有调用都被拒"造成的假绿。
    """
    box = RestrictedToolBox(empty_ctx, frozenset({"query_metrics"}), role="metrics")
    out = box.call("query_metrics", "{}")
    assert "拒绝" not in out
    assert empty_ctx.tool_log[-1]["ok"] is True
    assert box.denials == []


def test_base_toolbox_has_no_restriction(empty_ctx: RunContext):
    """对照：基类 ToolBox（baseline 用的）**不做任何限制**。

    这保证了对照实验的公平性：baseline 拿到的是完整工具面，
    多 Agent 拿到的是被切分的工具面。**变量只有协作结构。**
    """
    box = ToolBox(empty_ctx)
    names = sorted(s["function"]["name"] for s in box.specs())
    assert names == ["get_changes", "query_logs", "query_metrics"]


def test_fork_gives_independent_counters(empty_ctx: RunContext):
    """fork() 必须给出独立计数器，否则并发跑的三个 Agent 统计会互相污染。"""
    a = empty_ctx.fork()
    b = empty_ctx.fork()

    a.tool_calls = 5
    a.tool_log.append({"tool": "x"})

    assert b.tool_calls == 0, "分叉后的计数器应当互不影响"
    assert b.tool_log == []
    assert empty_ctx.tool_calls == 0, "分叉也不应影响原上下文"

    # 底层数据是共享的（读引用）
    assert a.run_id == b.run_id == empty_ctx.run_id


def test_baseline_module_is_frozen():
    """元测试：`baseline.py` 不允许被**随意**改动。

    它是 D5 数字的分母。若有人为了"统一代码风格"重构它，
    它的行为可能变化，**docs/05 的数字就失效了**。

    这条用例用内容指纹守住它：文件一旦改动就会变红，
    迫使改动者显式更新指纹**并重新跑一遍 baseline**。

    ── 指纹变更历史（每次都必须在这里留下理由）──

    `c5cad8da8af3d838` → `f2a1f143d5bb88fe`（2026-09-25）

        原因：**改任务定义**。原来的任务只问"根因"（单数、一句话），
        导致多故障场景（F8）里"只答出一个"被判不完整 ——
        那是拿一个它没被交付的任务去考它（`docs/06` §四）。

        改成了"列出这段时间内**所有**异常现象及其根本原因"：
          · `SYSTEM_PROMPT` 新增原则 5（可能同时存在多件事）+ 输出契约
            的 `root_cause`（字符串）改成 `root_causes`（列表）
          · `TASK_PROMPT` 明确要求分别列出、只报一个算不完整
          · 新增 `_parse_root_causes()`：**新旧两种格式都认** ——
            回放存档里存的是旧格式，只认新格式会让历史录像全部解析失败，
            那会让"改了任务"看起来像"模型变差了"

        ⇒ **docs/05 的全部数字随之作废，已重跑并更新。**

    `f2a1f143d5bb88fe` → `ba2fe0cb321bb615`（2026-09-25，D9 收尾）

        原因：**加了一次性的「JSON 催促」**（见 `src/rca/agents/contract.py`）。

        新任务下 JSON 解析成功率从 100% 掉到 **67%**（21 次里 2 次没给出干净 JSON，
        其中一次是一整段英文散文）。而解析失败时评分只能拿**未经约束的原始文本**
        去判 —— 那里面混着模型的**推理过程**而非**结论**，
        于是**测量口径会悄悄变松**（#16/#21 两次假阳性的根源正是这个）。

        做法：抠不出 JSON 时先催一次"请只输出 JSON 对象"，最多一次。
        三个调用点（baseline / 专职 Agent / 交叉质证）共用 `contract.py` 里的同一份逻辑。

        ⇒ **docs/05 重新测量并更新。**
    """
    import hashlib

    path = Path(__file__).resolve().parent.parent / "src" / "rca" / "agents" / "baseline.py"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    expected = "ba2fe0cb321bb615"

    assert digest == expected, (
        f"baseline.py 被改动了！\n"
        f"  期望指纹 {expected}\n"
        f"  实际指纹 {digest}\n"
        f"baseline 是对照实验的分母，改动它会让 docs/05 的数字失效。\n"
        f"若确实需要改，请：① 更新本用例里的指纹**并在此写清理由**；"
        f"② 重新跑一遍 baseline 并更新 docs/05。"
    )


# ================================================================
# 第二轮：交叉质证的 prompt 渲染
# ================================================================
#
# 真实缺陷：`CROSS_EXAM_PROMPT` 里含有 JSON 输出模板，
#   `str.format` 会把模板里的 `{` `}` 当成占位符，
#   直接抛 `KeyError: '\n  "revised_claim"'`。
#
# 为什么这条缺陷值得单独立一组用例：
#   它是**只有真跑起来才会暴露**的错误（渲染发生在发请求之前，但没人单向测过渲染）。
#   修好之后，如果只用"人工记得不要用 format"来保证，那它就是一条**靠自觉**的约束——
#   正是本项目要消灭的东西。
#
# 所以这里做两件事：
#   1. 让渲染变成可被直接调用的纯函数，用例**走真实代码路径**（行为面）；
#   2. 用 AST 钉住"生产代码确实在调用那个函数"（结构面）。
#      否则有人把渲染内联回 cross_examine，行为用例仍全绿，而生产路径已悄悄分叉。


def _mk_hypothesis(role: str, name: str, claim: str, evidence: list[str]) -> Hypothesis:
    return Hypothesis(role=role, name=name, claim=claim, evidence=evidence)


def test_cross_exam_message_renders_colleagues_and_keeps_json_contract() -> None:
    """渲染必须既塞进同事结论，又原样保住 JSON 输出模板。"""
    own = _mk_hypothesis("logs", "LogsAgent", "我看到下游超时", ["order 报 502"])
    others = [
        _mk_hypothesis("metrics", "MetricsAgent", "连接池被占满", ["pool_in_use=64"]),
        _mk_hypothesis("change", "ChangeAgent", "有人改了池大小", ["pool 64->2"]),
    ]

    msg = build_cross_exam_message(own, others)

    # 三个人的结论都必须在场（含自己的）
    assert "我看到下游超时" in msg
    assert "连接池被占满" in msg
    assert "有人改了池大小" in msg

    # 占位符必须被吃掉
    assert "{colleagues}" not in msg

    # JSON 输出模板必须**一个字符都没被动过** —— 这是原来炸掉的地方
    for key in ("revised_claim", "evidence_against", "why_changed", "falsifies"):
        assert f'"{key}"' in msg, f"JSON 契约字段 {key} 丢了"


def test_cross_exam_message_survives_braces_in_colleague_claims() -> None:
    """同事结论里自带 `{}` 时也不能炸。

    这不是假想：LLM 很爱在结论里直接贴 JSON 片段。
    只要渲染走的是 replace（而不是 format），插入的文本就是纯字面量，天然免疫。
    """
    own = _mk_hypothesis("logs", "LogsAgent", "我贴一段原始 JSON：{\"code\": 502}", [])
    others = [
        _mk_hypothesis(
            "metrics",
            "MetricsAgent",
            '指标快照 {"pool_in_use": 64, "pool_limit": 2} 说明池被打满',
            ["leak_bytes_total=1"],
        )
    ]

    msg = build_cross_exam_message(own, others)   # 不许抛异常

    assert '{"code": 502}' in msg
    assert '{"pool_in_use": 64, "pool_limit": 2}' in msg


def test_cross_exam_prompt_is_never_rendered_with_str_format() -> None:
    """结构面：模块里不许对 CROSS_EXAM_PROMPT 调用 `.format(...)`。

    只钉这一个对象，不做全模块的 `.format` 禁令，避免误伤无关代码。
    """
    tree = _coordinator_ast()

    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "CROSS_EXAM_PROMPT"
    ]

    assert not offenders, (
        f"coordinator.py 第 {offenders} 行对 CROSS_EXAM_PROMPT 调用了 .format(...)。\n"
        f"prompt 里含 JSON 输出模板，其中的 {{ }} 会被当成占位符并抛 KeyError。\n"
        f"请改用 build_cross_exam_message()（内部是 str.replace）。"
    )


def test_cross_examine_actually_calls_the_tested_renderer() -> None:
    """结构面：`cross_examine` 必须调用 `build_cross_exam_message`。

    否则上面两条行为用例测的是"一个没人用的函数"，
    而真正发出去的 prompt 已经绕过了它 —— 测试照样全绿，缺陷照样存在。
    """
    tree = _coordinator_ast()

    target = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "cross_examine"
        ),
        None,
    )
    assert target is not None, "coordinator.py 里找不到 cross_examine"

    called = {
        node.func.id
        for node in ast.walk(target)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "build_cross_exam_message" in called, (
        "cross_examine 没有调用 build_cross_exam_message()。\n"
        "渲染逻辑一旦被内联回去，针对渲染的回归用例就再也覆盖不到生产路径了。"
    )


def _coordinator_ast() -> ast.Module:
    src = (
        Path(__file__).resolve().parent.parent
        / "src" / "rca" / "agents" / "coordinator.py"
    ).read_text(encoding="utf-8")
    return ast.parse(src)


# ================================================================
# 所有 client.chat(...) 调用点的参数必须与真实签名一致
# ================================================================
#
# 真实缺陷：`adjudicate()` 里写了 `max_tokens=2048`，
# 但 `DeepSeekClient.chat()` 的签名里**没有这个参数**（输出长度统一由 config 决定）。
#
# 为什么这条特别值得立一组用例：
#
#   它**不在代码写错的那一刻暴露，也不在任何单测里暴露** ——
#   因为没有任何单测会真的走到"裁决"这一步（那要发网络请求）。
#   它只在**真正跑完整个多 Agent 闭环**的时候才炸，
#   而此时三个专职 Agent 和全部交叉质证都已经跑完、**钱已经花了**。
#
#   实测就是这样：F2 跑到最后一步裁决才抛
#       TypeError: DeepSeekClient.chat() got an unexpected keyword argument 'max_tokens'
#
# 单测里"复现"这个场景要花钱，所以这里换一个更根本、而且**零成本**的做法：
#   不测某一次调用的行为，而是**静态检查全部调用点的参数名**。
#   这样"参数名写错 / 参数被删掉 / 传了位置参数"这一整类错误都会被抓住，
#   而不只是这一处。


def _chat_call_sites() -> list[tuple[Path, ast.Call]]:
    """全仓库所有 `xxx.chat(...)` 调用点。

    ⚠️ 只匹配 `Call(func=Attribute(attr="chat"))`。
    底层 SDK 的用法是 `client.chat.completions.create(...)`，
    它的 func.attr 是 `create`，因此**不会**被误匹配进来。
    """
    out: list[tuple[Path, ast.Call]] = []
    roots = [Path(__file__).resolve().parent.parent / d for d in ("src", "eval", "scripts")]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "chat"
                ):
                    out.append((path, node))
    return out


def test_every_chat_call_matches_the_client_signature() -> None:
    """结构面：所有 `chat(...)` 调用点的参数必须是签名里真实存在的。"""
    import inspect

    from rca.llm.provider import DeepSeekClient

    params = inspect.signature(DeepSeekClient.chat).parameters
    allowed = set(params)

    problems: list[str] = []

    for path, node in _chat_call_sites():
        where = f"{path.relative_to(Path(__file__).resolve().parent.parent)}:{node.lineno}"

        # chat() 是 keyword-only 的（签名里有裸 `*`），所以位置参数一律是错的
        if node.args:
            problems.append(f"{where} 传了 {len(node.args)} 个位置参数，但 chat() 是 keyword-only")

        for kw in node.keywords:
            if kw.arg is None:                       # **kwargs 展开，静态看不出来
                continue
            if kw.arg not in allowed:
                problems.append(
                    f"{where} 传了未知参数 `{kw.arg}`；"
                    f"chat() 只接受：{sorted(allowed)}"
                )

    assert not problems, (
        "以下 chat() 调用点的参数与真实签名不符。\n"
        "这类错误**单测抓不到**（单测不会真的发请求），"
        "只会在跑完整闭环、钱都花完之后，在最后一步才炸：\n  "
        + "\n  ".join(problems)
    )


def test_the_chat_signature_check_actually_found_call_sites() -> None:
    """元测试：确认上面不是"扫了 0 个调用点"造成的假绿。"""
    sites = _chat_call_sites()
    where = sorted(
        f"{p.name}:{n.lineno}" for p, n in sites
    )
    assert len(sites) >= 4, f"只扫到 {len(sites)} 个 chat() 调用点：{where}"
    assert any(p.name == "coordinator.py" for p, _ in sites), (
        "没扫到 coordinator.py 里的 chat() 调用点，路径规则可能错了"
    )


# ================================================================
# 任务定义改成"列出所有根因"之后，结论解析必须**新旧格式都认**
# ================================================================
#
# 2026-09-25：任务从"定位根本原因"（单数）改成"列出所有异常及其根因"，
# 输出契约从 `root_cause`（字符串）改成 `root_causes`（列表）。
#
# ⚠️ 为什么解析必须容错两种格式：
#   1. **回放（replay）存档里存的是旧格式** —— 只认新格式会让历史录像
#      全部解析失败，变成一堆 `parse_ok=False`。
#      那会让"改了任务"看起来像"模型变差了" —— 最容易误判的一种情况。
#   2. 模型本身也经常"记得旧格式"。
#
# 这组用例守的是：**改了契约之后，旧数据还认不认。**


def test_parse_root_causes_accepts_both_contracts():
    from rca.agents.baseline import _parse_root_causes

    assert _parse_root_causes({"root_causes": ["a", "b"]}) == ["a", "b"]
    # 旧契约：单数（回放存档里的形状）
    assert _parse_root_causes({"root_cause": "单个原因"}) == ["单个原因"]
    # 两种都在时以新契约优先
    assert _parse_root_causes({"root_causes": ["新"], "root_cause": "旧"}) == ["新"]


def test_parse_root_causes_tolerates_a_string_where_a_list_was_asked_for():
    """模型没写数组、写了一句话时也要能用。

    这不是假想：契约说 `root_causes` 是列表，模型经常给一句用分号连起来的话。
    直接 `str(...)` 会把它变成 `"['a', 'b']"` 这种带方括号的怪物字符串，
    评分时匹配不到任何关键词 —— 又一个"看起来跑通了、其实数据是坏的"。
    """
    from rca.agents.baseline import _parse_root_causes

    assert _parse_root_causes({"root_causes": "原因甲；原因乙"}) == ["原因甲", "原因乙"]
    # 切不开就整条当一条，绝不能丢
    assert _parse_root_causes({"root_causes": "就一个原因"}) == ["就一个原因"]


def test_parse_root_causes_handles_empty_and_missing():
    from rca.agents.baseline import _parse_root_causes

    assert _parse_root_causes({}) == []
    assert _parse_root_causes({"root_causes": []}) == []
    assert _parse_root_causes({"root_cause": "   "}) == []
    # 空串元素要被丢掉，不能留下空条目
    assert _parse_root_causes({"root_causes": ["  a  ", ""]}) == ["a"]


def test_baseline_joins_the_list_into_the_legacy_field():
    """`root_cause`（拼起来的文本）必须仍然被填上。

    评分与存档一直用这个字段；它一旦空了，
    所有场景都会因为"文本里没有关键词"而判错 —— 而且**看不出是为什么**。
    """
    import inspect

    from rca.agents import baseline as mod

    src = inspect.getsource(mod)
    assert '"；".join(diag.root_causes)' in src, (
        "Diagnosis.root_cause 必须由 root_causes 拼出来（评分/存档依赖它）"
    )


# ================================================================
# 输出契约补救：模型没吐 JSON 时催一次（D9 收尾）
# ================================================================
#
# 真实退步：任务改成"列出所有异常"之后，baseline 的 JSON 解析成功率
# 从 100% 掉到 **67%**（21 次里 2 次没给出干净 JSON，其中一次是一整段英文散文）。
#
# 为什么这不是"丢一次数据"那么简单：
#   解析失败时，评分只能拿**未经约束的原始文本**去判 ——
#   那里面混着模型的**推理过程**，而不是它的**结论**。
#   而"推理里提过某个词"与"结论里主张某件事"是两件事 ——
#   这正是 #16 / #21 两次假阳性的根源。
#   ⇒ **解析失败会让测量口径悄悄变松。**


def test_contract_detects_a_prose_answer_as_needing_repair():
    from rca.agents.contract import needs_json_repair

    assert needs_json_repair("I now have enough evidence. Issue A ...") is True
    assert needs_json_repair('{"root_causes": ["a"], "evidence": [], "confidence": 0.6}') is False


def test_contract_repair_messages_append_a_correction_turn():
    """催促必须**在原对话后面追加**两轮，而不是替换掉已有的上下文。

    替换会丢掉模型已经看到的全部证据 —— 那等于让它从头再来一遍。
    """
    from rca.agents.contract import JSON_REPAIR_NUDGE, repair_messages

    original = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    out = repair_messages(original, "我写了一段散文")

    assert out[: len(original)] == original, "原有消息必须原样保留"
    assert out[-2] == {"role": "assistant", "content": "我写了一段散文"}
    assert out[-1] == {"role": "user", "content": JSON_REPAIR_NUDGE}
    # 催促只看格式，不许暗示结论 —— 否则就成了"引导它答对"
    assert "根因" not in JSON_REPAIR_NUDGE
    assert "风控" not in JSON_REPAIR_NUDGE


def test_contract_repair_does_not_mutate_the_input():
    """不许原地改传入的列表 —— 调用方要能自由决定用不用它。"""
    from rca.agents.contract import repair_messages

    original = [{"role": "system", "content": "s"}]
    snapshot = list(original)
    repair_messages(original, "prose")
    assert original == snapshot


def test_all_three_call_sites_share_the_same_repair_logic():
    """三个调用点必须**共用同一份**逻辑，不许各写一遍。

    复制三份迟早会走散，而"两份判定不一致"这种 bug 极难发现
    （本项目已经因为同一类问题栽过一次 —— 见 eval/scenarios.py 的注释）。
    """
    from pathlib import Path as _P

    root = _P(__file__).resolve().parent.parent
    for rel in ("src/rca/agents/baseline.py",
                "src/rca/agents/specialist.py",
                "src/rca/agents/coordinator.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "from .contract import repair_messages" in src, f"{rel} 没接上 JSON 催促"
        assert "repair_messages(messages, result.text)" in src, f"{rel} 没用共享的补救函数"

# ================================================================
# D11 成本优化：给"输出大户"加确定性的输出上限
# ================================================================
#
# 实测依据（`scripts/cost_breakdown.py`，一次 multi 诊断的 33 次调用）：
#
#     缓存命中输入  2.0%   /   未命中输入  25.7%   /   **输出  72.3%**
#
# ⇒ 成本大头是**输出**，而最大的几次来自交叉质证与裁决
#   （曾见 crossexam/metrics 1626 tok、coordinator 1293 tok）。
# ⇒ 与其在提示词里"请求简洁"（靠自觉），不如给一个**确定性的上限**。


def test_chat_accepts_an_explicit_max_tokens():
    """`chat()` 必须支持显式 `max_tokens`。

    ⚠️ 与 #15 的区别：那次是**误传了一个不存在的参数**（修法是删掉），
       这次是**把它做成受支持的参数** —— 两件事，别混。
    """
    import inspect

    from rca.llm.provider import DeepSeekClient

    params = inspect.signature(DeepSeekClient.chat).parameters
    assert "max_tokens" in params, "chat() 需要支持显式输出上限"
    assert params["max_tokens"].default is None, "默认必须是 None（回落到配置）"


def test_output_heavy_stages_pass_a_cap():
    """交叉质证与裁决这两个"输出大户"必须显式传上限。

    结构性检查（不实际发请求）：读源码确认那两个调用点带了 `max_tokens=`。
    """
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parent.parent
           / "src" / "rca" / "agents" / "coordinator.py").read_text(encoding="utf-8")
    for tag in ('tag=f"crossexam/', 'tag="coordinator"'):
        i = src.index(tag)
        window = src[max(0, i - 400): i]
        assert "max_tokens=" in window, (
            f"{tag} 这个调用点没有输出上限 —— 它是实测的成本大头（输出占 72%）"
        )


def test_coordinator_cap_is_not_tighter_than_cross_exam():
    """裁决的上限**不能**比质证更紧。

    裁决要逐条写「为什么否掉」加 `dissent`，压太紧会让它写不全，
    从而**为了省几分钱换来更差的结论** —— 那就本末倒置了。
    """
    import re
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parent.parent
           / "src" / "rca" / "agents" / "coordinator.py").read_text(encoding="utf-8")

    def cap_before(tag: str) -> int:
        i = src.index(tag)
        m = re.findall(r"max_tokens=(\d+)", src[max(0, i - 400): i])
        assert m, f"{tag} 附近找不到 max_tokens"
        return int(m[-1])

    assert cap_before('tag="coordinator"') >= cap_before('tag=f"crossexam/')
