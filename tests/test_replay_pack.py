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

PACK = ROOT / "replay_pack"   # ★ 随仓库提交的演示包（不是 runs/ 下那份本机包）


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

def test_bundled_replay_pack_is_intact() -> None:
    """随仓库提交的演示包必须完好 —— **这条用例不再 skip**。

    ⚠️ 为什么改了名（2026-09-25 自审时）：
    原来那条叫 `test_committed_replay_pack_is_intact`，却指向 `runs/_replay_pack`
    —— 那个包**并没提交**，于是它在干净克隆里只会 skip，名字却在说"已提交"。
    现在包真的随仓库提交（仓库根的 `replay_pack/`，1.73 MB，F1+F8），
    名字与事实一致，克隆里也真的执行。
    """
    manifest = runner.verify_replay_pack(PACK)
    assert manifest["scenarios"], "包里一个场景都没有"
    # 目录名就是 tag 的一部分：改名等于让回放 miss，所以必须在清单里对得上
    for name in {v["run_id"] for v in manifest["scenarios"].values()}:
        assert (PACK / name).is_dir(), f"清单里的场景目录不存在：{name}"


def test_replay_pack_is_read_only_for_every_other_mode() -> None:
    """只读包上只允许 replay —— 否则 `--mode record` 会往包里追加录制文件。

    自审时发现的真实缺口：`run()` 的录制路径也走 `data_root()`，
    所以设了 `RCA_REPLAY_ROOT` 之后 `--mode record` 会**写进只读产物**；
    而包自检只校验清单里列出的文件，**新增的文件它看不见** ⇒ 静默污染。
    """
    for mode in ("record", "live", "auto", ""):
        with pytest.raises(RuntimeError, match="只读重放包"):
            runner.check_replay_root_mode(mode)
    runner.check_replay_root_mode("replay")  # 不抛 = 允许


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


# ---------------------------------------------------------------- 7) 真的重放一次

def test_bundled_pack_replays_without_any_api_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 最强的一条：在"任何真实调用都必然失败"的环境里，真的重放一次。

    毒化两处：Key 换假的、`DEEPSEEK_BASE_URL` 指向 `127.0.0.1:9`（无人监听）。
    任何一次真实 HTTP 都会立刻失败 ⇒ **跑完就等于没走过网络**。

    它同时守着"录制裁得对不对"：包里那份录制裁掉了 390 条用不到的响应，
    少了任何一条必要的，这里都会 miss 并抛错。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-replay-only-not-a-real-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("RCA_REPLAY_ROOT", str(PACK))

    report = runner.run(fault_ids=["F1"], rounds=1, mode="replay", agent="multi", verbose=False)

    assert report.attempts, "一次尝试都没有 —— 包里的场景没被发现"
    a = report.attempts[0]
    assert a.correct, f"回放没答对（录制裁得不够？）：{a.root_cause[:150]}"
    assert a.cost_yuan > 0, "成本应按录制里的 token 用量折算出来（这不是花掉的钱）"
    assert a.steps > 0 and a.tool_calls > 0, "步数与工具调用数不该是 0"


def test_trimming_keeps_what_the_pack_needs(tmp_path: Path) -> None:
    """录制裁剪的两条边界（包里 643 条裁到 253 条，靠的就是这个规则）。

    必须保留：① 本次打包场景的条目 ② **不带场景目录**的 tag（`coordinator`
    是多 Agent 流程的收尾环节，按目录名过滤会把它们全滤掉 ⇒ 回放中途 miss）
    可以丢：其它场景目录的条目。
    """
    from scripts import make_replay_pack

    rows = [
        {"key": "k1", "tag": "specialist/metrics/r-20260924-054323", "response": {}},
        {"key": "k2", "tag": "crossexam/logs/r-20260925-072345", "response": {}},
        {"key": "k3", "tag": "coordinator", "response": {}},
        {"key": "k4", "tag": "specialist/change/r-20260924-054607", "response": {}},
        {"key": "k5", "tag": "baseline/r-20260924-054323", "response": {}},
    ]
    src = tmp_path / "record.ndjson"
    src.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )

    dst = tmp_path / "out" / "replay.ndjson"
    kept, dropped = make_replay_pack._trim_recording(
        src, dst, {"r-20260924-054323", "r-20260925-072345"}
    )

    assert (kept, dropped) == (4, 1), f"裁剪计数不对：kept={kept} dropped={dropped}"
    keys = [json.loads(ln)["key"] for ln in dst.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert keys == ["k1", "k2", "k3", "k5"], f"裁剪结果不对：{keys}"
