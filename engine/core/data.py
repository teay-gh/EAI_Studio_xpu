"""数据集扫描 / 格式审查 / 语料合并。

支持 .txt / .json / .jsonl：
  - 编码是否 UTF-8 可解
  - 字符量、行数、空行比例
  - 是否像对话格式（含 \\x01/\\x02/\\x03 或 role/content 字段）
  - 异常：二进制、超长行、控制字符、空文件

2026-10-17 扩展「能直接吃下的 JSON 形状」（用户：让 EAI 支持更多导入数据集的 json 格式）：
  · 问答键扩充：input/output、src/tgt、source/target、chosen、reply、summary、translation…
  · instruction + input 拼问（Alpaca）；只有 input/output 也认
  · 对话数组键扩充：messages/conversations/conversation/dialog/dialogue/turns/chat/history
  · 消息体是数组（OpenAI 多模态 content:[{type:text,text:…}]）自动拼文本
  · {"human": "…", "assistant": "…"} 形式的成对消息
  · 整份 .json 就是一串扁平消息（[{role:user},{role:assistant}]）→ 顺序配对
  · 一条记录一个纯文本字段（{"text": …} / {"content": …} / page_content …）→ 当预训练语料
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Iterable


CHAT_MARKS = ("\x01", "\x02", "\x03")
ROLE_KEYS = ("role", "from", "speaker")
CONTENT_KEYS = ("content", "text", "value", "message")

# 问句字段（按优先级）
Q_KEYS = ("question", "q", "prompt", "instruction", "query", "ask", "problem",
          "human", "user", "src", "source", "input")
# 答句字段（按优先级；text 放最后——它是纯文本记录的主键，别抢问答）
A_KEYS = ("answer", "a", "output", "response", "completion", "reply",
          "assistant", "gpt", "chosen", "target", "tgt", "translation", "summary")
# 一条记录 = 一段纯文本时的字段
TEXT_KEYS = ("text", "content", "body", "article", "document", "passage",
             "page_content", "raw_content", "paragraph", "completion")
# 对话数组字段
CONV_KEYS = ("messages", "conversations", "conversation", "dialog", "dialogue",
             "turns", "chat", "history")
ROLE_Q = ("user", "human", "q", "prompter", "customer", "instruction")
ROLE_A = ("assistant", "gpt", "bot", "model", "a", "ai", "chatgpt", "answerer")


@dataclass
class FileAudit:
    path: str
    name: str
    ext: str
    size_bytes: int = 0
    chars: int = 0
    lines: int = 0
    empty_lines: int = 0
    encoding_ok: bool = True
    looks_chat: bool = False
    looks_jsonl: bool = False
    looks_plain: bool = False
    looks_text_json: bool = False     # JSON/JSONL 但每条就是一段纯文本（可直接当预训练语料）
    sample: str = ""
    issues: list = field(default_factory=list)
    ok: bool = True

    def to_dict(self):
        return asdict(self)


def _sniff_text(raw: bytes) -> tuple[bool, str, str]:
    """返回 (utf8_ok, text, issue)。"""
    if b"\x00" in raw[:8000]:
        return False, "", "疑似二进制（含 NUL）"
    try:
        text = raw.decode("utf-8")
        return True, text, ""
    except UnicodeDecodeError:
        try:
            text = raw.decode("utf-8-sig")
            return True, text, ""
        except UnicodeDecodeError:
            try:
                text = raw.decode("gb18030")
                return True, text, "非 UTF-8，已按 GB18030 解码"
            except UnicodeDecodeError:
                return False, "", "无法解码为 UTF-8/GB18030"


# ---------------------------------------------------------------- 文本抽取小工具

def _text_of(v) -> str:
    """把任意 JSON 值压成字符串：str 原样、dict 取 text/content/value/answer、
    list（OpenAI 多模态 content）拼接其中各段文本。"""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("text", "content", "value", "answer", "message", "body"):
            t = _text_of(v.get(k))
            if t and t.strip():
                return t
        return ""
    if isinstance(v, list):
        parts = [_text_of(x) for x in v]
        return "\n".join([p for p in parts if p and p.strip()])
    return ""


def _pick(o: dict, keys) -> tuple[str, str]:
    """按 keys 顺序取第一个非空文本字段，返回 (key, text)。"""
    for k in keys:
        if k in o:
            t = _text_of(o.get(k))
            if t and t.strip():
                return k, t
    return "", ""


def _pairs_from_messages(msgs) -> list:
    """对话数组 → [(问, 答)]。支持 role/content、from/value、human/assistant 成对对象。"""
    out = []
    if not isinstance(msgs, list):
        return out
    cur = ""
    for m in msgs:
        if isinstance(m, str):
            continue
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or m.get("from") or m.get("speaker") or "").strip().lower()
        body = _text_of(
            m.get("content") if "content" in m else
            (m.get("value") if "value" in m else
             (m.get("text") if "text" in m else m.get("message")))
        )
        if not role:
            # {"human": "...", "assistant": "..."} / {"instruction": "...", "output": "..."}
            h = _text_of(m.get("human") or m.get("user") or m.get("instruction") or m.get("question"))
            a = _text_of(m.get("assistant") or m.get("gpt") or m.get("bot")
                         or m.get("output") or m.get("response") or m.get("answer"))
            if h and a:
                out.append((h, a))
            continue
        if role in ROLE_Q:
            cur = body
        elif role in ROLE_A and cur:
            out.append((cur, body))
            cur = ""
    return out


def _pairs_from_obj(o) -> list:
    """从一条 JSON 对象里抽出 (question, answer) 对。"""
    out = []
    if not isinstance(o, dict):
        return out

    # 1) 对话数组优先（messages / conversations / dialog …）
    for key in CONV_KEYS:
        got = _pairs_from_messages(o.get(key))
        if got:
            return got

    # 2) 单轮问答：question/answer、input/output、src/tgt、instruction+input/output …
    qk, q = _pick(o, Q_KEYS)
    if not q:
        return out
    if qk in ("instruction", "prompt", "question") and o.get("input"):
        extra = _text_of(o.get("input"))          # Alpaca：instruction + input 拼成问
        if extra.strip() and extra.strip() not in q:
            q = q.strip() + "\n" + extra.strip()
    ak, a = _pick(o, [k for k in A_KEYS if k != qk])
    if a and a.strip():
        out.append((q, a))
    # 多个参考答案
    for key in ("answers", "human_answers", "chatgpt_answers", "answer_list"):
        v = o.get(key)
        if isinstance(v, list):
            for x in v:
                t = _text_of(x)
                if t.strip():
                    out.append((q, t))
    return out


def _text_of_record(o) -> str:
    """一条记录 = 一段纯文本时返回它（问答记录返回空，避免被当纯文本）。"""
    if isinstance(o, str):
        return o
    if not isinstance(o, dict):
        return ""
    if _pairs_from_obj(o):
        return ""
    for k in TEXT_KEYS:
        if k in o:
            t = _text_of(o.get(k))
            if t.strip():
                return t
    return ""


def _walk_records(data, depth: int = 0):
    """把 .json 里的记录摊平成「一条一条」：list → 逐条，dict → 自身或其中第一个记录数组。"""
    if depth > 3:
        return
    if isinstance(data, list):
        # 扁平消息列表（[{role:user},{role:assistant}]）= 一整段对话
        if data and all(isinstance(x, dict) and (x.get("role") or x.get("from")) for x in data):
            yield data
            return
        for x in data:
            if isinstance(x, list):
                yield x
            else:
                yield x
        return
    if isinstance(data, dict):
        if _pairs_from_obj(data) or _text_of_record(data):
            yield data
            return
        for v in data.values():
            if isinstance(v, list):
                yield from _walk_records(v, depth + 1)


def extract_pairs(path: str, ext: str) -> list:
    """把文件转成 (question, answer) 列表；纯文本/无法解析则返回空。"""
    pairs = []
    try:
        if ext in (".jsonl", ".ndjson"):
            cur_q = None
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(o, list):
                        pairs.extend(_pairs_from_messages(o))
                        cur_q = None
                        continue
                    got = _pairs_from_obj(o)
                    if got:
                        pairs.extend(got)
                        cur_q = None
                        continue
                    # 一行一条消息（扁平 role/content 或 ShareGPT from/value）：跨行配对
                    if isinstance(o, dict):
                        role = str(o.get("role") or o.get("from") or o.get("speaker") or "").lower()
                        text = _text_of(o.get("content") if "content" in o else
                                        (o.get("value") if "value" in o else o.get("message")))
                        if role in ROLE_Q:
                            cur_q = text
                        elif role in ROLE_A and cur_q:
                            pairs.append((cur_q, text))
                            cur_q = None
            return pairs

        if ext == ".json":
            data = json.loads(open(path, "r", encoding="utf-8", errors="replace").read())
            for rec in _walk_records(data):
                if isinstance(rec, list):
                    pairs.extend(_pairs_from_messages(rec))
                else:
                    pairs.extend(_pairs_from_obj(rec))
            return pairs

        # .txt：若整文件已是 \x01q\x02a\x03 格式，原样切开重组，避免重复清洗
        text = open(path, "r", encoding="utf-8", errors="replace").read()
        stripped = text.lstrip()
        if stripped.startswith("{") and Q_MARK not in text:
            cur_q = None                          # 存错扩展名的 JSON/JSONL 文本：按行解析
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                got = _pairs_from_obj(o)
                if got:
                    pairs.extend(got)
                    cur_q = None
                    continue
                if isinstance(o, dict):
                    role = str(o.get("role") or o.get("from") or "").lower()
                    body = _text_of(o.get("content") if "content" in o else o.get("value"))
                    if role in ROLE_Q:
                        cur_q = body
                    elif role in ROLE_A and cur_q:
                        pairs.append((cur_q, body))
                        cur_q = None
            if pairs:
                return pairs
        if Q_MARK in text and A_MARK in text and E_MARK in text:
            for chunk in text.split(E_MARK):
                chunk = chunk.strip()
                if Q_MARK not in chunk:
                    continue
                body = chunk.split(Q_MARK, 1)[1]
                if A_MARK not in body:
                    continue
                q, a = body.split(A_MARK, 1)
                pairs.append((q, a))
            return pairs
    except Exception:
        return []
    return pairs


def extract_texts(path: str, ext: str, limit: int = 0) -> list:
    """把「一条记录一段纯文本」的文件摊平成文本列表（预训练用）。

    {"text": …} / {"content": …} / {"page_content": …} 这类下载来的语料 JSON、JSONL 全认；
    问答记录跳过（它们走 extract_pairs）。limit>0 时最多取这么多条。
    """
    texts = []
    try:
        if ext in (".jsonl", ".ndjson"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if limit and len(texts) >= limit:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = _text_of_record(o)
                    if t.strip():
                        texts.append(t)
            return texts
        if ext == ".json":
            data = json.loads(open(path, "r", encoding="utf-8", errors="replace").read())
            for rec in _walk_records(data):
                if limit and len(texts) >= limit:
                    break
                if isinstance(rec, list):
                    continue
                t = _text_of_record(rec)
                if t.strip():
                    texts.append(t)
            return texts
        if ext == ".txt":
            return [open(path, "r", encoding="utf-8", errors="replace").read()]
    except Exception:
        return []
    return texts


def audit_file(path: str, max_sample: int = 180, max_read: int = 2 * 1024 * 1024) -> FileAudit:
    """审查单个文件。只读前 max_read 字节做嗅探，避免把几 GB 的 txt 整个载入内存。"""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    rec = FileAudit(path=path, name=name, ext=ext)
    try:
        rec.size_bytes = os.path.getsize(path)
        with open(path, "rb") as f:
            raw = f.read(max_read)
            truncated = rec.size_bytes > len(raw)
    except OSError as e:
        rec.ok = False
        rec.issues.append(f"读取失败: {e}")
        return rec

    if rec.size_bytes == 0:
        rec.ok = False
        rec.issues.append("空文件")
        return rec

    ok, text, issue = _sniff_text(raw)
    rec.encoding_ok = ok
    if issue:
        rec.issues.append(issue)
    if not ok:
        rec.ok = False
        return rec

    if truncated:
        rec.issues.append(f"大文件（{rec.size_bytes/1e6:.0f} MB），仅扫描前 {len(raw)/1e6:.1f} MB")

    # 控制字符（排除常见的 \t \n \r 和对话分隔符）
    ctrl = sum(1 for c in text if ord(c) < 32 and c not in "\t\n\r" and c not in CHAT_MARKS)
    if ctrl > 0:
        rec.issues.append(f"含 {ctrl} 个控制字符")

    rec.chars = rec.size_bytes if truncated else len(text)
    rec.lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    rec.empty_lines = sum(1 for ln in text.splitlines() if not ln.strip())
    max_line = max((len(ln) for ln in text.splitlines()), default=0)
    if max_line > 20000:
        rec.issues.append(f"存在超长行（{max_line} 字符），可能是未换行的日志/压缩文本")

    has_marks = any(m in text for m in CHAT_MARKS)
    jsonl = False
    chat_json = False
    text_json = False
    if ext in (".jsonl", ".ndjson"):
        jsonl = True
    elif ext == ".json":
        try:
            data = json.loads(text)
            recs = list(_walk_records(data))[:6]
            if any(_pairs_from_obj(r) if isinstance(r, dict) else _pairs_from_messages(r)
                   for r in recs):
                chat_json = True
            elif recs and all((_text_of_record(r) if isinstance(r, dict) else "") for r in recs):
                text_json = True                  # 下载来的语料 JSON：{"text": …} 一条一段
            elif isinstance(data, list) and data and isinstance(data[0], dict):
                keys = set(data[0].keys())
                if keys & set(ROLE_KEYS) and keys & set(CONTENT_KEYS):
                    chat_json = True
                elif "conversations" in keys or "messages" in keys:
                    chat_json = True
        except json.JSONDecodeError:
            rec.issues.append("JSON 解析失败")
            rec.ok = False
    elif ext == ".txt":
        # 探测 jsonl 风格（含误存成 .txt 的问答 JSON）
        first = text.lstrip()[:400]
        if first.startswith("{"):
            if "\"role\"" in first or "\"messages\"" in first or "\"conversations\"" in first:
                jsonl = True
            elif "\"question\"" in first or "\"instruction\"" in first or "\"prompt\"" in first \
                    or "\"q\"" in first:
                jsonl = True

    rec.looks_jsonl = jsonl
    # 问答类 JSONL：HC3 的 question/human_answers，或 role/content 多轮
    head = text[:4000]
    qa_jsonl = jsonl and (
        ("\"question\"" in head and ("\"answer" in head or "\"human_answers\"" in head or "\"chatgpt" in head))
        or "\"role\"" in head
        or "\"messages\"" in head
        or "\"conversations\"" in head
        or "\"instruction\"" in head
        or "\"prompt\"" in head
    )
    if jsonl and not qa_jsonl:
        # 光看关键字会漏（q/a、Alpaca、扁平 ShareGPT…）：直接拿头几行真解析一次
        for _ln in [x for x in head.splitlines() if x.strip()][:5]:
            try:
                _o = json.loads(_ln)
            except Exception:                     # noqa: BLE001
                continue
            if isinstance(_o, dict) and (_pairs_from_obj(_o)
                                         or (set(_o) & set(ROLE_KEYS) and set(_o) & set(CONTENT_KEYS))):
                qa_jsonl = True
                break
    rec.looks_chat = has_marks or chat_json or qa_jsonl

    # JSONL 里一行为一段纯文本（{"text": …}）：能直接当预训练语料
    if (jsonl or ext in (".json", ".jsonl", ".ndjson")) and not rec.looks_chat and not text_json:
        _rows = [x for x in head.splitlines() if x.strip()][:5] if jsonl else []
        if _rows:
            _hit = 0
            for _ln in _rows:
                try:
                    _o = json.loads(_ln)
                except Exception:                 # noqa: BLE001
                    continue
                if _text_of_record(_o):
                    _hit += 1
            text_json = _hit >= max(1, len(_rows) - 1)
    rec.looks_text_json = bool(text_json)
    rec.looks_plain = not rec.looks_chat

    if rec.size_bytes < 30:
        rec.issues.append("内容过短（<30 字节）")
        rec.ok = False

    sample = re.sub(r"\s+", " ", text[: max_sample * 3])[:max_sample]
    rec.sample = sample
    return rec


def scan_folder(folder: str, exts: tuple = (".txt", ".json", ".jsonl", ".ndjson")) -> list[FileAudit]:
    """扫描目录下的数据文件。

    跳过两类不该入库的东西：
      · 以「.」开头的目录/文件（采集产物的 `.meta/` 记录 sidecar、各种内部清单）；
      · 文件名含 rejected 的隔离样本（「保存并隔离」留下的 *_rejected.jsonl）。
    这样一次采集只会给数据集页贡献一份能直接训练的文件，不会被导入两次。
    """
    out: list[FileAudit] = []
    if not folder or not os.path.isdir(folder):
        return out
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in sorted(files):
            if fn.startswith(".") or "rejected" in fn.lower():
                continue
            if os.path.splitext(fn)[1].lower() in exts:
                out.append(audit_file(os.path.join(root, fn)))
        # 只扫一层时可改：break
    return out


Q_MARK, A_MARK, E_MARK = "\x01", "\x02", "\x03"


def clean(s) -> str:
    if not s:
        return ""
    s = str(s).replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace(Q_MARK, "").replace(A_MARK, "").replace(E_MARK, "")
    return " ".join(s.split())


def wrap_pair(q, a) -> str | None:
    q, a = clean(q), clean(a)
    if not q or not a or len(a) < 2:
        return None
    return f"{Q_MARK}{q}{A_MARK}{a}{E_MARK}"


def file_digest(path: str) -> str:
    """整文件 SHA-256（用户 2026-10-19）：同一份数据既在工程内、又被「外部引用」
    登记时会被合并两遍，语料里每段文字出现 2 次；合并按内容指纹去重。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def merge_corpus(audits: Iterable[FileAudit], out_path: str, mode: str = "all") -> dict:
    """把选中文件转成对话语料（阶段二 SFT）。

    JSON/JSONL → 严格解析出问答，再写入 \\x01q\\x02a\\x03，
    绝不把 JSON 字段名原样灌进语料（否则模型会复读 question/human_answers）。
    纯文本文件会被跳过——它们属于阶段一预训练语料。
    """
    written = 0
    skipped = 0
    pairs_total = 0
    duplicates = 0
    seen_digest: set = set()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        for a in audits:
            if not a.ok and mode == "strict":
                skipped += 1
                continue
            dig = file_digest(a.path)                       # 内容相同的文件只收一次（用户 2026-10-19）
            if dig and dig in seen_digest:
                duplicates += 1
                continue
            if dig:
                seen_digest.add(dig)
            pairs = extract_pairs(a.path, a.ext)
            if not pairs:
                # 无法解析出问答的文件不要当原始 token 灌进去
                skipped += 1
                continue
            n_here = 0
            for q, ans in pairs:
                s = wrap_pair(q, ans)
                if not s:
                    continue
                f.write(s)
                n_here += 1
            if n_here == 0:
                skipped += 1
            else:
                written += 1
                pairs_total += n_here
    return {
        "written": written,
        "skipped": skipped,
        "duplicates": duplicates,
        "pairs": pairs_total,
        "out": out_path,
        "bytes": os.path.getsize(out_path),
    }


