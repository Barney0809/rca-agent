"""M5 端到端脚本（`scripts/remediate_demo.py`）里**可离线测**的那些判断。

============================ 为什么值一条用例 ============================

M5 的端到端跑一次要**两分钟**（还要往被测世界里注入故障），所以它自己的判断
最容易被"靠真跑一次世界来体现"糊过去 —— 而真跑一次**没人会每次都跑**（#36 的教训）。

这里只测**纯判断**，每一条都来自一次真实的失败：

  · **#63**：`scenario` 模式打完流量会**撤销故障**，`apply` 只施加。
    第一版只跑了 `scenario` ⇒ 所谓的"故障态"其实是在一个**健康**世界上量的，
    量出 10/10 全部成功。少了"故障真的在吗"这一步，就会把
    "没有症状"读成"修好了"。（当场是前置检查拦下的，但那时动作已经执行过了。）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from rca.remediation import Proposal, propose           # noqa: E402
from remediate_demo import (                            # noqa: E402
    fault_is_armed,
    metrics_disagree,
    patch_coverage,
    remediation_outcome,
)

#: 真实形状（取自 runs/r-20260926-235507/changes.ndjson）
RECORD = {"ts": "2026-09-26T23:55:18+08:00", "target": "inventory",
          "key": "W_INVENTORY_DOWNSTREAM_RETRIES", "from": "1", "to": "5", "by": "injector"}


def test_fault_is_armed_only_when_the_world_shows_the_recorded_value() -> None:
    """故障**在** ⇒ 过；故障**被撤销了** ⇒ 必须拦住（这就是 #63 那个假故障态）。"""
    armed = {"inventory": {"downstream_retries": 5}}                  # 变更记录的 to=5
    ok, why = fault_is_armed([RECORD], knobs=armed)
    assert ok is True and "都对得上" in why

    # ← 真实事故的形状：scenario 跑完把故障撤销了，世界回到 1，而变更记录还写着 1→5
    reverted = {"inventory": {"downstream_retries": 1}}
    bad, why2 = fault_is_armed([RECORD], knobs=reverted)
    assert bad is False, "世界明明已经回到默认值 ⇒ 不能把它当成'故障态'"
    assert "没有" in why2 and "downstream_retries" in why2, why2

    # env 名 → 旋钮名的翻译必须在这里也生效（变更记录里是 W_INVENTORY_...）
    assert fault_is_armed([RECORD], knobs={"inventory": {"W_INVENTORY_DOWNSTREAM_RETRIES": 5}})[0] is False, \
        "世界认的是**旋钮名**；用 env 名去读当然读不到 ⇒ 如实判'没施加'，不许猜"


def test_fault_is_armed_refuses_when_there_is_nothing_to_check() -> None:
    """没有变更记录 ⇒ 既没有可提案的依据，也无从核对故障 —— 不许当成"没问题"。"""
    ok, why = fault_is_armed([], knobs={"inventory": {"downstream_retries": 5}})
    assert ok is False and "没有变更记录" in why


# ------------------------------------------------------- #64：判定必须被守住

def _v(status: str) -> dict:
    return {"status": status}


def test_metrics_disagree_needs_a_user_side_that_did_not_improve() -> None:
    """#64：这条判定原来只靠"跑一次 live 看一眼"验证 —— 若它恒为 False，谁也发现不了。

    真实形状（两轮 live 都是）：判据（用户可感的成功订单数）`worse`，
    两个因果对齐的量 `improved` ⇒ 分歧 ⇒ 两条都要报。
    """
    assert metrics_disagree(user_facing=_v("worse"),
                            causal_aligned=[_v("improved"), _v("improved")]) is True
    assert metrics_disagree(user_facing=_v("improved"),
                            causal_aligned=[_v("improved"), _v("improved")]) is False, \
        "用户侧变好了就不叫分歧（那是修好了）"
    assert metrics_disagree(user_facing=_v("worse"),
                            causal_aligned=[_v("improved"), _v("worse")]) is False, \
        "因果侧只有一个变好也不算分歧 —— 那更像整体没好"
    assert metrics_disagree(user_facing=_v("unchanged"),
                            causal_aligned=[_v("improved")]) is True, \
        "没变化 + 因果侧变好，同样属于「动作对、用户侧没好处」"


