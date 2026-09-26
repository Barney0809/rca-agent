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

from remediate_demo import fault_is_armed  # noqa: E402

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
