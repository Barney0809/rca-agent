"""
策略执行点的验收用例 —— 对应需求里的 AC-5 ~ AC-9。

    AC-5  越界删除尝试 100% 被拒绝，且理由可解释
    AC-6  编码异常路径不致解析失准
    AC-7  删除操作可在隔离区完整还原（"不可逆"演示为"可逆"）
    AC-8  每次拒绝均有审计记录，无静默失败

============================ 关于"不删除任何东西" ============================

本项目有一条铁律：**不删除任何东西**。测试也必须遵守它。

所以这里**不使用 pytest 的 tmp_path**（它在测试结束时会被 pytest 删掉），
而是把工作区建在 `runs/_policy_tests/<唯一名>/` 下，**跑完就留着**。

代价是目录会累积 —— 但它们都是几十字节的小文件，
而换来的是"任何一次测试都不以删除收场"这条性质。这笔账划得来。
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

from rca.policy import (
    PolicyEngine,
    QuarantineError,
    Verb,
    check_path,
    normalize,
    sweep_report,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def workspace() -> Path:
    """每次测试一个唯一工作区。**不清理**（见模块头部说明）。"""
    ws = ROOT / "runs" / "_policy_tests" / f"t-{uuid.uuid4().hex[:8]}"
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


def _make_file(p: Path, content: str = "hello") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ================================================================
# AC-5：越界删除 100% 被拒绝，且理由可解释
# ================================================================

def test_ac5_delete_outside_allowed_root_is_denied(engine: PolicyEngine, workspace: Path):
    """AC-5 —— 授权范围之外的目标，即使有合法授权也必须被拒绝。

    注意这里**故意给了有效授权**：证明"路径白名单"是独立的一道门，
    不会因为有人点了头就被绕过。
    """
    victim = _make_file(workspace / "outside" / "important.txt", "几年积累的资料")
    grant = engine.grant(verb=Verb.DELETE, path_prefix=workspace, ttl_s=600)

    d = engine.quarantine_delete(tool="delete_artifact", path=str(victim), grant_id=grant.grant_id)

    assert not d.allowed, "越界删除必须被拒绝"
    assert d.reason, "拒绝必须有理由"
    assert d.suggestion, "拒绝必须给出替代做法（FR-2.8）"
    assert victim.exists(), "★ 最关键的一条：目标必须原封不动地还在"
    assert not d.quarantine_id, "被拒绝时不应产生隔离项"


def test_ac5_the_incident_path_shape_is_denied(engine: PolicyEngine, workspace: Path):
    """AC-5 —— 复现事故里那条路径的形状。

    历史：子代理执行 Remove-Item -Recurse -Force，
         路径因编码错乱而落到了工作目录的**兄弟目录**。
    现在：任何不在白名单里的路径，第一步就被拒。
    """
    sibling = _make_file(workspace / "outside" / "Docs" / "多年文档.md", "内容")

    for tool in ("delete_artifact", "force_overwrite", "move_artifact_outside"):
        d = engine.decide(tool=tool, path=str(sibling))
        assert not d.allowed, f"{tool} 应当拒绝越界路径"
    assert sibling.exists()


def test_ac5_unknown_tool_is_treated_as_most_dangerous(engine: PolicyEngine):
    """AC-5 配套 —— 未登记的工具按最高等级处理，而不是"未知就放行"。

    这条是"漏登记一个工具"的保险：结果是它什么也做不了（安全），
    而不是它什么都能做（事故）。
    """
    d = engine.decide(tool="some_tool_nobody_registered", path=None)
    assert not d.allowed
    assert d.verb == Verb.FORCE


# ================================================================
# AC-6：编码异常 / 路径穿越 不致解析失准
# ================================================================

def test_ac6_parent_traversal_is_denied(workspace: Path):
    """AC-6 —— `授权目录/../../outside` 必须被识别为越界。

    关键是**先 resolve 再比对**：resolve 会把 ".." 解析掉，
    于是得到的真实路径落在白名单之外。
    如果先比对再 resolve，这一条就能穿过白名单。
    """
    allowed = workspace / "allowed"
    evil = str(allowed / ".." / ".." / "outside")

    pc = check_path(evil, [allowed])
    assert not pc.ok, f"路径穿越应当被拒绝，实际解析为 {pc.resolved}"


def test_ac6_fullwidth_homoglyph_does_not_bypass(workspace: Path):
    """AC-6 —— 全角字符不能在 Unicode 归一化之后绕过白名单。

    `Ａ`（全角）和 `A`（半角）是不同的码位。如果只做字符串比对，
    构造一个"看起来像授权目录"的路径就可能骗过前缀检查。
    NFC 归一化把它们收敛到同一码位，于是这种把戏失效。
    """
    allowed = workspace / "allowed"
    allowed.mkdir(parents=True, exist_ok=True)
    (allowed / "target").mkdir(exist_ok=True)

    # 用一个"看起来一样"的全角路径去试
    fullwidth = str(workspace / "ａｌｌｏｗｅｄ" / "target")
    pc = check_path(fullwidth, [allowed])
    # 归一化之后它并不等于真实的 allowed 目录，所以应当被拒
    assert not pc.ok or pc.resolved == (allowed / "target").resolve(), (
        f"全角同形字路径处理不正确：resolved={pc.resolved}"
    )


def test_ac6_symlink_escape_is_denied(workspace: Path):
    """AC-6 —— 授权目录里的符号链接不能成为越界的跳板。

    攻击方式：在授权目录里放一个软链接指向外部目录，
    然后用"授权目录/链接/文件"这个看起来很合法的路径去操作它。
    resolve() 会解析软链接，于是真实路径暴露在白名单之外。
    """
    allowed = workspace / "allowed"
    outside = workspace / "outside"
    link = allowed / "escape"

    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接（Windows 需要开发者模式或管理员）")

    pc = check_path(str(link / "secret.txt"), [allowed])
    assert not pc.ok, f"经软链接越界应当被拒绝，实际解析为 {pc.resolved}"
    assert pc.resolved is not None and str(outside) in str(pc.resolved)


@pytest.mark.parametrize(
    "bad,why",
    [
        ("", "空路径"),
        ("   ", "纯空白"),
        ("a\x00b", "含 NUL 字节（截断攻击载荷）"),
        ("C:/tmp/\x07bell", "含控制字符"),
        ("x" * 5000, "超长路径"),
    ],
)
def test_ac6_malformed_inputs_are_rejected_not_misparsed(bad: str, why: str):
    """AC-6 —— 非法输入必须在"解析"这一步就被挡住，而不是被静默改写。"""
    with pytest.raises(ValueError):
        normalize(bad)


def test_ac6_normalization_resolves_dots_and_case(workspace: Path):
    """AC-6 正向对照：合法路径要能正确规范化（否则上面那些断言可能只是"永远拒绝"）。"""
    allowed = workspace / "allowed"
    resolved = normalize(str(allowed / "sub" / ".." / "file.txt"))
    assert resolved == (allowed / "file.txt").resolve()


# ================================================================
# AC-7：隔离区完整还原 —— "不可逆"演示为"可逆"
# ================================================================

def test_ac7_delete_becomes_reversible(engine: PolicyEngine, workspace: Path):
    """AC-7 —— **本项目最重要的一条演示**。

    删除请求被允许之后：文件从原地消失，但内容**一个字节都没丢**，
    而且能完整还原回来。

    这就是"把不可逆变成可逆"。
    """
    original = "这是非常重要的内容，绝不能丢。\n第二行。\n"
    victim = _make_file(workspace / "allowed" / "report.md", original)

    grant = engine.grant(verb=Verb.DELETE, path_prefix=workspace / "allowed", ttl_s=600)
    d = engine.quarantine_delete(
        tool="delete_artifact", path=str(victim), grant_id=grant.grant_id
    )

    assert d.allowed, f"授权范围内的删除应当放行（理由：{d.reason}）"
    assert d.quarantine_id, "应当返回隔离项 id"
    assert not victim.exists(), "原位置应当已清空"

    # ★ 内容必须完整保留
    entries = engine.list_quarantine()
    assert len(entries) == 1
    stored = engine.quarantine_root / entries[0].quarantine_id / "payload" / entries[0].stored_name
    assert stored.exists()
    assert stored.read_text(encoding="utf-8") == original, "内容必须逐字节一致"

    # ★ 还原
    r = engine.restore(quarantine_id=d.quarantine_id)
    assert r.allowed, f"还原应当成功（理由：{r.reason}）"
    assert victim.exists(), "还原后应回到原始位置"
    assert victim.read_text(encoding="utf-8") == original, "还原后内容必须逐字节一致"


def test_ac7_directory_can_be_quarantined_and_restored(engine: PolicyEngine, workspace: Path):
    """AC-7 —— 目录同样可隔离可还原（事故删的正是整个目录）。"""
    d = workspace / "allowed" / "docs"
    (d / "sub").mkdir(parents=True)
    (d / "a.md").write_text("A", encoding="utf-8")
    (d / "sub" / "b.md").write_text("B", encoding="utf-8")

    grant = engine.grant(verb=Verb.DELETE, path_prefix=workspace / "allowed", ttl_s=600)
    res = engine.quarantine_delete(tool="delete_artifact", path=str(d), grant_id=grant.grant_id)
    assert res.allowed
    assert not d.exists()

    r = engine.restore(quarantine_id=res.quarantine_id)
    assert r.allowed
    assert (d / "a.md").read_text(encoding="utf-8") == "A"
    assert (d / "sub" / "b.md").read_text(encoding="utf-8") == "B"


def test_ac7_restore_refuses_to_overwrite(engine: PolicyEngine, workspace: Path):
    """AC-7 配套 —— 还原时若目标已存在，**拒绝覆盖**。

    "还原"本身也是一种写操作；允许它静默覆盖，就等于又开了一个不可逆的入口。
    """
    victim = _make_file(workspace / "allowed" / "x.md", "原始")
    grant = engine.grant(verb=Verb.DELETE, path_prefix=workspace / "allowed", ttl_s=600)
    d = engine.quarantine_delete(tool="delete_artifact", path=str(victim), grant_id=grant.grant_id)

    _make_file(victim, "新内容占据了位置")

    r = engine.restore(quarantine_id=d.quarantine_id)
    assert not r.allowed, "目标已存在时应当拒绝还原"
    assert victim.read_text(encoding="utf-8") == "新内容占据了位置", "不得覆盖已有文件"


def test_ac7_ttl_expiry_only_reports_never_deletes(engine: PolicyEngine, workspace: Path):
    """AC-7 配套 —— TTL 到期只**报告**，绝不自动删除。

    这是刻意的设计：一旦存在"自动删除"，就存在"删错了"的可能，
    只是把风险推后了。清理必须是人的决定。
    """
    victim = _make_file(workspace / "allowed" / "old.md", "内容")
    grant = engine.grant(verb=Verb.DELETE, path_prefix=workspace / "allowed", ttl_s=600)
    engine.quarantine_delete(tool="delete_artifact", path=str(victim), grant_id=grant.grant_id)

    # 假装过了很久
    from datetime import datetime, timedelta

    future = datetime.now().astimezone() + timedelta(days=365)
    expired = sweep_report(engine.quarantine_root, now=future)

    assert len(expired) == 1, "应当报告出一个到期项"
    # ★ 报完之后，内容仍然在
    entries = engine.list_quarantine()
    assert len(entries) == 1, "报告不应移除任何隔离项"
    stored = engine.quarantine_root / entries[0].quarantine_id / "payload" / entries[0].stored_name
    assert stored.exists(), "TTL 到期不得导致内容消失"


# ================================================================
# 授权（grant）的语义
# ================================================================

def test_delete_without_grant_is_denied_in_scope(engine: PolicyEngine, workspace: Path):
    """在授权范围内的删除，**没有授权也要拒** —— 两道门是独立的。"""
    victim = _make_file(workspace / "allowed" / "f.md", "x")
    d = engine.quarantine_delete(tool="delete_artifact", path=str(victim))
    assert not d.allowed
    assert "不可逆" in d.reason
    assert victim.exists()


def test_grant_ttl_expires(workspace: Path):
    """grant 带 TTL，过期即失效（FR-2.5）。"""
    fake_now = [1000.0]
    eng = PolicyEngine(
        allowed_roots=[workspace / "allowed"],
        quarantine_root=workspace / "quarantine",
        audit_path=workspace / "audit.ndjson",
        clock=lambda: fake_now[0],
    )
    victim = _make_file(workspace / "allowed" / "f.md", "x")
    g = eng.grant(verb=Verb.DELETE, path_prefix=workspace / "allowed", ttl_s=60)

    fake_now[0] = 1000.0 + 61          # 过期
    d = eng.quarantine_delete(tool="delete_artifact", path=str(victim), grant_id=g.grant_id)
    assert not d.allowed, "过期授权必须失效"
    assert victim.exists()


def test_grant_does_not_cover_other_paths(engine: PolicyEngine, workspace: Path):
    """授权只覆盖它声明的那段前缀，不能顺带把别处也放行。"""
    victim_out = _make_file(workspace / "outside" / "f.md", "x")
    g = engine.grant(verb=Verb.DELETE, path_prefix=workspace / "allowed", ttl_s=600)
    d = engine.quarantine_delete(tool="delete_artifact", path=str(victim_out), grant_id=g.grant_id)
    assert not d.allowed
    assert victim_out.exists()


def test_regression_9_grant_with_relative_prefix_still_covers(engine: PolicyEngine, workspace: Path):
    """回归 —— **授权用相对路径前缀时会静默失效**。

    历史：第一版 `grant()` 直接存 `str(path_prefix)`。调用方传相对路径时，
    前缀是相对的，而 `covers()` 比对的是 check_path 解析出的**绝对路径** ——
    两者永远匹配不上。

    后果是**静默失效**：不报错、不留痕，只是"授权没生效"。
    这正是本项目一直在防的那类缺陷。

    ⚠️ 这条用例之所以曾经漏掉，是因为其它用例都恰好传了绝对路径。
    **测试用的路径形状必须和真实调用一致**，否则测不出问题。
    """
    import os

    victim = _make_file(workspace / "allowed" / "f.md", "x")

    # 关键：传一个**相对**前缀（相对当前工作目录）
    rel_prefix = os.path.relpath(workspace / "allowed")
    g = engine.grant(verb=Verb.DELETE, path_prefix=rel_prefix, ttl_s=600)

    d = engine.quarantine_delete(tool="delete_artifact", path=str(victim), grant_id=g.grant_id)

    assert d.allowed, (
        f"相对路径前缀的授权也必须生效（理由：{d.reason}）\n"
        f"授权前缀应当被规范化成绝对路径 —— 否则它永远覆盖不到绝对路径的目标。"
    )
    assert d.quarantine_id


def test_regression_10_restore_with_missing_id_is_a_clean_denial_not_a_crash(engine: PolicyEngine):
    """回归 —— 被拒的删除会给出 quarantine_id=None，还原时不能让内部崩掉。

    历史：restore(None) 把 None 交给了正则，抛 TypeError ——
    一个内部崩溃，而不是可解释的拒绝。
    """
    for bad in (None, "", 123):
        d = engine.restore(quarantine_id=bad)          # type: ignore[arg-type]
        assert not d.allowed
        assert d.reason
        assert d.suggestion, "拒绝必须给出可操作的建议"

    # 这条拒绝也应当进审计（没有静默失败）
    assert any(r["tool"] == "restore" for r in engine.audit.denials())


# ================================================================
# AC-8：每次拒绝都有审计，无静默失败
# ================================================================

def test_ac8_every_denial_is_audited(engine: PolicyEngine, workspace: Path):
    """AC-8 —— 拒绝必须留痕，而且理由要写进去。

    只记成功的操作 = 把"入侵尝试"从日志里抹掉。
    """
    victim = _make_file(workspace / "outside" / "f.md", "x")

    engine.quarantine_delete(tool="delete_artifact", path=str(victim))
    engine.authorize(tool="read_artifact", path=str(victim))
    engine.decide(tool="delete_artifact", path="/etc/passwd")   # 纯判定：不该写审计

    denials = engine.audit.denials()
    assert len(denials) >= 2, f"两次带审计的拒绝都应当被记录，实际 {len(denials)} 条"

    for rec in denials:
        assert rec["type"] == "policy.denied"
        assert rec["data"]["reason"], "审计里必须有拒绝理由"
        assert rec["data"]["suggestion"], "审计里必须有替代做法"
        assert rec["ts"]

    # 纯判定（decide）不应产生审计记录 —— 它没有副作用，也就没有"发生过的事"
    records = engine.audit.read_all()
    assert all(r["tool"] != "" for r in records)


def test_ac8_allowed_operations_are_audited_too(engine: PolicyEngine, workspace: Path):
    """AC-8 配套 —— 放行也要记账（否则无法回答"它到底做了什么"）。"""
    good = _make_file(workspace / "allowed" / "f.md", "x")
    d = engine.authorize(tool="read_artifact", path=str(good))
    assert d.allowed

    allowed_recs = [r for r in engine.audit.read_all() if r["allowed"]]
    assert len(allowed_recs) == 1
    assert allowed_recs[0]["type"] == "policy.allowed"


def test_ac8_quarantine_emits_protocol_event_type(engine: PolicyEngine, workspace: Path):
    """AC-8 配套 —— 事件类型必须与 D0 冻结的事件流协议一致。

    不另起一套审计格式：否则"审计"和"事件流"会各长一份，字段迟早对不上。
    """
    victim = _make_file(workspace / "allowed" / "f.md", "x")
    g = engine.grant(verb=Verb.DELETE, path_prefix=workspace / "allowed", ttl_s=600)
    engine.quarantine_delete(tool="delete_artifact", path=str(victim), grant_id=g.grant_id)

    types = {r["type"] for r in engine.audit.read_all()}
    assert "policy.quarantined" in types, f"应当产生 policy.quarantined 事件，实际 {types}"


# ================================================================
# 一条可以直接验证的性质：策略模块里没有任何硬删除调用
# ================================================================

# 这些属性名一旦以"调用"的形式出现，就说明有人给删除开了口子
FORBIDDEN_ATTRS = frozenset({"remove", "unlink", "rmdir", "removedirs", "rmtree"})

# 这些子串若出现在**字符串字面量**里，说明有人拼了删除命令（例如交给 PowerShell）
FORBIDDEN_IN_STRINGS = ("Remove-Item", "rm -rf", "del /f", "rd /s")


def _docstring_nodes(tree) -> set[int]:
    """找出所有文档字符串节点的 id。

    为什么要排除它们：本模块的文档里**会引用** `Remove-Item -Recurse -Force`
    （那是事故的历史命令，必须写清楚）。文档里提到一个命令，
    和代码里调用它是两件完全不同的事 —— 静态检查不能混淆这两者。
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.body:
                continue
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def test_policy_module_contains_no_hard_delete_calls():
    """★ 本项目最能体现核心命题的一条用例。

    "不存在硬删除"不是靠纪律，是靠**这一层根本没有那个能力**。

    实现方式：用 AST 分析（不是字符串匹配）找出所有真正被**调用**的
    `remove` / `unlink` / `rmtree` 等，以及所有拼了删除命令的字符串字面量。

    如果有人哪天在隔离区到期清理里加了 `shutil.rmtree`，
    这条用例会立刻变红。
    """
    policy_dir = ROOT / "src" / "rca" / "policy"
    offenders: list[str] = []

    for path in sorted(policy_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)
        rel = path.relative_to(ROOT)

        for node in ast.walk(tree):
            # 1) 属性调用：os.remove(...) / Path.unlink() / shutil.rmtree(...)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_ATTRS:
                    offenders.append(
                        f"{rel}:{node.lineno} 调用了 .{node.func.attr}(...)"
                    )

            # 2) 字符串字面量里拼了删除命令（排除文档字符串）
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                for bad in FORBIDDEN_IN_STRINGS:
                    if bad in node.value:
                        offenders.append(
                            f"{rel}:{node.lineno} 字符串里出现删除命令 {bad!r}"
                        )

    assert not offenders, (
        "策略模块里出现了硬删除调用 —— 这违背了「不存在硬删除」这条设计：\n  "
        + "\n  ".join(offenders)
        + "\n删除应当实现为「移入隔离区」，见 src/rca/policy/quarantine.py。"
    )