def merge_text_corpus(audits: Iterable[FileAudit], out_path: str, mode: str = "all") -> dict:
    """把纯文本文件拼接成预训练语料（阶段一）。

    只收 looks_plain 的文件：故事、文章、小说正文等。
    对话/JSONL 不要混进来，否则会把 question/human_answers 这类字段名当成自然语言。
    例外：JSON/JSONL 里「一条记录一段纯文本」（{"text": …}）→ 展开成正文再拼（用户 2026-10-17）。
    """
    written = 0
    skipped = 0
    duplicates = 0
    chars_total = 0
    seen_digest: set = set()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        for a in audits:
            if not a.ok and mode == "strict":
                skipped += 1
                continue
            if a.looks_chat:
                skipped += 1
                continue
            dig = file_digest(a.path)                       # 内容相同的文件只收一次
            if dig and dig in seen_digest:
                duplicates += 1
                continue
            if dig:
                seen_digest.add(dig)
            bodies = []
            if a.looks_jsonl or getattr(a, "looks_text_json", False) or a.ext in (".json", ".jsonl", ".ndjson"):
                bodies = [re.sub(r"\s+", " ", t).strip() for t in extract_texts(a.path, a.ext)]
                bodies = [b for b in bodies if len(b) >= 20]
                if not bodies:
                    skipped += 1
                    continue
            else:
                try:
                    text = open(a.path, "r", encoding="utf-8", errors="replace").read()
                except OSError:
                    skipped += 1
                    continue
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                if Q_MARK in text or A_MARK in text or E_MARK in text:
                    # 混入对话标记的文件不适合当纯文本
                    skipped += 1
                    continue
                body = text.strip()
                if len(body) < 50:
                    skipped += 1
                    continue
                bodies = [body]
            for body in bodies:
                if re.match(r"^[\s{}<>\W]*$", body[:40]):
                    continue
                f.write(body)
                f.write("\n\n")
                chars_total += len(body)
            written += 1
    return {
        "written": written,
        "skipped": skipped,
        "duplicates": duplicates,
        "chars": chars_total,
        "out": out_path,
        "bytes": os.path.getsize(out_path),
    }


