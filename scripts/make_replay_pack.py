"""把「录制 + 场景数据」打成一个可分享的**便携重放包**（2026-09-25，D19）。

============================ 为什么需要它 ============================

`--mode replay` 早就实现了，但**从来没有真正跑通过**。原因不在回放代码，
而在 key 的构成：

    key = sha256(tag + model + messages)

而 multi 的 tag 里嵌着**场景目录名**（`specialist/metrics/r-20260925-072345`），
场景目录名是一次故障注入产生的**时间戳**。⇒ 换一批场景目录，key 必然变，
回放第一步就 miss（实测见 `runs/_replay_probe.log`）。

所以「零成本重放」的完整前提是：

    录制文件  +  **同一批场景目录（同名同内容）**

场景日志原始体积 24.9 MB（7 个场景），不能直接入库；
gzip 后实测 2.66 MB（约 9:1），于是做成这个包。

============================ 包里有什么 ============================

    _replay_pack/
      pack.json                        清单（每个文件的 sha256 + 来源）
      _recordings/replay-<model>.ndjson  LLM 录制（回放的唯一数据源）
      <run_id>/scenario.json           场景元数据（含标准答案，**不给 Agent**）
      <run_id>/metrics-{before,after}.json
      <run_id>/changes.ndjson
      <run_id>/logs/<service>.log.gz   压缩后的日志（collect.py 直接读）

⚠️ 目录名必须**原样保留** —— 它就是 tag 的一部分，改名等于让回放 miss。

============================ 怎么用 ============================

    1) 打（本机，要有场景目录 + 录制）
       python scripts/make_replay_pack.py

    2) 重放（任何人，不需要 API Key、不花一分钱）
       $env:RCA_REPLAY_ROOT = "runs/_replay_pack"     # PowerShell
       python eval/runner.py --agent multi --rounds 3 --mode replay

    3) 自检：包里任何一个字节被改动、或少了文件，回放开始前就会**报错拒绝**，
       而不是跑到一半 miss（把「包坏了」和「真的对不上」分开）。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from eval.runner import RUNS_DIR, discover_runs  # noqa: E402

SERVICES = ("order", "inventory", "payment")
SMALL_FILES = ("scenario.json", "metrics-before.json", "metrics-after.json", "changes.ndjson")
COMPRESS_LEVEL = 6


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_gz(src: Path, dst: Path) -> tuple[int, int]:
    """压缩写入，并返回 (原始字节数, 压缩后字节数)。

    两处细节不能少：
      - `mtime=0`：否则 gz 头里嵌着压缩时刻，同样的输入每次算出不同哈希，
        清单会无端变化、自检会误报。**可复现**在这里是硬要求。
      - `newline` 无关：走二进制读、二进制写，不做任何换行转换。
    """
    raw = src.read_bytes()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(gzip.compress(raw, compresslevel=COMPRESS_LEVEL, mtime=0))
    return len(raw), dst.stat().st_size


def _write_text_lf(src: Path, dst: Path) -> None:
    """把文本**按 LF 换行**写进包里（2026-09-25，D20b：这是 #39 复发）。

    为什么必须归一化：
    `.gitattributes` 里是 `eol=lf`，所以 git 在克隆/检出时会把 CRLF 改成 LF。
    而包自检比的是 **sha256**：如果清单按 CRLF 的字节算，
    那么**本机一切正常**（磁盘上就是 CRLF），克隆里却每个文件都"内容不一致" ——
    包对外就是坏的。实测发生过：两个 `scenario.json` 在干净克隆里对不上哈希。

    ⇒ 写的时候就统一成 LF，清单自然按"克隆里拿到的字节"算。
    """
    text = src.read_text(encoding="utf-8", errors="replace")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8", newline="\n")


def _trim_recording(src: Path, dst: Path, run_ids: set[str]) -> tuple[int, int]:
    """把录制裁到"这次打包真正会用到"的条目，返回 (保留, 丢弃)。

    为什么需要裁（2026-09-25，D20b）：
    录制文件是**追加**的，里面混着多次运行、多个场景目录的响应（实测 643 条）。
    只装 2 个场景却带上全部条目 ⇒ 包里 90% 是永远命不中的数据。
    要把它随仓库提交（让陌生人克隆后就能零成本重放），就必须裁。

    规则：tag 的最后一段如果是 `r-…`（场景目录名），它必须在**这次打包的**场景里；
    `coordinator` 这类**不带场景目录**的 tag 一律保留
    —— 它们是多 Agent 流程的收尾环节，少了会让回放中途 miss。
    """
    kept = dropped = 0
    out: list[str] = []
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        leaf = str(rec.get("tag") or "").split("/")[-1]
        if leaf.startswith("r-") and leaf not in run_ids:
            dropped += 1
            continue
        out.append(line)
        kept += 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    return kept, dropped


def build(
    out_dir: Path,
    *,
    faults: list[str] | None,
    model: str,
    keep_all_recording: bool = False,
) -> dict:
    if os.environ.get("RCA_REPLAY_ROOT", "").strip():
        raise SystemExit(
            "检测到 RCA_REPLAY_ROOT 已设置 —— 打包必须从**本机真实 runs/** 打，\n"
            "否则会拿重放包去打重放包。请先清掉这个环境变量。"
        )

    picks = discover_runs(faults)
    if not picks:
        raise SystemExit("没有找到任何场景目录。先跑：python scripts/inject_fault.py scenario F1")

    record_src = RUNS_DIR / "_recordings" / f"record-{model}.ndjson"
    if not record_src.exists():
        raise SystemExit(
            f"找不到录制：{record_src}\n"
            f"  录制必须用真实调用生成：\n"
            f"    python eval/runner.py --agent multi --rounds 3 --mode record"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    scenarios: dict[str, dict] = {}
    raw_total = gz_total = 0

    for fid, run_dir in sorted(picks.items()):
        dst_dir = out_dir / run_dir.name
        # ⚠️ 目录名原样保留：它是 tag 的一部分。
        for name in SMALL_FILES:
            src = run_dir / name
            if not src.exists():
                continue
            dst = dst_dir / name
            _write_text_lf(src, dst)      # ⚠️ 必须 LF：见 _write_text_lf 的说明（#39 复发）
            files[str(dst.relative_to(out_dir)).replace("\\", "/")] = _sha256(dst)

        logs_raw = logs_gz = 0
        for svc in SERVICES:
            src = run_dir / "logs" / f"{svc}.log"
            if not src.exists():
                continue
            dst = dst_dir / "logs" / f"{svc}.log.gz"
            raw, gz = _write_gz(src, dst)
            logs_raw += raw
            logs_gz += gz
            files[str(dst.relative_to(out_dir)).replace("\\", "/")] = _sha256(dst)
        raw_total += logs_raw
        gz_total += logs_gz
        scenarios[fid] = {
            "run_id": run_dir.name,
            "source": str(run_dir.relative_to(ROOT)).replace("\\", "/"),
            "logs_raw_bytes": logs_raw,
            "logs_gz_bytes": logs_gz,
        }
        print(f"  {fid:4} {run_dir.name:22} 日志 {logs_raw/1e6:6.2f} MB → {logs_gz/1e6:5.2f} MB")

    rec_dst = out_dir / "_recordings" / f"replay-{model}.ndjson"
    run_ids = {v["run_id"] for v in scenarios.values()}
    if keep_all_recording:
        rec_dst.parent.mkdir(parents=True, exist_ok=True)
        rec_dst.write_bytes(record_src.read_bytes())
        entries = sum(1 for ln in rec_dst.read_text(encoding="utf-8").splitlines() if ln.strip())
        dropped = 0
    else:
        entries, dropped = _trim_recording(record_src, rec_dst, run_ids)
    files[str(rec_dst.relative_to(out_dir)).replace("\\", "/")] = _sha256(rec_dst)

    manifest = {
        "kind": "rca-agent replay pack",
        "version": 1,
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": model,
        "recording": str(rec_dst.relative_to(out_dir)).replace("\\", "/"),
        "recording_entries": entries,
        "recording_entries_dropped": dropped,
        "scenarios": scenarios,
        "totals": {"logs_raw_bytes": raw_total, "logs_gz_bytes": gz_total},
        "files": files,
        "notes": [
            "场景数据来自本机 runs/<run_id>/，目录名与 tag 绑定，改名会让回放 miss。",
            "日志以 .gz 存放，collect.py 直接读；两种形式内容必须逐字节一致。",
            "录制默认**裁剪**：只保留本次打包场景用到的条目（外加 coordinator 这类不带场景目录的收尾环节）。",
            "重放不需要 API Key，也不会产生任何费用（录制未命中会直接报错，不会退回真实调用）。",
        ],
    }
    (out_dir / "pack.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    print()
    print(f"  日志合计    {raw_total/1e6:.2f} MB → {gz_total/1e6:.2f} MB")
    print(f"  录制        {entries} 条（裁掉 {dropped} 条）（{rec_dst.stat().st_size/1e6:.2f} MB）")
    print(f"  包           {out_dir}（{len(files)} 个受票据保护的文件）")
    print()
    print("  重放（零成本，不需要 API Key）：")
    print(f'    $env:RCA_REPLAY_ROOT = "{out_dir}"')
    print("    python eval/runner.py --agent multi --rounds 3 --mode replay")
    return manifest


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="打一个便携重放包")
    ap.add_argument("--out", default=str(RUNS_DIR / "_replay_pack"))
    ap.add_argument("--faults", nargs="*", default=None, help="只打这些场景（默认全部）")
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument(
        "--keep-all-recording", action="store_true",
        help="不裁剪录制（默认只保留本次打包场景用到的条目）",
    )
    args = ap.parse_args()

    print("=" * 96)
    print(f"  打重放包   输出={args.out}  模型={args.model}")
    print("=" * 96)
    build(
        Path(args.out), faults=args.faults, model=args.model,
        keep_all_recording=args.keep_all_recording,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
