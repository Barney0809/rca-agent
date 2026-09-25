"""ops 工具的用例：**命题的行动面到底有没有落点**（FR-2.1）。

============================ 这个文件在验什么 ============================

在 D15 接线之前，`src/rca/policy/` 是一个**建好了却没人调用**的包 ——
只有 `tests/test_policy.py` import 它。于是"任何不可逆操作都必须经过
独立于模型判断的确定性边界"这句话，在**代码路径上**是没有落点的。

这个文件验的是**接线之后的性质**，而不是策略引擎本身的性质
（引擎的性质在 `tests/test_policy.py`，那是 D4 就验过的 27 条）：

  1. **默认拒绝真的发生在"动作层"** —— 不只是"判定说不行"，
     而是**文件确实还在**。判定与后果必须一起验，否则"拒绝"只是句好话。
  2. **给了授权也不是抹掉** —— 移入隔离区，而且能原样还原（字节一致）。
  3. **路径越界优先于授权** —— 就算你有授权，也别想删到白名单外面去。
  4. **Agent 不能给自己发授权** —— 这是 deny-first 最容易被做错的一点：
     只要暴露一个 grant 工具，"默认拒绝"就名存实亡。
  5. **每一次判定都进审计**（允许的、拒绝的都要）—— 没有静默失败。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from rca.policy import PolicyEngine, Verb
from rca.tools_ops import OpsToolBox, ops_tool_names

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def workspace() -> Path:
    """唯一工作区。**不清理**（与 test_policy.py 一致：证据留痕优先）。"""
    ws = ROOT / "runs" / "_ops_tests" / f"t-{uuid.uuid4().hex[:8]}"
    (ws / "allowed").mkdir(parents=True)
    (ws / "quarantine").mkdir(parents=True)
    (ws / "outside").mkdir(parents=True)
    return ws


@pytest.fixture
def engine(workspace: Path) -> PolicyEngine:
    return PolicyEngine(
        allowed_roots=[workspace / "allowed"],
        quarantine_root=workspace / "quarantine",
        audit_path=workspace / "audit.ndjson",
        quarantine_ttl_s=3600,
    )


@pytest.fixture
def ops(engine: PolicyEngine) -> OpsToolBox:
    return OpsToolBox(engine, actor="operator", trace_id="t-ops")


def _audit_lines(workspace: Path) -> list[dict]:
    p = workspace / "audit.ndjson"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# 1. 默认拒绝：判定 + **后果**都要验
# --------------------------------------------------------------------------- #
def test_delete_is_denied_by_default_and_the_file_is_still_there(
    ops: OpsToolBox, workspace: Path
) -> None:
    target = workspace / "allowed" / "evidence.json"
    target.write_text("关键证据", encoding="utf-8")

    res = ops.delete_artifact(str(target))

    assert res.allowed is False
    assert "默认拒绝" in res.reason, f"拒绝理由没说清是 deny-first：{res.reason}"
    assert res.suggestion, "拒绝必须带一条可执行的建议（否则模型只能瞎猜）"
    assert target.exists(), "★ 判定说拒绝了，但文件没了 —— 这才是事故"
    assert target.read_text(encoding="utf-8") == "关键证据"


def test_denial_is_written_to_the_audit_log(ops: OpsToolBox, workspace: Path) -> None:
    target = workspace / "allowed" / "a.json"
    target.write_text("x", encoding="utf-8")

    ops.delete_artifact(str(target))

    lines = _audit_lines(workspace)
    assert lines, "拒绝也必须记账 —— 没有静默失败"
    assert any(ln.get("allowed") is False for ln in lines)


# --------------------------------------------------------------------------- #
# 2. 有授权也不抹掉：进隔离区，且能原样还原
# --------------------------------------------------------------------------- #
def test_delete_with_a_grant_goes_to_quarantine_and_restores_byte_identical(
    ops: OpsToolBox, engine: PolicyEngine, workspace: Path
) -> None:
    target = workspace / "allowed" / "keep-me.json"
    payload = '{"答案": "42"}'
    target.write_text(payload, encoding="utf-8")

    grant = engine.grant(verb=Verb.DELETE, path_prefix=workspace / "allowed", ttl_s=60)
    res = ops.delete_artifact(str(target), grant_id=grant.grant_id, note="测试")

    assert res.allowed is True, res.reason
    assert res.quarantine_id, "必须返回隔离区 id —— 否则没法还原"
    assert not target.exists(), "原始位置应当已经空了"
    assert "隔离区" in res.reason and "还原" in res.reason

    back = ops.restore_artifact(res.quarantine_id)

    assert back.allowed is True, back.reason
    assert target.exists(), "还原后应当回到原位"
    assert target.read_text(encoding="utf-8") == payload, "还原必须是**字节一致**的"


def test_a_grant_does_not_cover_a_path_outside_its_prefix(
    ops: OpsToolBox, engine: PolicyEngine, workspace: Path
) -> None:
    """授权是"某个动词 @ 某段前缀"，不是一张空白通行证。"""
    inside = workspace / "allowed"
    outside = workspace / "outside" / "victim.json"
    outside.write_text("别动我", encoding="utf-8")

    grant = engine.grant(verb=Verb.DELETE, path_prefix=inside, ttl_s=60)
    res = ops.delete_artifact(str(outside), grant_id=grant.grant_id)

    assert res.allowed is False
    assert "未覆盖" in res.reason or "授权范围" in res.reason, res.reason
    assert outside.exists() and outside.read_text(encoding="utf-8") == "别动我"


# --------------------------------------------------------------------------- #
# 3. 路径越界优先于授权（顺序不能换）
# --------------------------------------------------------------------------- #
def test_path_escape_is_refused_even_with_a_delete_grant(
    ops: OpsToolBox, engine: PolicyEngine, workspace: Path
) -> None:
    outside = workspace / "outside" / "config.json"
    outside.write_text("重要配置", encoding="utf-8")
    # 授权范围**尽可能宽**：连工作区根都授权了 —— 越界依然要被拦
    grant = engine.grant(verb=Verb.DELETE, path_prefix=workspace, ttl_s=60)

    res = ops.delete_artifact(
        str(workspace / "allowed" / ".." / ".." / "outside" / "config.json"),
        grant_id=grant.grant_id,
    )

    assert res.allowed is False, "带 `..` 的路径不得因为拿到授权就放行"
    assert outside.exists() and outside.read_text(encoding="utf-8") == "重要配置"


# --------------------------------------------------------------------------- #
# 4. Agent 不能给自己发授权
# --------------------------------------------------------------------------- #
def test_the_ops_toolbox_cannot_create_a_grant(ops: OpsToolBox) -> None:
    """deny-first 最容易被做错的一点：只要暴露一个"申请授权"的工具，它就名存实亡。

    `PolicyEngine.grant()` 的注释写着"授权只能由人/测试发"。
    这里从**接口层**再确认一次：ops 工具箱里没有任何能造出授权的方法，
    而且三个工具名里也不含 grant 语义。
    """
    assert not hasattr(ops, "grant"), "ops 工具箱暴露了 grant —— 门就白装了"

    creatable = [
        name for name in dir(ops)
        if not name.startswith("_") and callable(getattr(ops, name))
        and "grant" in name.lower()
    ]
    assert not creatable, f"这些方法名带 grant 语义，需要人工确认：{creatable}"

    assert ops_tool_names() == {"set_knobs", "delete_artifact", "restore_artifact"}
    assert not any("grant" in n for n in ops_tool_names())


# --------------------------------------------------------------------------- #
# 5. 可逆动作：放行，但**记账**
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """假的世界：只记录被调了什么，不真的发请求。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict | None = None, timeout: float | None = None):
        self.calls.append((url, dict(json or {})))
        return _FakeResponse({"ok": True})

    def get(self, url: str, timeout: float | None = None):
        return _FakeResponse({})

    def close(self) -> None:
        return None


