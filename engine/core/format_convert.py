"""数据集格式转换引擎（2026-10-17，用户：网上下载的 json 数据集格式很多，用 AI 转成 EAI 能读的）。

策略：**本地优先，AI 兜底**。
  1. 先用 `core.data` 的多格式解析器直读（覆盖 Alpaca / ShareGPT / OpenAI messages /
     input-output / src-tgt / preference chosen / 扁平消息串 / {"text": …} 语料 …）；
     命中率够（≥90% 记录都解析出来）就直接落盘，一个 token 都不花。
  2. 本地读不出来的（奇怪嵌套、自定义字段、混杂结构）才切成小批交给模型，
     让它输出 EAI 的两种规范形状之一：
        · 问答  → JSONL 每行 {"question": "...", "answer": "..."}
        · 纯文本 → 每行一段正文（落成 .txt）
     每批做校验（能不能解析、字段是否齐），不合格带着错误再要一次；仍不合格就记日志跳过。
  3. 产物落地后再用 data.audit_file / extract_pairs 复核一遍，报「解析出 N 条」。

产物位置：<工程>/data/corpora/<名>.jsonl（问答）/ <名>.txt（纯文本），
和「数据集」页默认扫描目录同一个地方，转完可直接在数据集页勾选训练。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable

from . import data as D


class ConvertError(RuntimeError):
    pass


def _emit(cb: Callable | None, kind: str, **payload) -> None:
    if cb:
        try:
            cb({"kind": kind, "ts": time.time(), **payload})
        except Exception:            # noqa: BLE001
            pass


def _short(v: Any, limit: int = 700) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def load_records(path: str, max_records: int = 0) -> list:
    """把任意 json / jsonl 读成「一条一条记录」（不改结构，留给 AI 看原始形状）。"""
    ext = os.path.splitext(path)[1].lower()
    recs: list = []
    if ext in (".jsonl", ".ndjson", ".txt"):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    recs.append(line)             # 裸文本行
                if max_records and len(recs) >= max_records:
                    break
        return recs
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    for rec in D._walk_records(data):
        recs.append(rec)
        if max_records and len(recs) >= max_records:
            break
    return recs


SYS_PROMPT = (
    "你是数据集格式转换器。EAI 训练器只吃两种形状：\n"
    "A) 问答（指令微调）：每行一个 JSON，字段 question / answer（都是字符串）。\n"
    "B) 纯文本（预训练）：一行一段正文，直接是自然语言，不含 JSON、不含字段名。\n"
    "现在给你若干条原始记录（来自用户下载的数据集）。请原样搬运内容，"
    "不要总结、不要改写、不要编造、不要加解释和前后缀。\n"
    "多轮对话请拆成多条一问一答；system 提示可忽略；噪声/空内容直接丢弃。\n"
    "只输出结果行本身（每行一个 JSON 或一段正文），不要输出 ``` 代码块围栏。"
)


def _chunks(records: list, size: int) -> list:
    return [records[i:i + size] for i in range(0, len(records), size)]


class FormatConverter:
    """一次转换任务。线程内跑，事件走 on_event（bridge 用）。"""

    def __init__(
        self,
        src: str,
        target: str = "auto",
        out_name: str = "",
        out_dir: str = "",
        client: Any = None,
        on_event: Callable | None = None,
        stop: threading.Event | None = None,
        max_records: int = 200,
        batch: int = 8,
    ) -> None:
        self.src = os.path.abspath(src or "")
        self.target = (target or "auto").lower()
        self.out_name = (out_name or "").strip()
        self.out_dir = out_dir or ""
        self.client = client
        self.on_event = on_event
        self.stop = stop or threading.Event()
        self.max_records = max(1, int(max_records or 200))
        self.batch = min(20, max(1, int(batch or 8)))
        self.pairs: list = []
        self.texts: list = []
        self.failed = 0
        self.mode = "local"
        self.out_path = ""

    # ------------------------------------------------------------ 探测
    def probe(self) -> dict:
        if not self.src or not os.path.isfile(self.src):
            raise ConvertError("源文件不存在：%s" % self.src)
        ext = os.path.splitext(self.src)[1].lower()
        if ext not in (".json", ".jsonl", ".ndjson", ".txt"):
            raise ConvertError("只接受 .json / .jsonl / .ndjson / .txt，当前是 %s" % (ext or "无扩展名"))
        audit = D.audit_file(self.src)
        recs = load_records(self.src, self.max_records)
        pairs = D.extract_pairs(self.src, ext)
        texts = D.extract_texts(self.src, ext, limit=self.max_records)
        local_kind = "none"
        if pairs and len(pairs) >= max(1, int(len(recs) * 0.9)):
            local_kind = "sft"
        elif texts and len(texts) >= max(1, int(len(recs) * 0.9)):
            local_kind = "pretrain"
        return {
            "path": self.src,
            "ext": ext,
            "size_bytes": audit.size_bytes,
            "records": len(recs),
            "pairs": len(pairs),
            "texts": len(texts),
            "looks_chat": audit.looks_chat,
            "looks_text_json": audit.looks_text_json,
            "issues": list(audit.issues),
            "local_kind": local_kind,          # sft / pretrain / none（none 才需要 AI）
            "samples": [_short(r, 320) for r in recs[:3]],
            "first_pairs": [{"q": q[:200], "a": a[:200]} for q, a in pairs[:3]],
            "first_texts": [t[:200] for t in texts[:2]],
        }

    # ------------------------------------------------------------ 落盘
    def _write(self) -> None:
        os.makedirs(self.out_dir or ".", exist_ok=True)
        stem = self.out_name or os.path.splitext(os.path.basename(self.src))[0]
        stem = re.sub(r'[\\/:*?"<>|\s]+', "_", stem)[:70].strip("._") or "converted"
        if self.pairs:
            self.out_path = os.path.join(self.out_dir, stem + ".jsonl")
            with open(self.out_path, "w", encoding="utf-8", newline="\n") as f:
                for q, a in self.pairs:
                    q2, a2 = D.clean(q), D.clean(a)
                    if not q2 or not a2:
                        continue
                    f.write(json.dumps({"question": q2, "answer": a2}, ensure_ascii=False) + "\n")
        else:
            self.out_path = os.path.join(self.out_dir, stem + ".txt")
            with open(self.out_path, "w", encoding="utf-8", newline="\n") as f:
                for t in self.texts:
                    body = (t or "").strip()
                    if len(body) < 20:
                        continue
                    f.write(body + "\n\n")

    def _verify(self) -> dict:
        a = D.audit_file(self.out_path)
        ext = os.path.splitext(self.out_path)[1].lower()
        pairs = D.extract_pairs(self.out_path, ext) if ext == ".jsonl" else []
        texts = D.extract_texts(self.out_path, ext, limit=5) if ext == ".txt" else []
        ok = (len(pairs) > 0) if self.pairs else bool(texts)
        return {
            "out": self.out_path,
            "bytes": a.size_bytes,
            "pairs": len(pairs),
            "texts": len(texts),
            "chars": a.chars,
            "ok": ok,
            "issues": list(a.issues),
        }

    # ------------------------------------------------------------ AI 兜底
    def _ask(self, records: list, target: str) -> list:
        """一批记录 → [(q,a)] 或 [text]；两次尝试。"""
        body = "\n".join(_short(r, 700) for r in records)
        want = ("A 问答：" if target == "sft" else "B 纯文本：")
        msgs = [
            {"role": "system", "content": SYS_PROMPT + "\n本批目标：" + want},
            {"role": "user", "content": "原始记录（每行一条）：\n" + body},
        ]
        last_err = ""
        for attempt in (1, 2):
            if self.stop.is_set():
                return []
            try:
                txt = self.client.chat(msgs, temperature=0.1, max_tokens=2400)
            except Exception as exc:                    # noqa: BLE001
                last_err = "模型调用失败：%s" % exc
                _emit(self.on_event, "log", text=last_err, level="err")
                continue
            got = self._parse(txt, target)
            if got:
                return got
            last_err = "模型输出不合规（第 %d 次）" % attempt
            _emit(self.on_event, "log", text=last_err + "，退回重试", level="warn")
            msgs = [
                {"role": "system", "content": SYS_PROMPT + "\n本批目标：" + want},
                {"role": "user", "content": "原始记录（每行一条）：\n" + body},
                {"role": "assistant", "content": (txt or "")[:1500]},
                {"role": "user", "content": "上面的输出不合规。请严格按要求重来："
                                            + ("每行一个 JSON，只有 question/answer 两个字段。"
                                               if target == "sft" else "每行一段正文，不要 JSON、不要字段名。")
                                            + " 不要任何解释、不要代码块围栏。"},
            ]
        if last_err:
            _emit(self.on_event, "log", text=last_err, level="err")
        return []

    @staticmethod
    def _parse(txt: str, target: str) -> list:
        if not txt:
            return []
        body = txt.strip()
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body).strip()
        if target == "sft":
            out = []
            for line in body.splitlines():
                line = line.strip().rstrip(",")
                if not line or not line.startswith("{"):
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                q = D._text_of(o.get("question") or o.get("q") or o.get("instruction")
                               or o.get("prompt") or o.get("input"))
                a = D._text_of(o.get("answer") or o.get("a") or o.get("output")
                               or o.get("response") or o.get("completion"))
                if q.strip() and a.strip():
                    out.append((q, a))
            return out
        # 纯文本：整段按空行切
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        if len(paras) <= 1:
            paras = [p.strip() for p in body.splitlines() if p.strip()]
        return [p for p in paras if len(p) >= 20 and not p.startswith("{")]

    # ------------------------------------------------------------ 主流程
    def run(self) -> dict:
        t0 = time.time()
        info = self.probe()
        _emit(self.on_event, "log",
              text="源文件 %s · %.1f KB · %d 条记录 · 本地可解析：问答 %d / 文本 %d"
                   % (os.path.basename(self.src), info["size_bytes"] / 1024.0,
                      info["records"], info["pairs"], info["texts"]))
        ext = info["ext"]

        want = self.target
        if want == "auto":
            want = info["local_kind"] if info["local_kind"] != "none" else "sft"

        # 1) 本地直读
        done = 0
        if info["local_kind"] != "none" and (self.target == "auto" or self.target == info["local_kind"]):
            if want == "sft":
                self.pairs = D.extract_pairs(self.src, ext)[: self.max_records]
            else:
                self.texts = D.extract_texts(self.src, ext, limit=self.max_records)
            self.mode = "local"
            done = len(self.pairs) or len(self.texts)
            _emit(self.on_event, "log",
                  text="本地解析命中：%s %d 条，无需调用模型" % ("问答" if self.pairs else "纯文本", done),
                  level="ok")
        else:
            # 2) AI 兜底
            if not self.client:
                raise ConvertError("这份文件本地解析不出来，需要模型转换，但模型未配置（去「语料蒸馏」页填 base_url / model）")
            recs = load_records(self.src, self.max_records)
            self.mode = "ai"
            batches = _chunks(recs, self.batch)
            _emit(self.on_event, "log",
                  text="交给模型转换：%d 条 → %d 批（每批 %d 条），目标 %s"
                       % (len(recs), len(batches), self.batch, "问答" if want == "sft" else "纯文本"))
            for i, ch in enumerate(batches, 1):
                if self.stop.is_set():
                    _emit(self.on_event, "log", text="已停止（完成 %d/%d 批）" % (i - 1, len(batches)), level="warn")
                    break
                got = self._ask(ch, want)
                if not got:
                    self.failed += len(ch)
                    _emit(self.on_event, "log", text="第 %d 批没有可用输出，跳过 %d 条" % (i, len(ch)), level="warn")
                else:
                    if want == "sft":
                        self.pairs.extend(got)
                    else:
                        self.texts.extend(got)
                    done += len(got)
                _emit(self.on_event, "progress", done=done, total=len(recs),
                      batch=i, batches=len(batches))

        if not self.pairs and not self.texts:
            raise ConvertError("没有转换出任何内容（本地解析失败、模型也没给出合规输出）")

        self._write()
        rep = self._verify()
        _emit(self.on_event, "log",
              text="写出 %s · %d 条 · %.1f KB · 复核 %s"
                   % (os.path.basename(self.out_path),
                      rep["pairs"] or rep["texts"], rep["bytes"] / 1024.0,
                      "通过" if rep["ok"] else "未通过（文件仍保留，可在数据集页人工确认）"),
              level="ok" if rep["ok"] else "warn")
        rep.update({
            "mode": self.mode,
            "target": want,
            "failed": self.failed,
            "records": info["records"],
            "elapsed_ms": int((time.time() - t0) * 1000),
            "src": self.src,
        })
        return rep