def test_metrics_disagree_is_not_vacuously_true() -> None:
    """⚠️ `all([])` 在 Python 里**恒为 True** —— 一把尺子都没有时不许报"分歧"。

    这正是本项目反复防的"空绿"：条件写漏一个 `bool(...)`，
    就会在**没有任何因果量**的情况下打出一条"两把尺子分歧"的结论。

    （#64 就是在这种地方栽的：判定写在 `main()` 里、只靠人眼验证。）
    """
    assert metrics_disagree(user_facing=_v("worse"), causal_aligned=[]) is False
    assert metrics_disagree(user_facing=_v("worse"), causal_aligned=[{}]) is False, \
        "缺 status 字段的量不算 improved（不许把读不到当成测到了）"


# -------------------------------------------- §4 那条"新认识"现在是机制，不是打印

F4_PATCHES = {"payment": {"risk_error_rate": 0.6}, "inventory": {"downstream_retries": 5}}


def test_patch_coverage_points_at_the_half_without_a_change_record() -> None:
    """**故障补丁 ≠ 变更记录**：F4 改两个旋钮，而只有重试次数留下记录。

    ⇒ "修完"仍有一半故障在，而且**只修一半可能比不修更差**（被撤掉的那个旋钮
    可能正在兜住另一半）。这件事必须在**动手之前**由代码判定出来并告警，
    而不是事后补一句解释、更不是写死某个旋钮名。
    """
    cov = patch_coverage(F4_PATCHES, propose([RECORD]))       # RECORD = inventory 那条
    assert cov["verdict"] == "partial", "两改一 ⇒ 必须报'只覆盖一部分'"
    assert cov["covered"] == [("inventory", "downstream_retries")]
    assert cov["uncovered"] == [("payment", "risk_error_rate")], \
        "没留下记录的那一半必须被**点名**（否则告警是句空话）"
    assert cov["fraction"] == 0.5

    full = patch_coverage(F4_PATCHES, [
        Proposal(service="inventory", knob="downstream_retries",
                 current_value="5", target_value="1", source="s"),
        Proposal(service="payment", knob="risk_error_rate",
                 current_value="0.6", target_value="0.0", source="s"),
    ])
    assert full["verdict"] == "full" and full["uncovered"] == []

    # 没有补丁（baseline 之类）⇒ **不许**把"没有故障"说成"只修了一半"
    assert patch_coverage({}, [])["verdict"] == "full"


def test_outcome_never_hides_a_worse_symptom() -> None:
    """结局标签必须**机器可读且不粉饰**：对 `worse` 只说 `action-ok-symptom-partial`，
    读者会以为"部分改善" —— 那就是把一个灾难说成进展的软措辞（同 #61 的家族病）。
    """
    assert remediation_outcome(action_ok=True, symptom_status="improved",
                               coverage="full") == "fixed"
    assert remediation_outcome(action_ok=True, symptom_status="improved",
                               coverage="partial") == "fixed-partial-coverage", \
        "只修了一半却说 fixed，就是把'另一半还在'藏起来"
    assert remediation_outcome(action_ok=True, symptom_status="worse",
                               coverage="full") == "action-ok-symptom-worse"
    assert remediation_outcome(action_ok=True, symptom_status="worse",
                               coverage="partial") == "action-ok-symptom-worse-partial-coverage"
    assert remediation_outcome(action_ok=True, symptom_status="unchanged",
                               coverage="full") == "action-ok-symptom-unchanged"
    assert remediation_outcome(action_ok=False, symptom_status="worse",
                               coverage="partial") == "action-failed", \
        "动作都没生效时，别拿症状说事"