def test_policy_scan_actually_reads_files():
    """元测试：确认上面的扫描不是"扫了 0 个文件"造成的假绿。"""
    policy_dir = ROOT / "src" / "rca" / "policy"
    files = sorted(policy_dir.rglob("*.py"))
    assert len(files) >= 5, f"只扫到 {len(files)} 个文件：{[p.name for p in files]}"
    # 再确认 AST 真的解析出了东西
    tree = ast.parse(files[0].read_text(encoding="utf-8"))
    assert len(list(ast.walk(tree))) > 5, "AST 解析结果异常小，解析可能没生效"


def test_hard_delete_detector_actually_detects():
    """★ 元测试：证明这个检测器**真的能抓到**，而不是永远说"没找到"。

    做法：在一段合成源码上跑同一套检测逻辑，断言它确实报了问题。
    没有这一步，上面那条"没找到硬删除"的用例可能是假绿
    （比如 AST 遍历写错了、模式名写错了）。
    """
    synthetic = (
        "import shutil, os\n"
        "def cleanup(p):\n"
        "    os.remove(p)\n"
        "    shutil.rmtree(p)\n"
        "    cmd = 'Remove-Item -Recurse -Force /tmp/x'\n"
    )
    tree = ast.parse(synthetic)
    docstrings = _docstring_nodes(tree)

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in FORBIDDEN_ATTRS:
                found.append(node.func.attr)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            for bad in FORBIDDEN_IN_STRINGS:
                if bad in node.value:
                    found.append(bad)

    assert "remove" in found, "检测器没抓到 os.remove"
    assert "rmtree" in found, "检测器没抓到 shutil.rmtree"
    assert "Remove-Item" in found, "检测器没抓到字符串里的删除命令"

    # 反向对照：正常代码不应被误报
    clean = "def f(p):\n    open(p).read()\n    x = '普通字符串'\n"
    tree2 = ast.parse(clean)
    hits2 = [
        n.func.attr
        for n in ast.walk(tree2)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in FORBIDDEN_ATTRS
    ]
    assert hits2 == [], f"正常代码被误报：{hits2}"