def test_set_knobs_is_allowed_but_audited(engine: PolicyEngine, workspace: Path) -> None:
    fake = _FakeClient()
    ops = OpsToolBox(
        engine,
        world={"order": "http://fake-order"},
        actor="operator",
        client=fake,  # type: ignore[arg-type]
    )

    res = ops.set_knobs("order", {"risk_latency_ms": 800})

    assert res.allowed is True, res.reason
    assert fake.calls == [("http://fake-order/_inject", {"risk_latency_ms": 800})]
    lines = _audit_lines(workspace)
    assert lines and lines[-1].get("allowed") is True, "放行也要记账"


def test_set_knobs_on_an_unknown_service_is_refused(engine: PolicyEngine) -> None:
    ops = OpsToolBox(engine, world={"order": "http://fake-order"},
                     client=_FakeClient())  # type: ignore[arg-type]

    res = ops.set_knobs("nope", {"a": 1})

    assert res.allowed is False
    assert "未知服务" in res.reason


# --------------------------------------------------------------------------- #
# 6. 未登记的工具按最高等级处理（"漏登记"不能变成"什么都能做"）
# --------------------------------------------------------------------------- #
def test_an_unregistered_tool_name_is_treated_as_the_most_dangerous(engine: PolicyEngine) -> None:
    from rca.policy import verb_for_tool

    assert verb_for_tool("ops_something_new") == Verb.FORCE
    d = engine.authorize(tool="ops_something_new")
    assert d.allowed is False, "未登记工具必须默认拒绝，而不是默认放行"


