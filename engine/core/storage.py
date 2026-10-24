"""统一数据集存储：文章 / 问答。

一条采集任务只产出「一份能直接训练的文件」，避免同一份内容被数据集页扫到两次：

- 文章（record 里有 article）→ `<输出目录>/txt/<名>_<stamp>_articles.txt`（纯正文，预训练用）
  完整记录（标题/主题/评分/模型名）另存 `<输出目录>/.meta/<名>_<stamp>.jsonl`，
  以「.」开头 → 扫描器跳过，只给「数据管理」页读，不参与训练语料。
- 问答（record 里有 question）→ `<输出目录>/<名>_<stamp>.jsonl`（一条一行，微调用；合并时会转成 \\x01问\\x02答\\x03）。
- 被拒样本 → `<输出目录>/<名>_<stamp>_rejected.jsonl`（名字含 rejected，扫描器跳过；仅「保存并隔离」开时写）。
"""

from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from typing import Any

Q_MARK, A_MARK, E_MARK = "\x01", "\x02", "\x03"


def article_to_txt(record: dict[str, Any]) -> str:
    """文章训练文本：只保留正文（纯 txt，可直接作预训练语料）。"""
    body = str(record.get("article") or "").strip()
    return body


def pair_to_txt(record: dict[str, Any]) -> str:
    """问答训练文本：\\x01问\\x02答\\x03。"""
    q = str(record.get("question") or "").strip()
    a = str(record.get("answer") or "").strip()
    if not q or not a:
        return ""
    return f"{Q_MARK}{q}{A_MARK}{a}{E_MARK}"