def format_report(audits: list[FileAudit]) -> str:
    if not audits:
        return "未扫描到数据文件。"
    lines = []
    ok_n = sum(1 for a in audits if a.ok)
    lines.append(f"共 {len(audits)} 个文件 · 可用 {ok_n} · 异常 {len(audits) - ok_n}")
    lines.append(f"总字符 {sum(a.chars for a in audits):,} · 总大小 {sum(a.size_bytes for a in audits)/1e6:.2f} MB")
    lines.append("-" * 56)
    for a in audits:
        flag = "OK" if a.ok else "!!"
        kind = "对话" if a.looks_chat else ("JSONL" if a.looks_jsonl else "纯文本")
        lines.append(f"[{flag}] {a.name}  {kind}  {a.chars:,} 字符  {a.lines:,} 行")
        for iss in a.issues:
            lines.append(f"      · {iss}")
    return "\n".join(lines)


def check_pretrain_format(a: FileAudit) -> tuple[bool, str]:
    """阶段一预训练：只收纯文本。返回 (是否合格, 说明)。"""
    if not a.ok:
        return False, "文件异常：" + ("；".join(a.issues) if a.issues else "无法读取")
    if a.looks_chat:
        return False, "检测到对话/问答格式，应放到「微调数据集」"
    if a.looks_jsonl or getattr(a, "looks_text_json", False):
        if getattr(a, "looks_text_json", False):
            return True, "JSON 文本字段，合并时按纯文本展开 OK"
        return False, "JSONL 通常不是纯文本语料，请确认或放到微调页"
    if a.size_bytes < 50:
        return False, "内容过短（<50 字节）"
    if any(m in a.sample for m in CHAT_MARKS):
        return False, "含有对话分隔符 \\x01/\\x02/\\x03"
    return True, "纯文本 OK"