# --------------------------------------------------------------------------- #
# 7. 授权要能跨进程（D15 实测出来的缺口：原本只在内存里）
# --------------------------------------------------------------------------- #
def test_a_grant_survives_a_new_engine_instance(workspace: Path) -> None:
    """「人先签授权，再执行动作」是**两个进程** —— 授权必须落盘。

    现场（D15 第一次跑 `scripts/ops.py` 的端到端流程）：
    授权签出来了，下一条命令却报"授权无效或已过期" ——
    其实是**根本没读到**（只存在内存里）。
    这种"看起来像过期、实际是被丢掉"的失败，正是本项目一直在防的静默失败。
    """
    allowed = workspace / "allowed"
    store = workspace / "grants" / "grants.ndjson"  # 刻意放在授权根**之外**
    audit = workspace / "audit.ndjson"
    qroot = workspace / "quarantine"

    first = PolicyEngine(allowed_roots=[allowed], quarantine_root=qroot,
                         audit_path=audit, grant_store=store)
    g = first.grant(verb=Verb.DELETE, path_prefix=allowed, ttl_s=300)
    assert store.exists(), "授权没有落盘 —— 下一个进程就读不到"

    second = PolicyEngine(allowed_roots=[allowed], quarantine_root=qroot,
                          audit_path=audit, grant_store=store)
    d = second.authorize(tool="delete_artifact", path=str(allowed / "x.json"),
                         grant_id=g.grant_id)

    assert d.allowed is True, f"新引擎没读到授权：{d.reason}"


def test_an_expired_persisted_grant_is_not_honoured(workspace: Path) -> None:
    """落盘不等于永久有效 —— TTL 仍然说了算（否则"一次性例外"变成"永久后门"）。"""
    allowed = workspace / "allowed"
    store = workspace / "grants" / "grants.ndjson"
    now = [1000.0]
    first = PolicyEngine(allowed_roots=[allowed], quarantine_root=workspace / "q",
                         audit_path=workspace / "a.ndjson", grant_store=store,
                         clock=lambda: now[0])
    g = first.grant(verb=Verb.DELETE, path_prefix=allowed, ttl_s=10)

    now[0] += 11  # 时间往前走，授权过期
    second = PolicyEngine(allowed_roots=[allowed], quarantine_root=workspace / "q",
                          audit_path=workspace / "a.ndjson", grant_store=store,
                          clock=lambda: now[0])
    d = second.authorize(tool="delete_artifact", path=str(allowed / "x.json"),
                         grant_id=g.grant_id)

    assert d.allowed is False


def test_the_grant_store_may_not_live_inside_the_agents_reachable_roots(workspace: Path) -> None:
    """★ **钥匙不能放在被锁的人手边。**

    授权库落在授权根之内的话，Agent 只要往那个文件追加一行，就给自己开了门 ——
    deny-first 会退化成"形式上的默认拒绝"。所以这必须是**构造时就拒绝**的硬错误，
    而不是一条"请记得别这么放"的注释。
    """
    allowed = workspace / "allowed"

    with pytest.raises(ValueError, match="授权库"):
        PolicyEngine(
            allowed_roots=[allowed],
            quarantine_root=workspace / "q",
            audit_path=workspace / "a.ndjson",
            grant_store=allowed / "grants.ndjson",      # ← 在授权根里面：必须拒绝
        )


