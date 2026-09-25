"""重放包（D19）的回归用例。

封堵的是什么（每一条都对应一个**真的会红**的场景）：

  1. `.log.gz` 与 `.log` 必须**逐字节等价** —— 一旦不等价，
     key 哈希就变了，回放会 miss（而且看起来像"模型/代码坏了"）。
  2. `RCA_REPLAY_ROOT` 必须真的改变数据根 —— 否则"陌生人零成本重放"
     只是文档里的一句话，实际还在读本机 runs/。
  3. 重放包必须能自检出**被改动/被删**的文件 —— 否则"包坏了"和
     "真的对不上"会混成一件事。
  4. `--mode replay` 必须**只读**：既不能写录制文件，也不能在未命中时
     偷偷退回真实调用（那会变成"以为没花钱、其实花了"）。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from eval import runner  # noqa: E402
from rca.llm.recording import Recorder  # noqa: E402
from rca.telemetry.collect import _read_log_text  # noqa: E402

PACK = ROOT / "runs" / "_replay_pack"


def _make_pack(root: Path, text: str = "2026-09-25 07:00:00 INFO [order] hello\n") -> Path:
    scenario = root / "r-20260925-000000"
    (scenario / "logs").mkdir(parents=True, exist_ok=True)
    (scenario / "logs" / "order.log.gz").write_bytes(gzip.compress(text.encode(), mtime=0))
    (scenario / "scenario.json").write_text(
        json.dumps({"fault_id": "F1", "run_id": "r-20260925-000000"}), encoding="utf-8"
    )
    files = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(root)).replace("\\", "/")
            files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    (root / "pack.json").write_text(
        json.dumps({"kind": "rca-agent replay pack", "files": files}, ensure_ascii=False),
        encoding="utf-8",
    )
    return scenario


# ---------------------------------------------------------------- 1) gz 等价

def test_gz_log_is_byte_equivalent_to_plain(tmp_path: Path) -> None:
    text = "2026-09-25 07:00:00 INFO [order] 下单成功 耗时=123ms\n" * 3
    run_dir = tmp_path / "r-x"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "logs" / "order.log").write_text(text, encoding="utf-8", newline="")

    assert _read_log_text(run_dir, "order") == text

    # 只有 .gz 时也要能读，且内容一模一样（重放包就是这样存放的）
    (run_dir / "logs" / "order.log").rename(run_dir / "logs" / "order.log.moved")
    (run_dir / "logs" / "order.log.gz").write_bytes(
        gzip.compress(text.encode("utf-8"), compresslevel=6, mtime=0)
    )
    assert _read_log_text(run_dir, "order") == text


def test_gz_prefers_plain_log_when_both_exist(tmp_path: Path) -> None:
    """两种形式同时存在时优先未压缩的 —— 本机跑的时候别去解压。"""
    run_dir = tmp_path / "r-x"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "logs" / "order.log").write_text("plain\n", encoding="utf-8")
    (run_dir / "logs" / "order.log.gz").write_bytes(gzip.compress(b"packed\n", mtime=0))
    assert _read_log_text(run_dir, "order") == "plain\n"


# ---------------------------------------------------------------- 2) 数据根

def test_data_root_honours_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RCA_REPLAY_ROOT", raising=False)
    assert runner.data_root() == runner.RUNS_DIR

    monkeypatch.setenv("RCA_REPLAY_ROOT", str(PACK))
    assert runner.data_root() == PACK


def test_discover_runs_reads_from_env_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_pack(tmp_path)
    monkeypatch.setenv("RCA_REPLAY_ROOT", str(tmp_path))
    found = runner.discover_runs(["F1"])
    assert list(found) == ["F1"]
    assert found["F1"].name == "r-20260925-000000"


# ---------------------------------------------------------------- 3) 包自检

def test_verify_replay_pack_accepts_intact_pack(tmp_path: Path) -> None:
    _make_pack(tmp_path)
    manifest = runner.verify_replay_pack(tmp_path)
    assert manifest["kind"] == "rca-agent replay pack"


def test_verify_replay_pack_rejects_tampered_file(tmp_path: Path) -> None:
    _make_pack(tmp_path)
    victim = tmp_path / "r-20260925-000000" / "logs" / "order.log.gz"
    victim.write_bytes(gzip.compress(b"tampered\n", mtime=0))
    with pytest.raises(RuntimeError, match="损坏"):
        runner.verify_replay_pack(tmp_path)


def test_verify_replay_pack_rejects_missing_file(tmp_path: Path) -> None:
    _make_pack(tmp_path)
    (tmp_path / "r-20260925-000000" / "scenario.json").rename(tmp_path / "moved.json")
    with pytest.raises(RuntimeError, match="缺失"):
        runner.verify_replay_pack(tmp_path)


def test_verify_replay_pack_rejects_pack_without_manifest(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="清单"):
        runner.verify_replay_pack(tmp_path)


# ---------------------------------------------------------------- 4) 回放只读

def test_replay_never_writes_and_never_falls_back(tmp_path: Path) -> None:
    rec = tmp_path / "replay-x.ndjson"
    recorder = Recorder(rec, mode="replay")

    # 未命中必须**报错**，不能返回 None 让调用方去真实调用
    with pytest.raises(RuntimeError, match="未命中"):
        recorder.lookup(tag="t", model="m", messages=[{"role": "user", "content": "hi"}])

    # 即使有人调用 save，也不能写盘
    recorder.save(
        tag="t", model="m", messages=[{"role": "user", "content": "hi"}],
        response={"choices": []},
    )
    assert not rec.exists(), "回放模式不得写入录制文件"


# ---------------------------------------------------------------- 5) 仓库里的包

@pytest.mark.skipif(not (PACK / "pack.json").exists(), reason="仓库未附带重放包")
def test_committed_replay_pack_is_intact() -> None:
    manifest = runner.verify_replay_pack(PACK)
    scenario_ids = {v["run_id"] for v in manifest["scenarios"].values()}
    assert len(scenario_ids) >= 1
    # 目录名就是 tag 的一部分：改名等于让回放 miss，所以必须在清单里对得上
    for name in scenario_ids:
        assert (PACK / name).is_dir(), f"清单里的场景目录不存在：{name}"


# ---------------------------------------------------------------- 6) 回放不得冒充测量

def test_demo_page_never_publishes_a_replay_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """交付页的"最完整的一次运行"必须跳过 replay 存档。

    这条是真被抓到过的：回放跑通后（21 次尝试），页面上的准确率与成本
    当场被它顶掉 —— 而回放按定义每次结果都一样，它**不是测量**。
    """
    from scripts import make_demo

    eval_dir = tmp_path / "_eval"
    for name, mode, n in (("multi-20260925-131953", "record", 21), ("multi-20260925-160104", "replay", 21)):
        d = eval_dir / name
        d.mkdir(parents=True)
        (d / "results.json").write_text(
            json.dumps({"mode": mode, "attempts": [{} for _ in range(n)]}), encoding="utf-8"
        )

    monkeypatch.setattr(make_demo, "EVAL_DIR", eval_dir)
    picked = make_demo.latest("multi-*/results.json")
    assert picked is not None
    assert picked.parent.name == "multi-20260925-131953", "回放存档被当成了测量"
