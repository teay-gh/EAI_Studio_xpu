"""重复率守卫（用户 2026-10-21）：三个采集流水线共用一套判定，阈值写死不手动设。

口径：把收集到的文本切成「单元」（段落；切不出段落就整条算一个单元），
单元与之前已收单元做 4-gram 字符 shingle 的 Jaccard 相似度，> 0.6 记一个「重复单元」。
重复率 = 重复单元数 / 全部单元数（增量登记，随时可读）。

固定阈值（用户 2026-10-21）：
  ≥ 20% → 弹窗警告（一次）
  ≥ 40% → 中止采集（已收集的内容保留）
"""
from __future__ import annotations

import re
import threading

DUP_WARN = 0.20
DUP_ABORT = 0.40
DUP_LEVEL = 0.6            # 两个单元算「重复」的相似度门槛（比段落去重的 0.82 松，抓的是内容重复）

# 换问法阈值（用户 2026-10-21）：重复率过线就在出题/规划提示词后面追加「换一种问法」，
# 和规定字数一样直接补在提示词后面，用户不需要感知到这个机制。
REASK_LEVELS = (0.20, 0.25, 0.30, 0.35)


def reask_level(rate: float) -> int:
    """当前重复率落在第几档（0-4）：>20% / >25% / >30% / >35% 各升一档。"""
    n = 0
    for t in REASK_LEVELS:
        if rate > t:
            n += 1
    return n


_REASK_HINTS = (
    "",
    "（换一种{w}：换一个角度，句式不要和之前的雷同。）",
    "（换一种{w}：换全新的角度和句式，不要沿用之前的切入方式。）",
    "（换一种{w}：用完全不同的角度与表述结构，避开之前的切面。）",
    "（必须彻底换一种{w}：从完全不同的方向入手，句式和用词都要和之前不同。）",
)


def reask_hint(level: int, w: str = "问法") -> str:
    """给提示词追加的「换一种问法」后缀；level=0 返回空串。"""
    if level <= 0:
        return ""
    return _REASK_HINTS[min(4, int(level))].format(w=w)


def shingles(text: str, n: int = 4) -> set:
    s = re.sub(r"\s+", "", text or "")
    if not s:
        return set()
    if len(s) <= n:
        return {s}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def units(text: str) -> list[str]:
    """文本切单元：空行/换行分段，短于 20 字的段并回整条。"""
    t = text or ""
    parts = [p.strip() for p in re.split(r"\n\s*\n|\n", t)]
    parts = [p for p in parts if len(p) >= 20]
    return parts if parts else ([t.strip()] if t.strip() else [])


class DupGuard:
    """线程安全的增量重复率计。"""

    def __init__(self, warn: float = 0.0, abort: float = 0.0) -> None:
        self._lock = threading.Lock()
        self._kept: list = []        # 已收单元的 shingle 集
        self.total = 0
        self.dup = 0
        self.warned = False
        self.aborted = False
        # 阈值可设（用户 2026-10-21：网页整理高级设置「重复率上限」）；上限=中止线，警告线取其一半
        self.abort_at = float(abort) if abort and abort > 0 else DUP_ABORT
        self.warn_at = float(warn) if warn and warn > 0 else self.abort_at / 2.0
        if self.warn_at >= self.abort_at:
            self.warn_at = self.abort_at / 2.0

    def add(self, text: str) -> float:
        sh = shingles(text)
        with self._lock:
            if sh:
                self.total += 1
                if any(jaccard(sh, o) > DUP_LEVEL for o in self._kept):
                    self.dup += 1
                else:
                    self._kept.append(sh)
            return self._rate()

    def add_many(self, texts) -> float:
        for t in texts or []:
            self.add(t)
        return self.rate()

    def _rate(self) -> float:
        return round(self.dup / float(max(1, self.total)), 3)

    def rate(self) -> float:
        with self._lock:
            return self._rate()

    def level(self) -> str:
        """"ok" | "warn" | "abort" —— 读完即视为已处理；abort 只吐一次（用户 2026-10-21：只提醒一次）。"""
        with self._lock:
            r = self._rate()
            if r >= self.abort_at:
                self.warned = True          # 直接中止就不再单独弹警告
                first = not self.aborted
                self.aborted = True
                return "abort" if first else "ok"
            if r >= self.warn_at:
                if self.warned:
                    return "ok"
                self.warned = True
                return "warn"
            return "ok"
