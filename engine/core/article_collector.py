"""采集流水线：想题目 → 写文章 → 审阅。重大错误则放弃。"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from .client import ChatClient
from .config import parse_themes
from .dup import DupGuard, reask_level, units
from . import prompts   # 提示词文案（用户 2026-10-20：语言跟随界面）
from .storage import DatasetStore, count_words


@dataclass
class CollectStats:
    total: int = 0
    done: int = 0
    discarded: int = 0
    failed: int = 0
    inflight: int = 0
    started_at: float = 0.0
    messages: list[str] = field(default_factory=list)


class Collector:
    def __init__(
        self,
        titler: ChatClient,
        writer: ChatClient,
        reviewer: ChatClient,
        store: DatasetStore,
        *,
        themes: str,
        count: int,
        concurrency: int = 2,
        min_words: int = 600,
        max_words: int = 2000,
        discard_on_reject: bool = True,
        titler_system: str = "",
        writer_system: str = "",
        reviewer_system: str = "",
        lang: str = "zh",             # 界面语言（用户 2026-10-20）：中文界面出中文语料，英文界面出英文语料
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.titler = titler
        self.writer = writer
        self.reviewer = reviewer
        self.store = store
        self.themes = parse_themes(themes)
        self.count = max(1, int(count))
        self.concurrency = max(1, int(concurrency))
        self.min_words = max(0, int(min_words))
        self.max_words = max(self.min_words, int(max_words))
        self.discard_on_reject = bool(discard_on_reject)
        self.titler_system = titler_system or ""
        self.writer_system = writer_system or ""
        self.reviewer_system = reviewer_system or ""
        self.lang = prompts.lang_of(lang)
        self.on_event = on_event

        self._stop = threading.Event()
        self.stats = CollectStats(total=self.count)
        self.guard = DupGuard()          # 重复率守卫（用户 2026-10-21：≥20% 警告 / ≥40% 中止）

    # ---------- 控制 ----------
    def stop(self) -> None:
        self._stop.set()

    def _emit(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        if self.on_event:
            self.on_event(kind, payload or {})

    def _log(self, text: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        self.stats.messages.append(line)
        self._emit("log", {"text": line})

    # ---------- 运行 ----------
    def run(self) -> CollectStats:
        self.stats = CollectStats(total=self.count)
        self.stats.started_at = time.time()
        self._stop.clear()
        self.guard = DupGuard()
        self._emit("started", {"total": self.count, "mode": "pipeline"})

        try:
            self._run_stream()
        except Exception as exc:  # noqa: BLE001
            self._log(f"采集异常终止: {exc}")
            self._emit("error", {"message": str(exc), "trace": traceback.format_exc()})
        finally:
            elapsed = max(0.001, time.time() - self.stats.started_at)
            self._emit(
                "finished",
                {
                    "done": self.stats.done,
                    "discarded": self.stats.discarded,
                    "failed": self.stats.failed,
                    "elapsed": elapsed,
                },
            )
            self._log(
                f"采集结束：入库 {self.stats.done}/{self.stats.total}，"
                f"审阅丢弃 {self.stats.discarded}，失败 {self.stats.failed}，用时 {elapsed:.1f}s"
            )
        return self.stats

    def _run_stream(self) -> None:
        """流水线：一个 worker 走完 题目→写作→审阅→落盘/丢弃。"""
        limit = max(1, self.concurrency)
        api_slots = threading.Semaphore(limit)
        idx_lock = threading.Lock()
        cursor = {"i": 0}

        def process_one() -> None:
            while not self._stop.is_set():
                with idx_lock:
                    idx = cursor["i"]
                    if idx >= self.count:
                        return
                    cursor["i"] = idx + 1
                    self.stats.inflight += 1
                try:
                    if self._stop.is_set():
                        continue
                    item = self._pipeline_one(idx, api_slots)
                    if item is None:
                        continue
                    status = item.get("_status")
                    if status == "ok":
                        self.store.append(item["record"])
                        self.stats.done += 1
                        self._log(f"入库 #{self.stats.done}: {item['record']['title'][:48]}")
                        self._emit("record", {"record": item["record"]})
                        self._dup_check(item["record"].get("article") or "")
                    elif status == "discarded":
                        self.stats.discarded += 1
                        self.store.append_rejected(item["record"])
                        self._log(f"审阅未过，已丢弃: {item['record'].get('title', '')[:40]}")
                    else:
                        self.stats.failed += 1
                except Exception as exc:  # noqa: BLE001
                    self.stats.failed += 1
                    self._log(f"任务失败 #{idx + 1}: {exc}")
                finally:
                    with idx_lock:
                        self.stats.inflight -= 1
                    self._emit("progress", self._progress_payload())

        workers = min(limit, self.count)
        self._log(f"流水线启动：最多 {limit} 个并发任务（题目→写作→审阅）")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(process_one) for _ in range(workers)]
            try:
                for fut in as_completed(futs):
                    fut.result()
            except Exception as exc:  # noqa: BLE001
                self._log(f"线程异常: {exc}")
            finally:
                if self._stop.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
        self._emit("progress", self._progress_payload())

    # ---------- 单条流水线 ----------
    def _pipeline_one(self, index: int, api_slots: threading.Semaphore) -> dict[str, Any] | None:
        """返回 {"_status": ok|discarded|failed, "record": {...}} 或 None。"""
        if self._stop.is_set():
            return None

        with api_slots:
            if self._stop.is_set():
                return None
            title_item = self._make_title(index)
        if not title_item:
            return {"_status": "failed", "record": {}}

        if self._stop.is_set():
            return None

        with api_slots:
            if self._stop.is_set():
                return None
            article = self._write_article(title_item)
        if not article:
            return {"_status": "failed", "record": {}}

        if self._stop.is_set():
            return None

        with api_slots:
            if self._stop.is_set():
                return None
            review = self._review_article(title_item, article)

        record = dict(title_item)
        record["article"] = article
        record["word_count"] = count_words(article)
        record["review"] = review
        record["writer_model"] = self.writer.model
        record["reviewer_model"] = self.reviewer.model

        if not review.get("pass", False) and self.discard_on_reject:
            return {"_status": "discarded", "record": record}
        return {"_status": "ok", "record": record}

    def _pick_theme(self, index: int) -> str:
        if self.themes:
            return self.themes[index % len(self.themes)]
        return ""

    def _make_title(self, index: int) -> dict[str, Any] | None:
        theme = self._pick_theme(index)
        user = (prompts.title_user(self.lang, index, theme)
                + prompts.reask_hint(self.lang, reask_level(self.guard.rate())))

        messages = []
        if self.titler_system:
            messages.append({"role": "system", "content": self.titler_system})
        messages.append({"role": "user", "content": user})

        self._log(f"拟题中: {theme or '自由主题'}")
        try:
            title = self.titler.chat(messages).strip()
        except Exception as exc:  # noqa: BLE001
            self._log(f"拟题失败 #{index + 1}: {exc}")
            return None

        title = _clean_title(title)
        if not title:
            self._log(f"拟题结果为空 #{index + 1}")
            return None

        return {
            "id": time.strftime("%Y-%m-%d"),            # 采集日期（到日，用户 2026-10-13 口径）
            "index": index,
            "theme": theme,
            "title": title,
            "titler_model": self.titler.model,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _write_article(self, title_item: dict[str, Any]) -> str | None:
        title = title_item["title"]
        user = prompts.write_user(self.lang, title,
                                  prompts.length_hint(self.lang, self.min_words, self.max_words))

        messages = []
        if self.writer_system:
            messages.append({"role": "system", "content": self.writer_system})
        messages.append({"role": "user", "content": user})

        self._log(f"写作中: {title[:40]}")
        try:
            article = self.writer.chat(messages).strip()
        except Exception as exc:  # noqa: BLE001
            self._log(f"写作失败「{title[:32]}」: {exc}")
            return None

        article = _clean_article(article, title)
        if not article:
            self._log(f"文章为空「{title[:32]}」")
            return None

        wc = count_words(article)
        if self.min_words and wc < max(80, self.min_words // 3):
            self._log(f"文章过短（{wc} 字）「{title[:32]}」")
            return None

        return article

    def _review_article(self, title_item: dict[str, Any], article: str) -> dict[str, Any]:
        title = title_item["title"]
        user = prompts.article_review_user(self.lang, title, article)

        messages = []
        if self.reviewer_system:
            messages.append({"role": "system", "content": self.reviewer_system})
        messages.append({"role": "user", "content": user})

        self._log(f"审阅中: {title[:40]}")
        try:
            raw = self.reviewer.chat(messages)
        except Exception as exc:  # noqa: BLE001
            self._log(f"审阅失败「{title[:32]}」: {exc}")
            return {
                "pass": True,
                "score": None,
                "major_errors": [],
                "comment": f"审阅服务失败，默认放行：{exc}",
                "raw": "",
            }

        review = _parse_review(raw)
        if review.get("pass") is False:
            errs = review.get("major_errors") or []
            preview = "；".join(str(e) for e in errs[:3]) or review.get("comment", "")
            self._log(f"审阅未过「{title[:32]}」: {preview[:80]}")
        else:
            self._log(f"审阅通过「{title[:32]}」score={review.get('score')}")
        return review

    # ---------- 重复率（用户 2026-10-21）：≥20% 弹窗警告一次，≥40% 中止采集 ----------
    def _dup_check(self, text: str) -> None:
        rate = self.guard.add_many(units(text))
        level = self.guard.level()
        if level == "warn":
            self._log(f"[重复率] 已达 {rate * 100:.0f}%（≥20%），内容重复偏高")
            self._emit("dup_warn", {"rate": rate})
        elif level == "abort":
            self._log(f"[重复率] 已达 {rate * 100:.0f}%（≥40%），中止采集（已收集内容保留）")
            self._emit("dup_abort", {"rate": rate})
            self._stop.set()

    def _progress_payload(self) -> dict[str, Any]:
        finished = self.stats.done + self.stats.discarded + self.stats.failed
        return {
            "total": self.stats.total,
            "done": self.stats.done,
            "discarded": self.stats.discarded,
            "failed": self.stats.failed,
            "inflight": self.stats.inflight,
            "finished": finished,
            "stopped": self._stop.is_set(),
            "dup_rate": self.guard.rate(),
        }


# ---------- 文本清理 / 审阅解析 ----------

def _clean_title(text: str) -> str:
    t = (text or "").strip()
    for a, b in (('"', '"'), ("“", "”"), ("「", "」"), ("『", "』"), ("《", "》")):
        if len(t) >= 2 and t.startswith(a) and t.endswith(b):
            t = t[1:-1].strip()
    if t and t[0].isdigit() and (". " in t[:4] or "、" in t[:4] or ") " in t[:4]):
        for sep in (". ", "、", ") ", "．"):
            if sep in t[:4]:
                t = t.split(sep, 1)[1].strip()
                break
    t = re.sub(r"^(标题|题目|文章标题)[:：]\s*", "", t)
    return t.strip()


def _clean_article(text: str, title: str = "") -> str:
    t = (text or "").strip()
    # 去掉首行若与标题重复
    if title:
        lines = t.splitlines()
        if lines and _clean_title(lines[0]) == _clean_title(title):
            t = "\n".join(lines[1:]).strip()
    t = re.sub(r"^(正文|文章|以下是正文)[:：]\s*", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _parse_review(raw: str) -> dict[str, Any]:
    """从审阅模型输出中解析 JSON 判定。解析失败时保守放行并标注。"""
    text = (raw or "").strip()
    # 剥离代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            text = m.group(0)

    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001
        return {
            "pass": True,
            "score": None,
            "major_errors": [],
            "comment": "审阅输出无法解析，默认放行",
            "raw": (raw or "")[:500],
        }

    if not isinstance(obj, dict):
        return {
            "pass": True,
            "score": None,
            "major_errors": [],
            "comment": "审阅输出不是对象，默认放行",
            "raw": (raw or "")[:500],
        }

    major = obj.get("major_errors") or obj.get("errors") or []
    if isinstance(major, str):
        major = [major] if major.strip() else []
    if not isinstance(major, list):
        major = [str(major)]

    score = obj.get("score")
    try:
        score = float(score) if score is not None else None
    except Exception:  # noqa: BLE001
        score = None

    pass_flag = obj.get("pass")
    if pass_flag is None:
        # 兼容 pass/ok/approved 等字段，或用 score 推断
        for key in ("ok", "approved", "acceptable", "通过"):
            if key in obj:
                pass_flag = bool(obj[key])
                break
    if pass_flag is None:
        if major:
            pass_flag = False
        elif score is not None and score < 6:
            pass_flag = False
        else:
            pass_flag = True

    return {
        "pass": bool(pass_flag),
        "score": score,
        "major_errors": [str(e) for e in major if str(e).strip()],
        "comment": str(obj.get("comment") or obj.get("summary") or obj.get("reason") or ""),
        "raw": (raw or "")[:500],
    }


def suggest_themes(n: int = 8) -> list[str]:
    pool = [
        "中国历史",
        "世界历史",
        "科学技术",
        "自然地理",
        "文学艺术",
        "哲学思想",
        "心理学",
        "经济学",
        "法律常识",
        "医学健康",
        "计算机与人工智能",
        "生态环境",
        "建筑与设计",
        "音乐与电影",
        "教育方法",
        "社会文化",
    ]
    random.shuffle(pool)
    return pool[: max(1, n)]
