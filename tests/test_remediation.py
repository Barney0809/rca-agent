"""自动修复闭环（M5）的离线用例 —— 不花钱、不需要 Docker。

四条守的都是"比能自动修复更重要"的性质：

  1. **提案必须有据**：只能从变更记录反转出来，不许凭空发明修法；
  2. **无人审批就不执行**（而且"审批"不能是个默认通过的参数）；
  3. **不可逆动作不在这条链路上**；
  4. **缺少观测时必须说 unverified**，不许说"通过"（#26 的空绿）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rca import remediation  # noqa: E402
from rca.remediation import Proposal, apply, propose, verify  # noqa: E402

#: 真实的变更记录形状（取自 runs/r-*/changes.ndjson）
CHANGE = {"ts": "2026-09-24T04:26:32+08:00", "target": "inventory",
          "key": "W_INVENTORY_DOWNSTREAM_RETRIES", "from": "1", "to": "5", "by": "injector"}


class _Ops:
    """假的**MCP 门**：记录被怎么调的（真实现会拉起 stdio 子进程，测试里不需要）。"""

    def __init__(self, *, allowed: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.allowed = allowed

    def __call__(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        return {"allowed": self.allowed, "detail": {}, "reason": ""}


# ---------------------------------------------------------------- 1) 提案必须有据

def test_proposal_reverts_the_value_recorded_in_the_change_log() -> None:
    proposals = propose([CHANGE])
    assert len(proposals) == 1
    p = proposals[0]
    assert (p.service, p.knob) == ("inventory", "W_INVENTORY_DOWNSTREAM_RETRIES")
    assert p.current_value == "5" and p.target_value == "1", "要把 from 改回来，不是把 to 再设一遍"
    assert "injector" in p.source, "提案必须说清依据（哪条变更记录）"
    assert p.to_dict()["reversible"] is True


def test_incomplete_records_are_skipped_not_guessed() -> None:
    """缺字段就跳过 —— 宁可少提一条，也不要凭空补一个值出来。"""
    assert propose([{"target": "order", "key": "W_ORDER_POOL_SIZE", "to": "2"}]) == []
    assert propose([{"key": "x", "from": "1", "to": "2"}]) == []
    assert propose([]) == []


# ---------------------------------------------------------------- 2) 无人审批不执行

def test_apply_refuses_without_a_human_approval(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(remediation, "REMEDIATION_AUDIT_PATH", tmp_path / "audit.ndjson")
    ops = _Ops()
    out = apply(propose([CHANGE])[0], ops_call=ops, approved_by="   ")
    assert out["applied"] is False and "审批" in out["reason"]
    assert ops.calls == [], "没有审批就绝不能让动作到达 MCP 门"
    assert not (tmp_path / "audit.ndjson").exists(), "被拒绝的执行不该留下'已执行'的账"


# ---------------------------------------------------------------- 3) 执行：经工具箱 + 留痕

def test_apply_executes_through_the_mcp_door_and_writes_an_audit(monkeypatch, tmp_path) -> None:
    audit = tmp_path / "audit.ndjson"
    monkeypatch.setattr(remediation, "REMEDIATION_AUDIT_PATH", audit)
    ops = _Ops()
    out = apply(propose([CHANGE])[0], ops_call=ops, approved_by="barney")

    assert out["applied"] is True
    assert ops.calls == [("set_knobs", {"service": "inventory",
                                       "knobs": {"W_INVENTORY_DOWNSTREAM_RETRIES": "1"}})], \
        "动作必须是'把 target_value 写回去'，而且**经 MCP 门**（于是同时过策略点与无旁路保证）"
    line = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert line["approved_by"] == "barney" and line["applied"] is True
    assert line["proposal"]["knob"] == "W_INVENTORY_DOWNSTREAM_RETRIES"


def test_irreversible_actions_are_refused_even_with_approval(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(remediation, "REMEDIATION_AUDIT_PATH", tmp_path / "audit.ndjson")
    ops = _Ops()
    out = apply(Proposal(service="order", knob="x", current_value="1", target_value="0",
                         source="s", action="delete_artifact"),
                ops_call=ops, approved_by="barney")
    assert out["applied"] is False and "可逆白名单" in out["reason"]
    assert ops.calls == []


# ---------------------------------------------------------------- 4) 验证不许说假话

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
