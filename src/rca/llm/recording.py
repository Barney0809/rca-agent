"""
录制与回放 —— 让评测能被反复重跑，而不用每次都花钱。

============================ 为什么这一层很关键 ============================

评测要跑 **6 个场景 × 3 轮 = 18 次**，之后调参还要再跑很多次。
每次都要调 LLM，成本虽然不高（单场景约 ¥0.2），但有两个更麻烦的问题：

  1) **不可复现**：同样的输入，模型输出会有波动。
     调 prompt 时你分不清"是改动起作用了"还是"这次运气好"。
  2) **面试官跑不了**：他没有 API Key，就无法复现你的数字。

录制回放同时解决这两个问题：

    第一次跑     → 真实调用，并把请求+响应录下来
    之后每次跑   → 直接读录制结果，**零成本、完全一致**

录制的 key 是 `(tag, model, messages)` 的哈希 —— 只要输入一样，就一定命中回放。

============================ 与 FR-C.3 的关系 ============================

需求 FR-C.3：**LLM 响应录制/回放 —— 访客无需 API Key 即可离线复跑评测。**

本模块就是它的实现。录制文件随仓库提供（脱敏后），
于是"陌生人克隆 → 一条命令 → 复现全部数字"这条验收标准才成立。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class Recorder:
    """NDJSON 形式的录制器。

    文件格式：一行一个 JSON，每行含 key + 请求摘要 + 完整响应。
    """

    def __init__(self, path: Path, *, mode: str = "auto", batch: str = "") -> None:
        """
        mode:
            "record" —— 只录，不查（用于首次生成录制）
            "replay" —— 只查，不录；没命中就报错（用于保证可复现）
            "auto"   —— 先查，未命中则真实调用并录下来（默认，最省心）

        batch（2026-09-25，D19 补完「一批录制 = 一次运行」）：
            本次运行属于哪一批。**留空 = 未标注**（旧文件的语义，行为完全不变）。

            为什么需要它：录制文件是**追加**的，同一个 key 会被后来的运行反复写入。
            原来"后写者胜"意味着——**回放复现的是录制里最后一次运行**，
            而不是你想复现的那一次（实测过：同一批响应下，归档判 19/21、回放判 21/21，
            21 次尝试里 13 次步骤数不同）。

            ⇒ 录的时候标上批次，回放的时候**指定同一批次**：
              指定了就只认它（找不到就报未命中，**绝不**退而用别的批次）。
        """
        if mode not in ("record", "replay", "auto"):
            raise ValueError(f"未知的录制模式：{mode}")
        self.path = path
        self.mode = mode
        self.batch = batch
        # key → 命中该 key 的所有条目（**可能来自不同批次**，所以是一个列表）
        self._index: dict[str, list[dict]] = {}
        self._hits = 0
        self._misses = 0
        self._load()

    # ---------------------------------------------------------- 内部
    def _load(self) -> None:
        if not self.path.exists():
            return
        for raw in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            key = rec.get("key")
            if key:
                self._index.setdefault(key, []).append(rec)

    def _pick(self, candidates: list[dict]) -> dict | None:
        """从同一个 key 的多个条目里挑一个。

        ⚠️ 指定了批次**只**认那个批次：找不到就返回 None（调用方按未命中处理）。
           绝不能"退而用别的批次" —— 那等于静默串批，又回到了修复前的毛病。
        """
        if not candidates:
            return None
        if self.batch:
            exact = [r for r in candidates if str(r.get("batch") or "") == self.batch]
            return exact[-1] if exact else None
        return candidates[-1]        # 未指定批次：保持旧行为（后写者胜）

    @staticmethod
    def make_key(*, tag: str, model: str, messages: list[dict]) -> str:
        """由输入算出稳定 key。

        ⚠️ 必须 `sort_keys=True` —— 否则 dict 的键序会随实现变化，
           同样的输入算出不同的哈希，回放永远命中不了。
        """
        payload = json.dumps(
            {"tag": tag, "model": model, "messages": messages},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    # ---------------------------------------------------------- 对外
    def lookup(self, *, tag: str, model: str, messages: list[dict]) -> dict | None:
        """查录制。命中返回响应字典，否则 None。"""
        if self.mode == "record":
            return None
        key = self.make_key(tag=tag, model=model, messages=messages)
        rec = self._pick(self._index.get(key, []))
        if rec is None:
            self._misses += 1
            if self.mode == "replay":
                which = f"，批次={self.batch!r}" if self.batch else ""
                raise RuntimeError(
                    f"回放模式下未命中录制（tag={tag!r}, model={model!r}{which}）。\n"
                    f"  这说明这次请求的输入与录制时不同 —— 可能是 prompt 改了，\n"
                    f"  也可能是场景数据变了；指定了批次时，还可能是**这一批里没有这条**。\n"
                    f"  若确实该有新输入，请用 auto 模式重录。"
                )
            return None
        self._hits += 1
        return rec.get("response")

    def save(
        self, *, tag: str, model: str, messages: list[dict], response: dict
    ) -> None:
        """把一次真实调用录下来。"""
        if self.mode == "replay":
            return
        key = self.make_key(tag=tag, model=model, messages=messages)
        # 同一批次里同一个 key 只写一次；**不同批次**的同一 key 各留一份（这正是批次的意义）
        existing = self._index.get(key, [])
        if any(str(r.get("batch") or "") == self.batch for r in existing):
            return
        record = {
            "key": key,
            "tag": tag,
            "model": model,
            "batch": self.batch,
            "n_messages": len(messages),
            "response": response,
        }
        existing.append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()

    # ---------------------------------------------------------- 统计
    @property
    def stats(self) -> dict:
        return {
            "entries": len(self._index),
            "hits": self._hits,
            "misses": self._misses,
            "mode": self.mode,
            "path": str(self.path),
        }