def check_sft_format(a: FileAudit) -> tuple[bool, str]:
    """阶段二微调：收对话/问答。返回 (是否合格, 说明)。"""
    if not a.ok:
        return False, "文件异常：" + ("；".join(a.issues) if a.issues else "无法读取")
    pairs = extract_pairs(a.path, a.ext)
    if not pairs:
        if getattr(a, "looks_text_json", False):
            return False, "这是纯文本 JSON（一条一段），应放到「预训练数据集」"
        if a.looks_plain and not a.looks_jsonl:
            return False, "未解析出问答对，应放到「预训练数据集」"
        return False, "无法解析出问答（需要 question/answer、messages/conversations、input/output 等）"
    if len(pairs[0]) < 2 or not str(pairs[0][0] or "").strip():
        return False, "问答字段不完整"
    return True, f"问答 OK · 抽样 {len(pairs)} 条"


def format_check_report(audits: list[FileAudit], kind: str) -> str:
    """kind: 'pretrain' | 'sft'。输出格式检查报告。"""
    checker = check_pretrain_format if kind == "pretrain" else check_sft_format
    title = "预训练（纯文本）" if kind == "pretrain" else "微调（对话问答）"
    if not audits:
        return f"{title}格式检查：尚未选择文件。\n\n" + (
            "预训练语料应为纯文本（故事/文章），不要混入 JSON 问答。\n"
            if kind == "pretrain" else
            "微调语料应为问答对：JSONL 的 question/answers，或 \\x01问\\x02答\\x03。\n"
            "此项可选——没有微调数据时仍可只做预训练。\n"
        )
    lines = [f"{title}格式检查", "─" * 48]
    ok_n = 0
    for a in audits:
        ok, msg = checker(a)
        mark = "✓" if ok else "✗"
        if ok:
            ok_n += 1
        lines.append(f"{mark} {a.name}")
        lines.append(f"    {msg}")
        if a.issues:
            for iss in a.issues[:3]:
                lines.append(f"    · {iss}")
    lines.append("─" * 48)
    lines.append(f"通过 {ok_n} / {len(audits)}")
    return "\n".join(lines)
