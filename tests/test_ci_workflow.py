"""CI 工作流本身的守卫（D21）。

真实事故（2026-09-26，第一次让 CI 真跑时）：

    我在 `dump container logs on failure` 那一步前面插了一行 `if: always()`，
    而**那一步本来就有 `if: failure()`** ⇒ 同一个 mapping 里出现**两个 `if:` 键** ⇒
    GitHub 直接判定"workflow 文件有问题"、**整个 workflow 不启动**（两个 job 都没跑）。
    而我在本地用 `yaml.safe_load` 检查过，**它静默通过了** ——
    PyYAML 对重复键是"后者覆盖前者"，不报错。

⇒ 本地门禁比真解析器弱，这就是要补的洞。两条守卫：

  1. 工作流 YAML **不许有重复键**（用严格 loader 解析）；
  2. 工作流里探活用的 URL **必须真的存在**（路径要在 `world/*/main.py` 里被注册过）——
     这条对应的真实事故是：探活 curl 的是 `/healthz`，而服务注册的是 `/health`，
     于是容器全部 Healthy、应用日志写着 redis 可用，readiness 却一直等到超时。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


class _StrictLoader(yaml.SafeLoader):
    """把"重复键"从静默覆盖变成硬错误 —— 与 PyYAML 默认行为的**唯一**区别。"""


def _no_duplicate_keys(loader, node, deep=False):        # noqa: ANN001
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, f"重复的键：{key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


def test_workflows_exist() -> None:
    assert WORKFLOWS, "找不到任何工作流 —— 守卫不能空转"


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_workflow_has_no_duplicate_keys(path: Path) -> None:
    """重复键必须**当场报错**（GitHub 会因此拒绝整个文件，而 PyYAML 不吭声）。"""
    text = path.read_text(encoding="utf-8")
    try:
        yaml.load(text, Loader=_StrictLoader)            # noqa: S506 —— 严格 loader 是本地类
    except yaml.constructor.ConstructorError as exc:
        pytest.fail(f"{path.name} 里有重复的键（GitHub 会直接拒绝这个文件）：{exc}")
    # 也确认默认 loader 能读（两条都过才算真的没问题）
    assert yaml.safe_load(text) is not None


def test_health_urls_in_the_workflow_are_really_served() -> None:
    """工作流里探活的路径，必须在某个服务里**真的注册过**。

    （真实事故：探 `/healthz`，服务却是 `/health` ⇒ 容器健康、探活超时。）
    """
    declared: set[str] = set()
    for main in (ROOT / "world").glob("*/main.py"):
        declared |= set(re.findall(r'@app\.(?:get|post)\("(/[^"]+)"\)', main.read_text(encoding="utf-8")))
    assert declared, "没能从 world/*/main.py 里解析出任何路由 —— 守卫不能空转"

    checked = 0
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        for url in re.findall(r"http://127\.0\.0\.1:\d+(/[A-Za-z0-9_/-]*)", text):
            checked += 1
            assert url in declared, (
                f"{path.name} 探活用了 {url}，但没有任何服务注册这个路径 "
                f"（已注册：{sorted(declared)}）—— 这会让 readiness 一直等到超时"
            )
    assert checked, "工作流里一个探活 URL 都没找到 —— 守卫不能空转"