# --------------------------------------------------------------------------- #
# 7. #49：还原要**留下记录**，而不是留一条"幽灵"条目
# --------------------------------------------------------------------------- #
def test_restore_marks_the_entry_instead_of_leaving_a_ghost(
    ops: OpsToolBox, engine: PolicyEngine, workspace: Path
) -> None:
    """#49：还原之后，隔离区里那条记录必须**如实说"已经还原了"**。

    现场（D15 做 demo 时撞到的）：还原过后再 `restore` 一次，报的是
    「**隔离项内容已丢失**」—— 听起来像数据丢了，其实只是**已经还原过**。
    而 `quarantine` 列表里那条记录看上去仍是"待还原"（剩余 72 小时）：
    **列出来像有东西要处理，实际没有。**

    ⇒ 同族：#37 的空白格、#38 的"skip 也是绿" —— 都是"看着像 A，其实是 B"。
    """
    from rca.policy import QuarantineError, list_entries, restore as restore_entry, summarize

    target = workspace / "allowed" / "once.json"
    target.write_text("内容", encoding="utf-8")
    grant = engine.grant(verb=Verb.DELETE, path_prefix=workspace / "allowed", ttl_s=60)
    res = ops.delete_artifact(str(target), grant_id=grant.grant_id)

    # 还原
    ops.restore_artifact(res.quarantine_id)

    entries = {e.quarantine_id: e for e in list_entries(workspace / "quarantine")}
    entry = entries[res.quarantine_id]
    assert entry.is_restored, "还原后没有留下任何标记 —— 列表里会显示成待还原"
    assert entry.restored_at, "应当记下还原时刻"

    # ★ 第二次还原要**说清是"已经还原过"**，而不是谎称内容丢失
    with pytest.raises(QuarantineError) as ei:
        restore_entry(workspace / "quarantine", res.quarantine_id)
    assert "已经还原过" in str(ei.value), f"报错信息仍有误导性：{ei.value}"
    assert "内容已丢失" not in str(ei.value)

    # ★ 概况里不该把已还原的算成"还在隔离区"
    summary = summarize(workspace / "quarantine")
    assert summary["count"] == 0, f"已还原的条目仍被算作待处理：{summary}"
    assert summary["restored_count"] == 1


def test_an_already_restored_entry_is_not_reported_as_pending_cleanup(
    ops: OpsToolBox, engine: PolicyEngine, workspace: Path
) -> None:
    """已还原的条目**不算"过期待清理"** —— 它的内容早就回去了。

    （构造方式：TTL 设 0 秒 ⇒ 立刻"过期"；再还原它 ⇒ 不该出现在清理报告里。）
    """
    from rca.policy import PolicyEngine as PE
    from rca.policy import sweep_report

    ws_engine = PE(
        allowed_roots=[workspace / "allowed"],
        quarantine_root=workspace / "quarantine",
        audit_path=workspace / "audit.ndjson",
        grant_store=workspace / "grants.json",
        quarantine_ttl_s=0,          # 立刻过期
    )
    box = OpsToolBox(ws_engine, world={"order": "http://fake"}, actor="t")
    target = workspace / "allowed" / "expiring.json"
    target.write_text("x", encoding="utf-8")
    grant = ws_engine.grant(verb=Verb.DELETE, path_prefix=workspace / "allowed", ttl_s=60)
    res = box.delete_artifact(str(target), grant_id=grant.grant_id)

    assert [e.quarantine_id for e in sweep_report(workspace / "quarantine")] == [res.quarantine_id]

    box.restore_artifact(res.quarantine_id)

    assert sweep_report(workspace / "quarantine") == [], (
        "已还原的条目还报成待清理 —— 人工去看会发现隔离区里什么也没有"
    )
# --------------------------------------------------------------------------- #
# 8. 诊断角色与 ops 工具的隔离（这是 #45 之后「不可逆操作给不到」的落点）
# --------------------------------------------------------------------------- #
def test_diagnosis_roles_have_only_read_level_tools() -> None:
    """三个诊断专员的工具必须是**只读**的 —— 它们连"写"都做不了，更别说删除。"""
    from rca.agents.roles import ALL_ROLES
    from rca.policy import verb_for_tool

    assert len(ALL_ROLES) == 3
    for role in ALL_ROLES:
        assert verb_for_tool(role.tool) == Verb.READ, (
            f"{role.key} 的工具 {role.tool} 不是只读级 —— 诊断路径上不许有写动作"
        )


def test_no_diagnosis_role_can_reach_an_ops_tool() -> None:
    """诊断角色的工具名里**不许出现**任何 ops 工具（隔离的机械保证）。"""
    from rca.agents.roles import ALL_ROLES

    diagnosis_tools = {r.tool for r in ALL_ROLES}
    overlap = diagnosis_tools & ops_tool_names()

    assert not overlap, f"诊断角色拿到了 ops 工具：{overlap}"
    assert "delete_artifact" not in diagnosis_tools
    assert "set_knobs" not in diagnosis_tools
