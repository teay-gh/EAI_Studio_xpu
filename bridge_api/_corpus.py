"""语料合并原语：**训练启动**与**数据集页合并任务**共用一份实现。

语义与 Tk 版 DatasetPage._merge 完全一致（也是 dataset.py 原来的内联实现）：
  - pretrain → core.data.merge_text_corpus（纯文本正文，段间 \\n\\n）
        → <工程>/corpora/pretrain_corpus.txt（在 data/ 之外，扫描找不到它）
  - sft      → core.data.merge_corpus（\\x01问\\x02答\\x03，每对一行）
        → <工程>/corpora/sft_corpus.txt
  - 扫描目录 = <工程>/data/.scan_dirs.json 的清单（文件不存在 = data/article|response|corpora）；
    与数据集页共用同一份清单，避免「页面里勾的文件」和「训练时合并的文件」不是同一批。
  - 安全护栏（core 的已知陷阱：合并函数先以 "w" 截断输出再读源，把输出当源会读到空）：
      (1) 输出文件自身绝不当源；
      (2) 先写到临时文件 <out>.merging；
      (3) 结果为空（written == 0）默认**不覆盖**已有语料，除非 allow_empty=True。
  - 不改 config.json、不删源文件。

对外只暴露：scan_dirs / corpus_path / scan / merge。
emit(kind, payload) 可选：把日志（"log"）与进度（"progress"）抛给调用方（数据集页拿它写任务面板，
训练启动拿它写训练日志）。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Optional

# 默认扫描目录 / 清单文件 / 语料文件名（口径必须与 bridge_api/dataset.py、core.project 一致）
SUBDIRS = ("article", "response", "corpora")
SCAN_DIRS_FILE = ".scan_dirs.json"
CORPUS_NAMES = {"pretrain": "pretrain_corpus.txt", "sft": "sft_corpus.txt"}

_data_mod: Any = None


# ---------------------------------------------------------------- 依赖加载

def _engine_dir() -> str:
    import _host                                     # 同目录，借用它的 ENGINE_DIR
    return getattr(_host, "ENGINE_DIR", None) or _host.IDE_DIR


def _data():
    """按需 import core.data（导入期间关掉字节码写入，桥不该在 ENGINE_DIR 里造 __pycache__）。"""
    global _data_mod
    if _data_mod is not None:
        return _data_mod
    engine = _engine_dir()
    if engine not in sys.path:
        sys.path.insert(0, engine)
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        import core.data as _d  # type: ignore
    finally:
        sys.dont_write_bytecode = prev
    _data_mod = _d
    return _data_mod


# ---------------------------------------------------------------- 路径与清单

def default_dirs(proj: str) -> list:
    """工程内的三个默认数据目录。"""
    return [os.path.join(proj, "data", k) for k in SUBDIRS]


def dirs_file(proj: str) -> str:
    return os.path.join(proj, "data", SCAN_DIRS_FILE)


def scan_dirs(proj: str) -> list:
    """当前扫描目录清单：默认三目录 / 用户增删后的清单（空清单也尊重，不回退）。"""
    try:
        with open(dirs_file(proj), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return default_dirs(proj)
    except Exception:                                # noqa: BLE001  坏文件当没配过
        return default_dirs(proj)
    dirs = raw.get("dirs") if isinstance(raw, dict) else raw
    if not isinstance(dirs, list):
        return default_dirs(proj)
    out = []
    for d in dirs:
        s = str(d or "").strip()
        if s:
            out.append(s)
    return out


CORPUS_DIR = "corpora"          # 合并产物目录（<工程>/corpora），故意不在 data/ 下：
                                # 扫描目录只有 data/article|response|corpora，合并时找不到它，
                                # 否则合并结果会被当成又一份语料再合一遍（用户 2026-10-19）。


def _migrate_legacy(proj: str, name: str, new_path: str) -> None:
    """老布局 <工程>/data/corpora/<name> 就地挪到新目录，避免它被扫描当成源文件。"""
    old = os.path.join(proj, "data", "corpora", name)
    if os.path.isfile(old) and not os.path.isfile(new_path):
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        try:
            os.replace(old, new_path)
        except OSError:
            import shutil
            shutil.copy2(old, new_path)
            try:
                os.remove(old)
            except OSError:
                pass


def corpus_path(proj: str, kind: str) -> str:
    """合并输出路径（与 core.project.PRETRAIN_CORPUS / SFT_CORPUS 固定名一致）。"""
    name = CORPUS_NAMES["sft" if kind == "sft" else "pretrain"]
    new_path = os.path.join(proj, CORPUS_DIR, name)
    _migrate_legacy(proj, name, new_path)
    return new_path


METHODS = ("pretrain", "continue", "sft", "lora")   # 四种训练方式 = 数据集页四个页签（2026-10-15）


def norm_kind(kind: str) -> str:
    """归到两大类（决定合并产物与字节格式）：pretrain=纯文本语料 / sft=问答语料（lora 同 sft）。"""
    return "sft" if str(kind or "").strip().lower() in ("sft", "finetune", "微调", "lora") else "pretrain"


def sel_key(kind: str) -> str:
    """勾选清单键：四种训练方式**各存各的**（预训练/继续预训练/指令微调/LoRA 微调各选各的数据集）。
    非方法键（旧调用方只传 sft/finetune/微调）归组键，兼容旧 .corpus_sel.json 只有 pretrain/sft 两键。"""
    k = str(kind or "").strip().lower()
    return k if k in METHODS else norm_kind(k)


# ---------------------------------------------------------------- 勾选清单

SEL_FILE = ".corpus_sel.json"          # <工程>/data/ 下；{"pretrain": [路径...], "sft": [...]}


def sel_file(proj: str) -> str:
    return os.path.join(proj, "data", SEL_FILE)


def load_selection(proj: str, kind: str):
    """数据集页勾选参与合并的文件清单。None = 该类型从没设置过（调用方按「全部合格文件」处理）。"""
    try:
        with open(sel_file(proj), encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:                                # noqa: BLE001  缺失/损坏都当没设置
        return None
    if not isinstance(raw, dict):
        return None
    item = raw.get(sel_key(kind))
    if item is None and sel_key(kind) != norm_kind(kind):
        item = raw.get(norm_kind(kind))          # 旧两键文件：continue/lora 没勾过就看组键那份
    if not isinstance(item, list):
        return None
    return [str(p) for p in item if str(p or "").strip()]


def save_selection(proj: str, kind: str, paths) -> str:
    """写勾选清单（合并写，不碰另一类的勾选）。"""
    path = sel_file(proj)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raw = {}
    except Exception:                                # noqa: BLE001
        raw = {}
    raw[sel_key(kind)] = [str(p) for p in (paths or []) if str(p or "").strip()]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- 扫描

REFS_FILE = ".scan_files.json"


def refs_file(proj: str) -> str:
    """外部引用文件清单：<工程>/data/.scan_files.json（用户 2026-10-16：
    添加外部文件=直接引用路径、不复制进工程；合并产物才落工程文件夹）。"""
    return os.path.join(proj, "data", REFS_FILE)


def scan_refs(proj: str) -> list:
    try:
        with open(refs_file(proj), encoding="utf-8") as fh:
            raw = json.load(fh)
        files = raw.get("files") if isinstance(raw, dict) else raw
        if not isinstance(files, list):
            return []
        out: list = []
        for item in files:
            if isinstance(item, str) and item.strip():
                ap = os.path.abspath(os.path.expandvars(item.strip()))
                if ap not in out:
                    out.append(ap)
        return out
    except Exception:                              # noqa: BLE001  无清单/坏档=没引用
        return []


def save_refs(proj: str, files: list) -> None:
    d = os.path.join(proj, "data")
    os.makedirs(d, exist_ok=True)
    with open(refs_file(proj), "w", encoding="utf-8") as fh:
        json.dump({"files": list(files)}, fh, ensure_ascii=False, indent=1)


def add_ref(proj: str, path: str) -> bool:
    ap = os.path.abspath(path)
    refs = scan_refs(proj)
    if ap in refs:
        return False
    refs.append(ap)
    save_refs(proj, refs)
    return True


def del_ref(proj: str, path: str) -> bool:
    ap = os.path.abspath(path)
    refs = scan_refs(proj)
    if ap not in refs:
        return False
    save_refs(proj, [p for p in refs if p != ap])
    return True


def scan(proj: str) -> tuple:
    """真实扫描：返回 (core.data 模块, 工程根, 目录清单, FileAudit 列表)。

    = 扫描目录清单 + 外部引用文件（引用只读原位置，不复制进工程）。"""
    D = _data()
    roots = scan_dirs(proj)
    audits: list = []
    seen: set = set()

    def _key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    # 合并产物自家目录（<工程>/corpora）永不参与扫描（用户 2026-10-19）：
    # 即使有人把它误加进扫描目录，也不会把合并结果再合一遍。
    own_dir = _key(os.path.join(proj, CORPUS_DIR))

    def _own(path: str) -> bool:
        return _key(path).startswith(own_dir + os.sep)

    for root in roots:
        if os.path.isdir(root) and not _own(root):
            for a in D.scan_folder(root):
                k = _key(getattr(a, "path", ""))
                if _own(k):
                    continue
                if k not in seen:
                    seen.add(k)
                    audits.append(a)
    for ref in scan_refs(proj):
        k = _key(ref)
        if k in seen or _own(k):
            continue
        if os.path.isfile(ref):
            seen.add(k)
            try:
                audits.append(D.audit_file(ref))
            except Exception:                       # noqa: BLE001  引用读不了就跳过（照实不编造）
                pass
    return D, proj, roots, audits


# ---------------------------------------------------------------- 真合并

def merge(kind: str, proj: str, sources=None, allow_empty: bool = False,
          emit: Optional[Callable[[str, dict], None]] = None,
          audits: Optional[list] = None,
          on_selected: Optional[Callable[[list], None]] = None) -> dict:
    """扫描 → 选中 → core 合并到临时文件 → 原子替换；返回**真实**统计（绝无编造）。"""
    raw_kind = str(kind or "")                    # 选源按「训练方式」键（continue/lora 各自的勾选）
    kind = norm_kind(kind)
    logs: list = []

    def say(text: str) -> None:
        logs.append(text)
        if emit:
            try:
                emit("log", {"text": text})
            except Exception:                        # noqa: BLE001
                pass

    out_path = corpus_path(proj, kind)
    try:
        D, proj, roots, audits_scanned = scan(proj)
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "kind": kind, "project": proj, "error": "core.data 不可用：%s" % exc,
                "logs": logs}

    if audits is None:
        audits = audits_scanned
        say("扫描工程数据目录：" + " · ".join(roots or []))
        say("扫描到 %d 个文件。" % len(audits))
    audits = list(audits)

    out_abs = os.path.abspath(out_path)
    if sources:
        want = set()
        for s in sources:
            s = str(s or "").strip()
            if s:
                want.add(os.path.abspath(s))
        selected = [a for a in audits if os.path.abspath(a.path) in want]
    else:
        persisted = load_selection(proj, raw_kind)
        if persisted is None:
            selected = list(audits)
        else:
            # 数据集页勾过就按勾选来（训练启动时走的正是这条：勾谁合谁）
            want = set(os.path.normcase(os.path.abspath(p)) for p in persisted)
            selected = [a for a in audits
                        if os.path.normcase(os.path.abspath(getattr(a, "path", ""))) in want]
            say("按数据集页勾选过滤：%d / %d 个文件参与合并。" % (len(selected), len(audits)))
    # 护栏(1)：输出文件自身绝不当源
    selected = [a for a in selected if os.path.abspath(a.path) != out_abs]

    say("参与合并 %d 个文件（已排除合并目标自身）。" % len(selected))
    if on_selected:
        try:
            on_selected(selected)
        except Exception:                            # noqa: BLE001
            pass

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    prev_exists = os.path.isfile(out_path)
    prev_bytes = os.path.getsize(out_path) if prev_exists else 0

    merge_fn = D.merge_text_corpus if kind == "pretrain" else D.merge_corpus
    tmp = out_path + ".merging"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass

    result = merge_fn(selected, tmp, mode="all")
    written = int(result.get("written") or 0)
    skipped = int(result.get("skipped") or 0)
    dups = int(result.get("duplicates") or 0)          # 内容完全相同的文件（用户 2026-10-19）
    if dups:
        say("按内容去重：跳过 %d 个重复文件（同一份数据既在工程内又被外部引用）。" % dups)
    tmp_bytes = int(result.get("bytes") or 0)
    if emit:
        try:
            emit("progress", {"written": written, "skipped": skipped})
        except Exception:                            # noqa: BLE001
            pass

    # 护栏(2)：结果为空 → 默认保留原语料，不覆盖
    replaced = False
    if allow_empty or written > 0:
        os.replace(tmp, out_path)
        replaced = True
        say("已原子替换输出：%s（%d 字节）。" % (out_path, tmp_bytes))
    else:
        try:
            os.remove(tmp)
        except OSError:
            pass
        say("合并结果为空，未覆盖输出（无可用源文件）。")

    out_bytes = os.path.getsize(out_path) if os.path.isfile(out_path) else 0
    if not replaced:
        note = ("合并结果为空（无可用源文件），保留原语料 %d 字节，未覆盖。" % prev_bytes) if prev_exists \
            else "合并结果为空（无可用源文件），未写出语料。"
    elif written == 0:
        note = "合并结果为空，已按 allow_empty 写出空语料。"
    else:
        note = "已写出 %d 字节。" % out_bytes
    say(note)

    return {
        "ok": True,
        "kind": kind,
        "project": proj,
        "roots": list(roots or []),
        "out": out_path,
        "out_rel": os.path.relpath(out_path, proj).replace(os.sep, "/"),
        "replaced": replaced,
        "written": written,                       # 写入的文件数（core 口径）
        "skipped": skipped,                       # 跳过的文件数（core 口径）
        "duplicates": dups,                       # 内容重复被跳过的文件数
        "scanned": len(audits),
        "selected": len(selected),
        "pairs": int(result.get("pairs") or 0),        # sft：问答对数量
        "merge_chars": int(result.get("chars") or 0),  # pretrain：core 统计的正文字符量
        "tmp_bytes": tmp_bytes,
        "out_bytes": out_bytes,
        "prev_bytes": prev_bytes,
        "note": note,
        "logs": logs,
    }
