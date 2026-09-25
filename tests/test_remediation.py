"""自动修复闭环（M5）的离线用例 —— 不花钱、不需要 Docker。

守的性质（前四条来自设计，后两条来自**第一次 live 跑抓到的真事故**）：

  1. **提案必须有据**：只能从变更记录反转出来，不许凭空发明修法；
  2. **无人审批就不执行**（而且"审批"不能是个默认通过的参数）；
  3. **不可逆动作不在这条链路上**；
  4. **缺少观测时必须说 unverified**，不许说"通过"（#26 的空绿）；
  5. **名字要翻译**：变更记录里是 env 名（`W_INVENTORY_DOWNSTREAM_RETRIES`），
     世界只认旋钮名（`downstream_retries`）；名字用错时世界**返回 200 且什么都不改**；
  6. **成功要看读回**，不能只看返回码 —— 否则"执行成功"是一句假话，而且没有报错。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rca import remediation  # noqa: E402
from rca.remediation import Proposal, apply, propose, verify  # noqa: E402

#: 真实的变更记录形状（取自 runs/r-*/changes.ndjson）
CHANGE = {"ts": "2026-09-24T04:26:32+08:00", "target": "inventory",
          "key": "W_INVENTORY_DOWNSTREAM_RETRIES", "from": "1", "to": "5", "by": "injector"}


class _Ops:
    """假的 **MCP 门** + 假的**旋钮读回**（真实现会拉起子进程 / 打真实世界）。"""

    def __init__(self, *, allowed: bool = True, observed: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.allowed = allowed
        self.observed = observed          # None = 假装世界接受了修改
        self.reads: list[tuple[str, str]] = []

    def __call__(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        return {"allowed": self.allowed, "detail": {}, "reason": ""}

    def read(self, service: str, knob: str):
        self.reads.append((service, knob))
        if self.observed is not None:
            return self.observed.get(knob, self.observed.get("__default__"))
        wanted = next((c[1]["knobs"].get(knob) for c in reversed(self.calls) if "knobs" in c[1]), None)
        return wanted


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
                                        "knobs": {"downstream_retries": "1"}})], \
        "动作必须是'把 target_value 用**旋钮名**写回去'，而且经 MCP 门"
    assert ops.reads == [("inventory", "downstream_retries")], "必须读回确认，不能只看返回码"
    line = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert line["approved_by"] == "barney" and line["applied"] is True
    assert line["observed_after"] == "1"


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
    """读不到就**不能**说成功 —— "读不到"与"值不对"是两件事，但都不算生效。"""
    monkeypatch.setattr(remediation, "REMEDIATION_AUDIT_PATH", tmp_path / "audit.ndjson")
    ops = _Ops(observed={"__default__": None})
    out = apply(propose([CHANGE])[0], ops_call=ops, read_knob=ops.read, approved_by="barney")
    assert out["applied"] is False and "静默空操作" in out["reason"]


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
