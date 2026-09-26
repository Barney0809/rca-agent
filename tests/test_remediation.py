"""自动修复闭环（M5）的离线用例 —— 不花钱、不需要 Docker。

守的性质（前四条来自设计，其余来自**live 跑抓到的真事故**）：

  1. **提案必须有据**：只能从变更记录反转出来，不许凭空发明修法；
  2. **无人审批就不执行**（而且"审批"不能是个默认通过的参数）；
  3. **不可逆动作不在这条链路上**；
  4. **缺少观测时必须说 unverified**，不许说"通过"（#26 的空绿）；
  5. **名字要翻译**：变更记录里是 env 名（`W_INVENTORY_DOWNSTREAM_RETRIES`），
     世界只认旋钮名（`downstream_retries`）；名字用错时世界**返回 200 且什么都不改**；
  6. **成功要看读回**，不能只看返回码 —— 否则"执行成功"是一句假话，而且没有报错；
  7. **判据必须有方向**（#61）：写死"越小越好"会把「成功订单 10 → 0」报成 improved；
  8. **指标在两种状态下都必须有信号**：故障态本来就没症状时，「修好了」没有依据 ⇒
     宁可不下结论，也不给一个没依据的结论。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rca import remediation  # noqa: E402
from rca.remediation import (  # noqa: E402
    Proposal,
    apply,
    precheck_metric_signal,
    propose,
    verify,
)

#: 真实的变更记录形状（取自 runs/r-*/changes.ndjson）
CHANGE = {"ts": "2026-09-24T04:26:32+08:00", "target": "inventory",
          "key": "W_INVENTORY_DOWNSTREAM_RETRIES", "from": "1", "to": "5", "by": "injector"}


class _Ops:
    """假的 **MCP 门** + 假的**旋钮读回**（真实现会拉起子进程 / 打真实世界）。

    ⚠️ 它必须**忠实地**模仿世界的两个性质，否则用例会放过真缺陷（#62）：

      · 世界**不做类型转换**：收到什么就 `setattr` 什么（写进去字符串，它就存字符串）；
      · 世界的当前值**有类型**：读回要能区分 `1` 与 `"1"`。
    """

    def __init__(self, *, allowed: bool = True, observed: dict | None = None,
                 current: dict | None = None, store_as=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.allowed = allowed
        self.observed = observed          # None = 假装世界接受了修改
        self.current = dict({"downstream_retries": 5} if current is None else current)  # 世界现在的值（**带类型**）
        self.store_as = store_as          # 非 None 时模拟"世界把值存成了别的类型"
        self.reads: list[tuple[str, str]] = []

    def __call__(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        if self.allowed:                  # 世界收到就存，**不做类型转换**
            for k, v in (arguments.get("knobs") or {}).items():
                self.current[k] = self.store_as(v) if self.store_as else v
        return {"allowed": self.allowed, "detail": {}, "reason": ""}

    def read(self, service: str, knob: str):
        self.reads.append((service, knob))
        if self.observed is not None:
            return self.observed.get(knob, self.observed.get("__default__"))
        return self.current.get(knob)


# ---------------------------------------------------------------- 1) 提案必须有据

def test_proposal_reverts_the_value_recorded_in_the_change_log() -> None:
    proposals = propose([CHANGE])
    assert len(proposals) == 1
    p = proposals[0]
    assert (p.service, p.knob) == ("inventory", "downstream_retries"), \
        "世界认的是**旋钮名**，不是变更记录里的 env 名"
    assert p.env_key == "W_INVENTORY_DOWNSTREAM_RETRIES", "原始 env 名要留档以便追溯"
    assert p.current_value == "5" and p.target_value == "1", "要把 from 改回来，不是把 to 再设一遍"
    assert "injector" in p.source, "提案必须说清依据（哪条变更记录）"
    assert p.to_dict()["reversible"] is True


def test_incomplete_records_are_skipped_not_guessed() -> None:
    """缺字段就跳过 —— 宁可少提一条，也不要凭空补一个值出来。"""
    assert propose([{"target": "order", "key": "W_ORDER_POOL_SIZE", "to": "2"}]) == []
    assert propose([{"key": "x", "from": "1", "to": "2"}]) == []
    assert propose([]) == []


# ---------------------------------------------------------------- 5) 名字翻译

def test_env_name_translation() -> None:
    assert remediation.knob_name_from_env("W_INVENTORY_DOWNSTREAM_RETRIES") == "downstream_retries"
    assert remediation.knob_name_from_env("W_PAYMENT_RISK_ERROR_RATE") == "risk_error_rate"
    assert remediation.knob_name_from_env("W_ORDER_POOL_SIZE") == "pool_limit", "别名要走规范表"
    assert remediation.knob_name_from_env("downstream_retries") == "downstream_retries", "已是旋钮名就原样"


def test_env_name_translation_matches_the_injector() -> None:
    """两张表必须一致（注入器在 scripts/ 下，从 src/ import 它会把依赖方向倒过来，
    所以那边保留一份；这里把"一致"钉住，防止静默漂移）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from inject_fault import _KNOB_ALIASES                          # noqa: PLC0415

    assert remediation.KNOB_ALIASES == dict(_KNOB_ALIASES), \
        "两份旋钮别名表漂移了 —— 一方改了另一方没改，就会再现'静默空操作'"