class DatasetStore:
    def __init__(
        self,
        output_dir: str,
        dataset_name: str = "dataset",
        save_rejected: bool = False,
        kind: str = "mixed",
    ) -> None:
        self.output_dir = output_dir or "data"
        self.dataset_name = dataset_name or "dataset"
        self.save_rejected = bool(save_rejected)
        self.kind = kind
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._rejected: list[dict[str, Any]] = []
        self._stamp_paths(time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.meta_dir, exist_ok=True)
        # txt/ 不在这里建：问答线（响应蒸馏）从不写它，别在工程里留个空 txt 文件夹；
        # 文章线真要写正文时再按需创建（见 _ensure_txt_dir）。

    def _ensure_txt_dir(self) -> None:
        """正文目录按需创建（= 板块目录本身，2026-10-20 起不再单独建 txt/）。"""
        os.makedirs(self.txt_dir, exist_ok=True)

    def _stamp_paths(self, stamp: str) -> None:
        """按时间戳排定本轮产物路径（问答主产物 / 文章正文 / 文章记录 sidecar / 被拒）。"""
        self.stamp = stamp
        self.path = os.path.join(self.output_dir, f"{self.dataset_name}_{stamp}.jsonl")
        self.reject_path = os.path.join(self.output_dir, f"{self.dataset_name}_{stamp}_rejected.jsonl")
        # 2026-10-20（用户）：文章正文直接放板块目录，不再套 txt/ 子文件夹（和 chats/ 一样平铺）；
        # 记录仍在 .meta/。旧工程由 bridge 侧的 _migrate_txt_flat 迁上来。
        self.txt_dir = self.output_dir
        self.meta_dir = os.path.join(self.output_dir, ".meta")
        self.article_txt = os.path.join(self.txt_dir, f"{self.dataset_name}_{stamp}_articles.txt")
        self.meta_path = os.path.join(self.meta_dir, f"{self.dataset_name}_{stamp}.jsonl")

    @property
    def main_path(self) -> str:
        """对外汇报的产物路径：文章线 = 正文 txt，问答线 = 记录 jsonl。"""
        return self.article_txt if self.kind == "article" else self.path

    def append(self, record: dict[str, Any]) -> None:
        """入库一条记录：只落「一份训练用的文件」+（文章线）一份不进扫描的记录 sidecar。"""
        with self._lock:
            self._records.append(record)
            if record.get("article") is not None:
                # 文章线：训练产物 = 纯正文；完整记录进 .meta（不进扫描、不重复入库）
                with open(self.meta_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                body = article_to_txt(record)
                if body:
                    self._ensure_txt_dir()
                    with open(self.article_txt, "a", encoding="utf-8", newline="\n") as f:
                        f.write(body)
                        f.write("\n\n")
            else:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append_rejected(self, record: dict[str, Any]) -> None:
        if not self.save_rejected:
            return
        with self._lock:
            self._rejected.append(record)
            with open(self.reject_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def extend(self, records: list[dict[str, Any]]) -> None:
        for r in records:
            self.append(r)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records)

    def rejected_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._rejected)

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def rejected_count(self) -> int:
        with self._lock:
            return len(self._rejected)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._rejected.clear()
            self._stamp_paths(time.strftime("%Y%m%d_%H%M%S"))
            os.makedirs(self.meta_dir, exist_ok=True)

    def delete_indices(self, indices: list[int]) -> int:
        with self._lock:
            for idx in sorted(set(indices), reverse=True):
                if 0 <= idx < len(self._records):
                    del self._records[idx]
            self._rewrite()
            return len(self._records)

    def _rewrite(self) -> None:
        """删条后重写落盘文件：问答记录写主 jsonl，文章记录写 .meta + 正文 txt。"""
        arts = [r for r in self._records if r.get("article") is not None]
        qas = [r for r in self._records if r.get("article") is None]
        for path, rows in ((self.path, qas), (self.meta_path, arts)):
            if not rows and not os.path.exists(path):
                continue
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
        if arts:
            self._ensure_txt_dir()
            with open(self.article_txt, "w", encoding="utf-8", newline="\n") as f:
                for r in arts:
                    body = article_to_txt(r)
                    if body:
                        f.write(body + "\n\n")

    def export_jsonl(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as f:
            for r in self.all():
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return path

    def export_json(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.all(), f, ensure_ascii=False, indent=2)
        return path

    def export_csv(self, path: str) -> str:
        records = self.all()
        if not records:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write("")
            return path
        keys: list[str] = []
        for r in records:
            for k in r:
                if k not in keys and k != "review" and not isinstance(r[k], (dict, list)):
                    keys.append(k)
        if "score" not in keys:
            keys.append("score")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            for r in records:
                row = {k: r.get(k, "") for k in keys}
                review = r.get("review") or {}
                row["score"] = review.get("score", "")
                writer.writerow(row)
        return path

    def export_markdown(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as f:
            for r in self.all():
                if r.get("article") is not None:
                    f.write(f"## {r.get('title', '(无标题)')}\n\n")
                    f.write(
                        f"> 主题：{r.get('theme', '')} · 字数：{r.get('word_count', '')} · "
                        f"评分：{(r.get('review') or {}).get('score', '')}\n\n"
                    )
                    f.write(str(r.get("article", "")).strip() + "\n\n---\n\n")
                else:
                    f.write(f"## {r.get('question', '(无问题)')}\n\n")
                    f.write(str(r.get("answer", "")).strip() + "\n\n---\n\n")
        return path

    def export_train_txt(self, path: str | None = None) -> str:
        """导出训练用纯文本：文章→正文拼接；问答→ \\x01q\\x02a\\x03。"""
        records = self.all()
        if path is None:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            suffix = "articles" if any(r.get("article") is not None for r in records) else "sft"
            path = os.path.join(self.txt_dir, f"{self.dataset_name}_{stamp}_{suffix}.txt")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for r in records:
                if r.get("article") is not None:
                    body = article_to_txt(r)
                    if body:
                        f.write(body)
                        f.write("\n\n")
                else:
                    line = pair_to_txt(r)
                    if line:
                        f.write(line)
                        f.write("\n")
        return path

    def export_sharegpt(self, path: str) -> str:
        out = []
        for r in self.all():
            if r.get("article") is not None:
                out.append(
                    {
                        "conversations": [
                            {"from": "human", "value": f"请写一篇关于「{r.get('title', '')}」的完整文章。"},
                            {"from": "gpt", "value": r.get("article", "")},
                        ],
                        "theme": r.get("theme", ""),
                        "title": r.get("title", ""),
                    }
                )
            else:
                out.append(
                    {
                        "conversations": [
                            {"from": "human", "value": r.get("question", "")},
                            {"from": "gpt", "value": r.get("answer", "")},
                        ],
                        "topic": r.get("topic", ""),
                    }
                )
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        return path

    @staticmethod
    def import_jsonl(path: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if isinstance(obj, dict) and (obj.get("article") or obj.get("question") or obj.get("title")):
                    records.append(obj)
        return records

    @staticmethod
    def load_json(path: str) -> list[dict[str, Any]]:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [
                r
                for r in data
                if isinstance(r, dict) and (r.get("article") or r.get("question") or r.get("title"))
            ]
        return []


def count_words(text: str) -> int:
    t = text or ""
    cjk = len(re.findall(r"[一-鿿]", t))
    latin = len(re.findall(r"[A-Za-z0-9]+", t))
    return cjk + latin
