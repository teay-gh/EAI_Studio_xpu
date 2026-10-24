"""采集流水线：出题 → 答题 → 审阅 → 落盘/丢弃。支持取消与并发。"""

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
from .config import parse_topics
from .dup import DupGuard, reask_level, units
from . import prompts   # 提示词文案（用户 2026-10-20：语言跟随界面）
from .storage import DatasetStore


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
        generator: ChatClient,
        responder: ChatClient,
        store: DatasetStore,
        *,
        reviewer: ChatClient | None = None,
        topics: str,
        count: int,
        concurrency: int = 2,
        mode: str = "stream",
        auto_topic: bool = True,
        generator_system: str = "",
        responder_system: str = "",
        reviewer_system: str = "",
        discard_on_reject: bool = True,
        min_words: int = 0,          # 答案最少字数（用户 2026-10-19：与语料蒸馏一样在采集中心调）
        max_words: int = 0,          # 答案最多字数（作为提示词约束）
        lang: str = "zh",            # 界面语言（用户 2026-10-20）：中文界面出中文问答，英文界面出英文问答
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.generator = generator
        self.responder = responder
        self.reviewer = reviewer
        self.store = store
        self.topics = parse_topics(topics)
        self.count = max(1, int(count))
        self.concurrency = max(1, int(concurrency))
        self.mode = mode if mode in ("stream", "batch") else "stream"
        self.auto_topic = bool(auto_topic)
        self.generator_system = generator_system or ""
        self.responder_system = responder_system or ""
        self.reviewer_system = reviewer_system or ""
        self.discard_on_reject = bool(discard_on_reject)
        self.min_words = max(0, int(min_words or 0))
        _mx = max(0, int(max_words or 0))
        # 0 = 不限（用户口径：只填最少字数时，只按下限提示，不再拼成「50-50」）
        self.max_words = max(self.min_words, _mx) if _mx else 0
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

    LOG_KEEP = 50        # 运行日志只留最近 50 条（用户 2026-10-19）

    def _log(self, text: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        msgs = self.stats.messages
        msgs.append(line)
        if len(msgs) > self.LOG_KEEP:          # 长跑时不要把整轮日志全攒在内存里
            del msgs[:-self.LOG_KEEP]
        self._emit("log", {"text": line})

    # ---------- 运行 ----------
    def run(self) -> CollectStats:
        self.stats = CollectStats(total=self.count)
        self.stats.started_at = time.time()
        self._stop.clear()
        self.guard = DupGuard()
        self._emit("started", {"total": self.count, "mode": self.mode})

        try:
            if self.mode == "batch":
                self._run_batch()
            else:
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
            # 结束汇总由 finished 事件统一打印（用户 2026-10-19：原来这里和前端各打一条，明细里重复）
            self.stats.messages.append(
                f"[{time.strftime('%H:%M:%S')}] 采集结束：入库 {self.stats.done}/{self.stats.total}，"
                f"审阅丢弃 {self.stats.discarded}，失败 {self.stats.failed}，用时 {elapsed:.1f}s"
            )
            if len(self.stats.messages) > self.LOG_KEEP:
                del self.stats.messages[:-self.LOG_KEEP]
        return self.stats

    def _run_stream(self) -> None:
        """流水线：出题后立刻答题。

        并发 N = 最多 N 个「未完成」任务（出题中/答题中/待答都算）。
        未完成数满 N 时，不会再出新题。
        """
        limit = max(1, self.concurrency)
        api_slots = threading.Semaphore(limit)
        idx_lock = threading.Lock()
        cursor = {"i": 0}

        def process_one() -> None:
            """一个 worker 负责一整条：出题→答题→落盘。占用期间算 1 个未完成。"""
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
                        self._log(f"入库 #{self.stats.done}: {item['record']['question'][:48]}")
                        self._emit("record", {"record": item["record"]})
                        self._dup_check(item["record"].get("answer") or "")
                    elif status == "discarded":
                        self.stats.discarded += 1
                        self.store.append_rejected(item["record"])
                        self._log(f"审阅未过，已丢弃: {item['record'].get('question', '')[:40]}")
                    else:
                        self.stats.failed += 1
                except Exception as exc:  # noqa: BLE001
                    self.stats.failed += 1
                    self._log(f"任务失败 #{idx + 1}: {exc}")
                finally:
                    with idx_lock:
                        self.stats.inflight -= 1
                    self._emit("progress", self._progress_payload())

        # worker 数 == 并发上限：队列不会无限堆题
        workers = min(limit, self.count)
        self._log(f"流水线启动：最多 {limit} 个未完成任务（出题/答题交错，满则暂停出题）")
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
            q_item = self._make_question(index)
        if not q_item:
            return {"_status": "failed", "record": {}}

        if self._stop.is_set():
            return None

        with api_slots:
            if self._stop.is_set():
                return None
            record = self._answer_question(q_item)
        if not record:
            return {"_status": "failed", "record": q_item}

        if self._stop.is_set():
            return None

        if self.reviewer is not None:
            with api_slots:
                if self._stop.is_set():
                    return None
                review = self._review_answer(record)
            record["review"] = review
            record["reviewer_model"] = self.reviewer.model
            if not review.get("pass", False) and self.discard_on_reject:
                return {"_status": "discarded", "record": record}

        return {"_status": "ok", "record": record}

    def _run_batch(self) -> None:
        """两阶段：先出完全部问题，再统一答题。"""
        limit = max(1, self.concurrency)
        api_slots = threading.Semaphore(limit)
        workers = min(self.count, limit * 2 + 2)
        self._log("阶段 1/2：批量生成问题…")
        questions: list[dict[str, Any]] = []
        lock = threading.Lock()

        def gen_worker(start: int, step: int) -> None:
            i = start
            while i < self.count and not self._stop.is_set():
                try:
                    with api_slots:
                        q = self._make_question(i)
                    if q:
                        with lock:
                            questions.append(q)
                    else:
                        with lock:
                            self.stats.failed += 1
                except Exception as exc:  # noqa: BLE001
                    self.stats.failed += 1
                    self._log(f"出题失败 #{i + 1}: {exc}")
                self._emit("progress", self._progress_payload())
                i += step

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(gen_worker, w, min(workers, self.count)) for w in range(min(workers, self.count))]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    self.stats.failed += 1
                    self._log(f"出题失败: {exc}")

        if self._stop.is_set() or not questions:
            return

        self._log(f"阶段 2/2：批量回答 {len(questions)} 条问题…")
        cursor = {"i": 0}

        def ans_worker() -> None:
            while not self._stop.is_set():
                with lock:
                    idx = cursor["i"]
                    if idx >= len(questions):
                        return
                    cursor["i"] = idx + 1
                    item = questions[idx]
                try:
                    with api_slots:
                        if self._stop.is_set():
                            return
                        self.stats.inflight += 1
                        try:
                            record = self._answer_question(item)
                        finally:
                            self.stats.inflight -= 1
                    if not record:
                        self.stats.failed += 1
                        continue
                    if self.reviewer is not None:
                        with api_slots:
                            if self._stop.is_set():
                                return
                            review = self._review_answer(record)
                        record["review"] = review
                        record["reviewer_model"] = self.reviewer.model
                        if not review.get("pass", False) and self.discard_on_reject:
                            self.stats.discarded += 1
                            self.store.append_rejected(record)
                            self._log(f"审阅未过，已丢弃: {record.get('question', '')[:40]}")
                            continue
                    self.store.append(record)
                    self.stats.done += 1
                    self._log(f"入库 #{self.stats.done}: {record['question'][:48]}")
                    self._emit("record", {"record": record})
                    self._dup_check(record.get("answer") or "")
                except Exception as exc:  # noqa: BLE001
                    self.stats.failed += 1
                    self._log(f"答题失败: {exc}")
                finally:
                    self._emit("progress", self._progress_payload())

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(ans_worker) for _ in range(workers)]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    self._log(f"答题异常: {exc}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(ans_worker) for _ in range(workers)]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    self._log(f"答题异常: {exc}")

    # ---------- 单条 ----------
    def _make_pair(self, index: int) -> None:
        if self._stop.is_set():
            return
        self._emit("progress", self._progress_payload())
        q_item = self._make_question(index)
        if not q_item:
            self.stats.failed += 1
            self._emit("progress", self._progress_payload())
            return
        if self._stop.is_set():
            return
        record = self._answer_question(q_item)
        if record:
            self.store.append(record)
            self.stats.done += 1
            self._log(f"完成 #{self.stats.done}: {record['question'][:48]}")
            self._emit("record", {"record": record})
        else:
            self.stats.failed += 1
        self._emit("progress", self._progress_payload())

    def _pick_topic(self, index: int) -> str:
        if self.topics:
            return self.topics[index % len(self.topics)]
        return ""

    def _words_hint(self, for_questioner: bool = False) -> str:
        """采集中心「字数区间」的一句话提示（用户 2026-10-19 / 2026-10-20）。

        出题模型和答题模型都发一份：出题侧用来控制问题该有多大（答得进这个篇幅），
        答题侧用来控制回答长度。语言随界面（用户 2026-10-20）：中文界面说「字」，英文界面说 words。
        """
        return prompts.answer_hint(self.lang, self.min_words, self.max_words, for_questioner)

    def _make_question(self, index: int) -> dict[str, Any] | None:
        topic = self._pick_topic(index)
        hint = self._words_hint(True)          # 字数配置给出题模型也发一份（用户 2026-10-20）
        if not self.auto_topic and not topic:
            self._log(f"#{index + 1} 无主题且关闭了自动出题，跳过")
            return None
        user = prompts.question_user(
            self.lang, index, topic, self.auto_topic, hint,
            prompts.reask_hint(self.lang, reask_level(self.guard.rate())),   # 重复率过线追加「换一种问法」（用户 2026-10-21：对用户不可见）
        )

        messages = []
        if self.generator_system:
            messages.append({"role": "system", "content": self.generator_system})
        messages.append({"role": "user", "content": user})

        self._log(f"出题中: {topic or '自由主题'}")
        try:
            question = self.generator.chat(messages).strip()
        except Exception as exc:  # noqa: BLE001
            self._log(f"出题失败 #{index + 1}: {exc}")
            return None

        question = _clean_question(question)
        if not question:
            self._log(f"出题结果为空 #{index + 1}")
            return None

        return {
            "id": time.strftime("%Y-%m-%d"),            # 采集日期（到日，用户 2026-10-13 口径）
            "index": index,
            "topic": topic,
            "question": question,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _answer_question(self, q_item: dict[str, Any]) -> dict[str, Any] | None:
        # 字数区间（用户 2026-10-19）：与语料蒸馏同一口径 —— 区间写进提问，过短的答案丢弃
        hint = self._words_hint(False)
        messages = []
        if self.responder_system:
            messages.append({"role": "system", "content": self.responder_system})
        messages.append({"role": "user", "content": q_item["question"] + hint})

        self._log(f"回答中: {q_item['question'][:40]}")
        try:
            answer = self.responder.chat(messages).strip()
        except Exception as exc:  # noqa: BLE001
            self._log(f"答题失败「{q_item['question'][:32]}」: {exc}")
            return None

        if not answer:
            self._log(f"答题为空「{q_item['question'][:32]}」")
            return None

        if self.min_words:
            from .storage import count_words
            wc = count_words(answer)
            if wc < max(10, self.min_words // 3):
                self._log(f"回答过短（{wc} 字）「{q_item['question'][:32]}」")
                return None

        record = dict(q_item)
        record["answer"] = answer
        record["generator_model"] = self.generator.model
        record["responder_model"] = self.responder.model
        return record

    def _review_answer(self, record: dict[str, Any]) -> dict[str, Any]:
        if self.reviewer is None:
            return {"pass": True, "score": None, "major_errors": [], "comment": "未启用审阅", "raw": ""}

        question = record.get("question", "")
        answer = record.get("answer", "")
        user = prompts.resp_review_user(self.lang, question, answer)

        messages = []
        if self.reviewer_system:
            messages.append({"role": "system", "content": self.reviewer_system})
        messages.append({"role": "user", "content": user})

        self._log(f"审阅中: {question[:40]}")
        try:
            raw = self.reviewer.chat(messages)
        except Exception as exc:  # noqa: BLE001
            self._log(f"审阅失败「{question[:32]}」: {exc}")
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
            self._log(f"审阅未过「{question[:32]}」: {preview[:80]}")
        else:
            self._log(f"审阅通过「{question[:32]}」score={review.get('score')}")
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


def _clean_question(text: str) -> str:
    t = text.strip()
    # 去掉常见包裹
    for a, b in (('"', '"'), ("“", "”"), ("「", "」"), ("『", "』")):
        if len(t) >= 2 and t.startswith(a) and t.endswith(b):
            t = t[1:-1].strip()
    # 去掉编号前缀
    if t and t[0].isdigit() and (". " in t[:4] or "、" in t[:4] or ") " in t[:4]):
        for sep in (". ", "、", ") ", "．"):
            if sep in t[:4]:
                t = t.split(sep, 1)[1].strip()
                break
    return t.strip()


def _parse_review(raw: str) -> dict[str, Any]:
    """从审阅模型输出中解析 JSON 判定。解析失败时保守放行并标注。"""
    text = (raw or "").strip()
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


def suggest_topics(n: int = 8) -> list[str]:
    pool = [
        "机器学习基础概念",
        "深度学习与神经网络",
        "自然语言处理",
        "计算机视觉",
        "强化学习",
        "大语言模型",
        "数据结构与算法",
        "操作系统",
        "计算机网络",
        "数据库系统",
        "概率论与数理统计",
        "线性代数",
        "软件工程",
        "分布式系统",
        "云计算与容器",
        "信息安全",
    ]
    random.shuffle(pool)
    return pool[: max(1, n)]