# ---------------------------------------------------------------- 2) 无人审批不执行

def test_apply_refuses_without_a_human_approval(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(remediation, "REMEDIATION_AUDIT_PATH", tmp_path / "audit.ndjson")
    ops = _Ops()
    out = apply(propose([CHANGE])[0], ops_call=ops, read_knob=ops.read, approved_by="   ")
    assert out["applied"] is False and "审批" in out["reason"]
    assert ops.calls == [], "没有审批就绝不能让动作到达 MCP 门"
    assert ops.reads == [], "更不该去读世界"
    assert not (tmp_path / "audit.ndjson").exists(), "被拒绝的执行不该留下'已执行'的账"


# ---------------------------------------------------------------- 3) 执行 + 4) 读回确认

def test_apply_executes_through_the_mcp_door_and_writes_an_audit(monkeypatch, tmp_path) -> None:
    audit = tmp_path / "audit.ndjson"
    monkeypatch.setattr(remediation, "REMEDIATION_AUDIT_PATH", audit)
    ops = _Ops()
    out = apply(propose([CHANGE])[0], ops_call=ops, read_knob=ops.read, approved_by="barney")

    assert out["applied"] is True
    assert ops.calls == [("set_knobs", {"service": "inventory",
                                        "knobs": {"downstream_retries": 1}})], \
        "动作必须是'把 target_value 用**旋钮名**写回去'，而且经 MCP 门"
    # ⚠️ 类型必须**跟世界一致**（这里是 int，不是字符串）：变更记录里是 "1"，
    #    直接写回去会让世界存下字符串 ⇒ `max(1, "1")` → 之后每个请求 500（#62 实测）
    assert isinstance(ops.calls[0][1]["knobs"]["downstream_retries"], int), \
        "字符串旋钮会把服务在运行时弄崩，而且读回按字符串比还会报'成功'"
    assert ops.reads == [("inventory", "downstream_retries")] * 2, \
        "读两次：一次学当前值的**类型**，一次读回确认（都走只读的 /_knobs）"
    line = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert line["approved_by"] == "barney" and line["applied"] is True
    assert line["observed_after"] == 1


def test_apply_sends_the_value_in_the_type_the_world_uses() -> None:
    """#62：**世界的 `/_inject` 不做类型转换** —— 类型由"当前值"决定，不是我们猜的。"""
    # 当前值是 float ⇒ 写回去也必须是 float
    ops = _Ops(current={"risk_error_rate": 0.6})
    out = apply(Proposal(service="payment", knob="risk_error_rate",
                         current_value="0.6", target_value="0.0", source="t"),
                ops_call=ops, read_knob=ops.read, approved_by="barney")
    sent = ops.calls[0][1]["knobs"]["risk_error_rate"]
    assert isinstance(sent, float) and sent == 0.0, f"应当写 0.0（float），实际 {sent!r}"
    assert out["applied"] is True

    # 当前值读不到 ⇒ **不猜类型，拒绝执行**（宁可不写）
    blind = _Ops(current={})                       # 世界里没有这个旋钮
    out2 = apply(propose([CHANGE])[0], ops_call=blind, read_knob=blind.read, approved_by="barney")
    assert out2["applied"] is False and blind.calls == [], \
        "读不到当前值就不知道类型 ⇒ 不许动手（写坏了是把系统弄崩，不是'少改一个数'）"


def test_a_value_that_only_matches_as_a_string_is_not_a_success() -> None:
    """#62 的另一半：世界把值存成了别的类型 ⇒ **不算成功**。

    这正是当时骗过我的地方 —— 旧的检查写的是 `str(observed) == str(target)`，
    于是"整数被写成字符串"看起来完全成功，直到世界开始 500。
    """
    ops = _Ops(current={"downstream_retries": 5}, store_as=str)   # 世界把 int 存成了 str
    out = apply(propose([CHANGE])[0], ops_call=ops, read_knob=ops.read, approved_by="barney")
    assert out["applied"] is False, "值一样、类型不对 ⇒ 必须报不成功"
    assert "类型" in out["reason"] and "崩" in out["reason"], \
        f"理由要说清'类型被改坏了会崩'，实际：{out['reason']}"


def test_apply_reports_failure_when_the_world_did_not_change(monkeypatch, tmp_path) -> None:
    """**实测过的真事故**：世界对不认识的字段返回 200 且什么都不改。

    所以"动作生效"的唯一凭据是**读回**：读回还是旧值 ⇒ 报 applied=False 并说清原因。
    """
    monkeypatch.setattr(remediation, "REMEDIATION_AUDIT_PATH", tmp_path / "audit.ndjson")
    ops = _Ops(observed={"downstream_retries": "5"})       # 世界没变
    out = apply(propose([CHANGE])[0], ops_call=ops, read_knob=ops.read, approved_by="barney")

    assert out["applied"] is False, "返回码 200 不等于动作生效"
    assert "静默空操作" in out["reason"] and "downstream_retries" in out["reason"]
    assert out["observed"] == "5"


def test_apply_treats_an_unreadable_world_as_unverified(monkeypatch, tmp_path) -> None:
    """读不到就**不能**说成功 —— "读不到"与"值不对"是两件事，但都不算生效。

    ⚠️ 行为在 #62 之后**变强了**（这条用例随之更新，不是它抓错了）：
       既然"当前值"是**类型**的唯一来源，读不到它就**根本不该动手** ——
       所以现在停在"拒绝执行"，而不是"先写了、再报读不到"。
       **不变的那条性质**：读不到 ⇒ 绝不报成功（下面两条都钉着它）。
    """
    monkeypatch.setattr(remediation, "REMEDIATION_AUDIT_PATH", tmp_path / "audit.ndjson")
    ops = _Ops(observed={"__default__": None})
    out = apply(propose([CHANGE])[0], ops_call=ops, read_knob=ops.read, approved_by="barney")
    assert out["applied"] is False, "读不到世界 ⇒ 绝不能说成功"
    assert "不敢写" in out["reason"] and "类型" in out["reason"], \
        f"理由要说清'读不到当前值 ⇒ 不知道类型 ⇒ 不写'，实际：{out['reason']}"
    assert ops.calls == [], "读不到当前值就不该把动作送到 MCP 门"


def test_irreversible_actions_are_refused_even_with_approval(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(remediation, "REMEDIATION_AUDIT_PATH", tmp_path / "audit.ndjson")
    ops = _Ops()
    out = apply(Proposal(service="order", knob="x", current_value="1", target_value="0",
                         source="s", action="delete_artifact"),
                ops_call=ops, read_knob=ops.read, approved_by="barney")
    assert out["applied"] is False and "可逆白名单" in out["reason"]
    assert ops.calls == []


# ---------------------------------------------------------------- 6) 验证不许说假话

def test_verify_refuses_to_pass_without_after_observations() -> None:
    out = verify(symptom="payment WARNING", before=8049, after=None)
    assert out["status"] == "unverified", "缺事后观测就必须说 unverified，不许说通过"
    assert "需要再跑一次场景" in out["why"]
    assert "after" not in out


def test_verify_compares_counts_honestly() -> None:
    assert verify(symptom="w", before=100, after=0)["status"] == "improved"
    assert verify(symptom="w", before=100, after=100)["status"] == "unchanged"
    assert verify(symptom="w", before=100, after=140)["status"] == "worse"
    assert verify(symptom="w", before=None, after=3)["status"] == "unverified"


def test_verify_respects_the_direction_of_the_metric() -> None:
    """**踩出来的**：判据写死了"越小越好"，于是"成功订单数 10 → 0"被报成 improved ✗✗
    —— 脚本用漂亮的措辞报告了一场灾难，还 exit 0 说"通过"。

    ⇒ 方向必须显式写出来：坏事的计数越小越好（默认），好的计数（成功数/成功率）越大越好。
    """
    assert verify(symptom="成功订单数", before=10, after=0)["status"] == "improved", \
        "先确认默认行为：这条在'越小越好'下**确实**会被判成改善（所以必须显式给方向）"
    assert verify(symptom="成功订单数", before=10, after=0,
                  lower_is_better=False)["status"] == "worse", "10 掉到 0 是灾难，不是改善"
    assert verify(symptom="成功订单数", before=0, after=10,
                  lower_is_better=False)["status"] == "improved"
    assert verify(symptom="WARNING 条数", before=0, after=10)["status"] == "worse"


def test_precheck_refuses_to_conclude_when_the_metric_has_no_signal() -> None:
    """前置检查：**两种状态下指标都必须有信号**，否则不许下结论。

    这是 #61 修完之后紧接着撞上的第二种错法：判据没错，只是**什么也没证明** ——
    故障态本来就没症状（指标对这个故障不敏感，或故障没生效），
    此时无论恢复态是几，「修好了」都是没有依据的结论。
    和 #61 一样，它不会报错、格式也漂亮，只是结论不成立。
    """
    # ① 故障态真失败过 ⇒ 两态都有信号（理想形状：9/10 → 10/10）
    good = precheck_metric_signal(symptom="成功订单数", sent_before=10, ok_before=9,
                                 sent_after=10, ok_after=10)
    assert good["ok"] is True and good["status"] == "signal-in-both-states"

    # ② 故障态**全部成功** ⇒ 这个指标上看不到故障 ⇒ 不许下结论（哪怕两侧数字一样漂亮）
    bad = precheck_metric_signal(symptom="成功订单数", sent_before=10, ok_before=10,
                                 sent_after=10, ok_after=10)
    assert bad["ok"] is False
    assert bad["status"] == "no-signal-in-faulted-state", "前置检查必须认出'没有信号'"

    # ③ 没有样本：有一侧一笔订单都没发出去 ⇒ 没有可比较的观测
    assert precheck_metric_signal(symptom="x", sent_before=0, ok_before=0,
                                  sent_after=10, ok_after=10)["status"] == "no-sample"

    # ④ 没有观测：「没测到」不等于「测到 0」
    assert precheck_metric_signal(symptom="x", sent_before=10, ok_before=9,
                                  sent_after=10, ok_after=None)["status"] == "no-observation"

    # ⑤ ⚠️ 口径：成功数 0 是**地板值**，它仍然**有信号**（方向可读、幅度不可读）
    #    把它当成"没信号"会让"反而更差"这种真结论也说不出来
    floor = precheck_metric_signal(symptom="成功订单数", sent_before=10, ok_before=9,
                                   sent_after=10, ok_after=0)
    assert floor["ok"] is True, "0 是地板值，不是'没信号' —— 它仍然指出了一个方向"
