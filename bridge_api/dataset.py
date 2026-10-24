"""bridge_api/dataset.py —— 「数据集」页桥接口（扫描 / 格式检查只读；合并已接真引擎）。

对应 Tk 版页面：ENGINE_DIR/pages/dataset.py（DatasetPage）。

真实数据来源：
  - 配置：<应用目录>\\engine\\config.json（只读）
  - 当前工程：config.app.recent_projects[0]（必须存在且是目录），否则
    <应用目录>\\engine\\projects\\default（与 app.py `_open_recent_project` 与
    bridge.py 的 resolve_project 同一套规则）
  - 扫描范围（固定，与 Tk 版 _project_roots 一致）：
        <工程>/data/article · <工程>/data/response · <工程>/data/corpora

接口：
  GET  /api/dataset/scan?kind=pretrain|sft[&project=<abs>]        真实扫描结果（只读）
  GET  /api/dataset/check?kind=pretrain|sft[&project=<abs>]       真实格式检查报告（只读）
  POST /api/dataset/merge                                         真合并（后台线程任务）
       body: {"kind":"pretrain"|"sft", "sources":["<abs>", ...可选], "allow_empty":false}
  GET  /api/dataset/merge/status?kind=...&since=N                 后台合并任务增量进度 + 结果

合并语义（照抄 Tk 版 DatasetPage._merge，复用 core.data，与 Tk 同字节格式）：
  - pretrain → core.data.merge_text_corpus（纯文本正文，段间 \\n\\n），输出
        <工程>/corpora/pretrain_corpus.txt（合并产物，故意不在 data/ 下）
  - sft      → core.data.merge_corpus（\\x01问\\x02答\\x03，每对一行），输出
        <工程>/corpora/sft_corpus.txt
  - 只写 <工程>/corpora/ 下的固定文件名；**不改 config.json**
    （Tk 版会回写 train.pretrain_corpus/sft_corpus；本桥按铁律不动 config.json），
    源文件保留不删。
  - 安全护栏（core 的一个已知陷阱）：core 的合并函数会先以 "w" 截断输出再逐文件读取，
    若把「输出文件自身」当成源，读到的就是空。本桥因此
      (1) 先把合并结果写到临时文件 <out>.merging，
      (2) 从源清单里剔除输出文件自身，
      (3) 仅当结果非空（written>0）或目标文件原本不存在时才原子替换目标；
          结果为空的默认不覆盖、保留原语料（body 传 "allow_empty":true 可强制写出空文件）。
    这样既能真写出与 Tk 同字节的语料，又不会把用户已有语料误清空。

铁律：只返回真实读取/写出的数据；读不到、跑不了就照实返回 {"error": "..."}，
      绝不返回编造的假数据。合并只写工程 corpora/，绝不改 config.json。
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import _host                                     # 引擎宿主（同目录，bridge_api 已在 sys.path）

ENGINE_DIR = _host.IDE_DIR
CONFIG_PATH = _host.CONFIG_PATH
# Tk 版 DatasetPage._project_roots 的三个固定目录（相对工程根）—— 现在只是「默认清单」
SUBDIRS = ("article", "response", "corpora")

# 用户增删后的扫描目录清单存在这里（工程内、相对 data/ 的一个点文件）
SCAN_DIRS_FILE = ".scan_dirs.json"

# 训练语料固定文件名（与 core.project.PRETRAIN_CORPUS / SFT_CORPUS 一致）
CORPUS_NAMES = {"pretrain": "pretrain_corpus.txt", "sft": "sft_corpus.txt"}

MERGE_GROUP = "dataset"                          # 同一时刻每种 kind 只跑一个合并任务

_data_mod: Any = None


# ---------------------------------------------------------------- 依赖加载

def _data():
    """按需 import core.data（只依赖标准库）。失败抛异常，由调用方转成 error。

    导入期间临时关闭字节码写入：桥不该在 ENGINE_DIR 下生成/更新 __pycache__。
    """
    global _data_mod
    if _data_mod is not None:
        return _data_mod
    if ENGINE_DIR not in sys.path:
        sys.path.insert(0, ENGINE_DIR)
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        import core.data as _d  # type: ignore
    finally:
        sys.dont_write_bytecode = prev

    _data_mod = _d
    return _data_mod


# ---------------------------------------------------------------- 工程解析

def _read_config() -> dict:
    import json

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _resolve_project(override: str = "") -> str:
    """当前工程根：显式 override（须是目录） > recent_projects[0]。

    2026-10-21 撤兜底：没有工程直接报「请先创建工程」，不再回落默认工程。
    """
    if override:
        # 2026-10-23（用户）：?project= 也必须是带 .eproj 的工程目录
        return _host.require_project(override)
    recent = (_read_config().get("app") or {}).get("recent_projects") or []
    if isinstance(recent, list):
        for item in recent[:1]:   # 只认当前工程（2026-10-23）：不回落到第 2 个，防工程串
            if isinstance(item, str) and _host.is_project_dir(item):
                return os.path.abspath(item)
    raise RuntimeError("请先创建工程")   # 撤兜底（2026-10-21）


def _default_roots(proj: str) -> list[str]:
    """默认扫描目录：工程内的 data/article、data/response、data/corpora。"""
    return [os.path.join(proj, "data", k) for k in SUBDIRS]


def _distill_roots(proj: str) -> list[str]:
    """工程内用于存放蒸馏数据的目录（文章 / 响应）—— 设置里可默认引入。"""
    return [os.path.join(proj, "data", k) for k in ("article", "response")]


def _auto_distill() -> bool:
    """设置页「蒸馏数据集默认引入」（config.app.auto_distill_dirs，默认开启）。"""
    try:
        app = (_read_config().get("app") or {})
        v = app.get("auto_distill_dirs", True)
    except Exception:                            # noqa: BLE001  读不到就当默认开启
        return True
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("0", "false", "off", "no", "")


def _required_roots(proj: str) -> list[str]:
    """必须留在扫描清单里的目录（默认引入的蒸馏目录）；空 = 没有强制项。"""
    return _distill_roots(proj) if _auto_distill() else []


def _dirs_file(proj: str) -> str:
    """扫描目录清单文件：<工程>/data/.scan_dirs.json。"""
    return os.path.join(proj, "data", SCAN_DIRS_FILE)


def _load_dirs(proj: str) -> list[str]:
    """当前扫描目录清单（可含工程外目录、也可不含默认目录）。

    来源 `<工程>/data/.scan_dirs.json` = {"dirs": [绝对路径, ...]}：
      - 文件不存在（从没增删过）→ 设置开着「蒸馏数据集默认引入」时是三个默认目录，否则空；
      - 存在就是清单本身（空清单 = 用户全删了，尊重它，不回退默认）。
      - **强制项**：设置开着时，工程内 data/article、data/response 永远在最前面且不能移出。
    """
    req = _required_roots(proj)
    try:
        with open(_dirs_file(proj), encoding="utf-8") as fh:
            raw = json.load(fh)
        dirs = raw.get("dirs") if isinstance(raw, dict) else raw
        if not isinstance(dirs, list):
            raise ValueError("bad dirs")
        base = list(dirs)                         # 清单文件存在 → 只用清单本身
    except Exception:                            # noqa: BLE001 无清单/坏文件 → 默认（关了默认引入就是空）
        base = _default_roots(proj) if req else []
    out: list[str] = []
    for item in req + list(base):
        if not isinstance(item, str) or not item.strip():
            continue
        ap = os.path.abspath(os.path.expandvars(item.strip()))
        if ap not in out:
            out.append(ap)
    return out


def _save_dirs(proj: str, dirs: list) -> str:
    """原子写清单（先写 .tmp 再 replace），返回文件路径。"""
    path = _dirs_file(proj)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"dirs": list(dirs)}, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def _roots(proj: str) -> list[str]:
    """扫描/合并实际使用的目录清单：用户可增删，可指向工程外。"""
    return _load_dirs(proj)


def _dir_rows(proj: str, dirs: list) -> list[dict]:
    """给渲染层的行数据：是否存在 / 是否工程内 / 是否默认目录。"""
    defaults = _default_roots(proj)
    proj_abs = os.path.abspath(proj)
    rows = []
    locked = set(os.path.normcase(os.path.abspath(p)) for p in _required_roots(proj))
    for d in dirs:
        ap = os.path.abspath(d)
        inside = ap == proj_abs or ap.startswith(proj_abs + os.sep)
        rows.append({
            "path": ap,
            "name": os.path.basename(ap.rstrip("\\/")) or ap,
            "exists": os.path.isdir(ap),
            "inside": bool(inside),
            "default": d in defaults,
            "locked": os.path.normcase(ap) in locked,   # 默认引入的蒸馏目录：不可移出
        })
    return rows


def _corpus_path(proj: str, kind: str) -> str:
    """合并输出：<工程>/corpora/<pretrain|sft>_corpus.txt（用户 2026-10-19）。

    故意挪出 data/ —— 数据集扫描只扫 data/article|response|corpora，
    合并产物放在工程根下的 corpora/ 里，合并时永远找不到它，不会被再合一遍。
    """
    name = CORPUS_NAMES[kind]
    new_path = os.path.join(proj, "corpora", name)
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
    return new_path


def _norm_kind(query: dict) -> str:
    """归到两大类（格式判定/合并产物）：lora 与 sft 同吃问答语料。"""
    raw = str(query.get("kind") or "").strip().lower()
    return "sft" if raw in ("sft", "finetune", "微调", "lora") else "pretrain"


def _sel_kind(query: dict) -> str:
    """勾选清单键 = 训练方式（pretrain/continue/sft/lora 四页签各存各的，2026-10-15）。"""
    raw = str(query.get("kind") or "").strip().lower()
    return raw if raw in ("pretrain", "continue", "sft", "lora") else _norm_kind(query)


def _use_map(proj: str) -> dict:
    """采集启动时记的「训练用途」：文件名 → {kind, use}（pretrain/continue/sft/lora）；缺 = 未标注。"""
    p = os.path.join(proj, "data", "_collect_use.json")
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:                                # noqa: BLE001
        return {}


# ---------------------------------------------------------------- 文件事实

def _count_chars_lines(path: str) -> tuple[int, int]:
    """真实统计输出语料的字符数与行数（只读，逐块读）。

    newline="" 关闭通用换行转换，字符数口径与 core.data.audit_file 一致。
    """
    chars = 0
    lines = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            for chunk in iter(lambda: f.read(1 << 20), ""):
                chars += len(chunk)
                lines += chunk.count("\n")
    except OSError:
        return 0, 0
    return chars, lines


def _read_head(path: str, n: int = 200) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(n)
    except OSError:
        return ""


def _file_fact(path: str) -> dict:
    """文件的真实存在性 / 字节数 / 字符数 / 行数 / 修改时间。"""
    fact = {"path": path or "", "exists": False, "size_bytes": 0,
            "chars": 0, "lines": 0, "mtime": None}
    try:
        if path and os.path.isfile(path):
            st = os.stat(path)
            fact["exists"] = True
            fact["size_bytes"] = int(st.st_size)
            fact["chars"], fact["lines"] = _count_chars_lines(path)
            fact["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
    except OSError:
        pass
    return fact


# ---------------------------------------------------------------- 审查 → 行

def _kind_label(a: Any) -> str:
    if getattr(a, "looks_chat", False):
        return "对话"
    if getattr(a, "looks_text_json", False):
        return "JSON 文本"          # 一条记录一段正文的下载语料（2026-10-17）
    if getattr(a, "looks_jsonl", False):
        return "JSONL"
    return "纯文本"


def _fmt_verdict(a: Any, kind: str) -> tuple:
    """该文件在本 kind 下是否合格 —— 直接用 core.data 的官方判定，不在桥里另写一套规则。"""
    try:
        D = _data()
        checker = D.check_sft_format if kind == "sft" else D.check_pretrain_format
        ok, msg = checker(a)
        return bool(ok), str(msg or "")
    except Exception as exc:                     # noqa: BLE001
        return False, "格式检查不可用：%s" % exc


def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.abspath(str(p or "")))


def _kind_key(a: Any) -> str:
    """机器判定的类型键（对应 _kind_label 的中文）：chat=对话 / plain=纯文本 / other=判定不出（空白·损坏）。"""
    if bool(getattr(a, "looks_chat", False)):
        return "chat"
    if bool(getattr(a, "looks_plain", False)):
        return "plain"
    return "other"


def _file_tabs(a: Any) -> list:
    """文件归属哪个页签：文章→预训练；对话→微调；判定不出的两个页签都露面。

    两类清单分开之后每页只列自己那一类，勾选不再被另一类干扰；判定不出的两边都列，
    才能让「异常文件 = 扫描文件 − 可用文件」在两个页签里都对得上。
    """
    k = _kind_key(a)
    if k == "plain":
        return ["pretrain"]
    if k == "chat":
        return ["sft"]
    return ["pretrain", "sft"]


def _file_row(a: Any, kind: str = "pretrain", sel_paths=None, use: str = "") -> dict:
    fmt_ok, fmt_msg = _fmt_verdict(a, kind)
    return {
        "name": getattr(a, "name", "") or os.path.basename(getattr(a, "path", "")),
        "folder": os.path.basename(os.path.dirname(getattr(a, "path", ""))) or "",
        "path": getattr(a, "path", ""),
        "ext": (getattr(a, "ext", "") or "").lstrip(".") or "?",
        "kind": _kind_label(a),
        "chars": int(getattr(a, "chars", 0)),
        "size_bytes": int(getattr(a, "size_bytes", 0)),
        "lines": int(getattr(a, "lines", 0)),
        "ok": bool(getattr(a, "ok", True)),
        "encoding_ok": bool(getattr(a, "encoding_ok", True)),
        "looks_chat": bool(getattr(a, "looks_chat", False)),
        "looks_jsonl": bool(getattr(a, "looks_jsonl", False)),
        "looks_plain": bool(getattr(a, "looks_plain", False)),
        "issues": list(getattr(a, "issues", []) or []),
        # 本 kind 下的格式判定
        "fmt_ok": fmt_ok,
        "fmt_msg": fmt_msg,
        # 类型分栏：kind_key 给前端上色/分栏，tab 告诉前端这个文件该出现在哪个页签
        "kind_key": _kind_key(a),
        "tab": _file_tabs(a),
        # 蒸馏时标注的训练方式（pretrain/continue/sft/lora，空 = 未标注）；
        # 只是标注，不限制用途（用户 2026-10-15：标注了不一定真用这个训练方式）
        "use": use,
        # 勾选态（数据集页写盘、训练启动时按它选源）；None = 还没设置过任何勾选
        "selected": bool(sel_paths is not None and _norm_path(getattr(a, "path", "")) in sel_paths),
    }


def _scan(query: dict) -> tuple[Any, str, list[str], list]:
    """真实扫描：返回 (core.data 模块, 工程根, 三个目录, FileAudit 列表)。

    = 目录扫描 + 外部引用文件（用户 2026-10-16：添加外部文件=引用路径、不复制进工程）。"""
    D = _data()
    proj = _resolve_project(str(query.get("project") or ""))
    roots = _roots(proj)
    audits: list = []
    seen: set = set()
    for root in roots:
        if os.path.isdir(root):
            for a in D.scan_folder(root):
                k = os.path.normcase(os.path.abspath(getattr(a, "path", "")))
                if k not in seen:
                    seen.add(k)
                    audits.append(a)
    import _corpus
    for ref in _corpus.scan_refs(proj):
        k = os.path.normcase(ref)
        if k in seen or not os.path.isfile(ref):
            continue
        seen.add(k)
        try:
            audits.append(D.audit_file(ref))
        except Exception:  # noqa: BLE001  引用读不了就跳过（照实不编造）
            pass
    return D, proj, roots, audits


# ---------------------------------------------------------------- 只读接口

def scan(query: dict) -> dict:
    kind = _norm_kind(query)
    try:
        _D, proj, roots, audits = _scan(query)
    except Exception as exc:  # noqa: BLE001
        # 「还没有工程」是首启正常态，不算崩（2026-10-24 开源首启实测）：不记日志，只回空态
        if not any(h in str(exc) for h in ("请先创建工程", "还没有工程", "当前工程文件夹不存在")):
            _host._log_exc("dataset.scan", exc, kind=kind)  # 真的扫崩了才留堆栈
        return {"error": "扫描失败：%s" % exc, "kind": kind}
    import _corpus                                     # 勾选清单：数据集页与训练启动共用一份
    sel = _corpus.load_selection(proj, _sel_kind(query))   # 按「训练方式」取（四页签各存各的）
    sel_paths = set(_norm_path(p) for p in (sel or [])) if sel is not None else None
    uses = _use_map(proj)
    files = []
    for a in audits:
        nm = getattr(a, "name", "") or os.path.basename(getattr(a, "path", ""))
        use = str(((uses.get(nm) or {}) if isinstance(uses.get(nm), dict) else {}).get("use") or "")
        files.append(_file_row(a, kind, sel_paths, use=use))

    # 本页签的四个数字：扫描文件 / 可用文件 / 异常文件 / 可用字符
    #   扫描文件 = 本类型文件 + 判定不出的文件（另一类的文件不在本页签出现）
    #   可用文件 = 本页签里「扫描通过 且 本类型格式合格」的
    #   异常文件 = 扫描文件 − 可用文件（空白 / 过短 / 损坏 / 格式有误）
    tabkey = "sft" if kind == "sft" else "pretrain"
    in_tab = [f for f in files if tabkey in (f.get("tab") or [])]
    usable = [f for f in in_tab if f.get("ok") and f.get("fmt_ok")]
    return {
        "kind": kind,
        "tab": tabkey,
        "project": proj,
        "data_dir": os.path.join(proj, "data"),       # 数据实际存放目录（界面按它显示，而不是工程根）
        "roots": roots,
        "root_missing": [r for r in roots if not os.path.isdir(r)],
        "roots_detail": _roots_detail(roots, files, proj),
        "files": files,
        "total": len(files),
        "total_chars": sum(f["chars"] for f in files),
        "ok_count": sum(1 for f in files if f["ok"]),
        "bad_count": sum(1 for f in files if not f["ok"]),
        "fmt_ok_count": sum(1 for f in files if f.get("fmt_ok")),
        "tab_total": len(in_tab),
        "usable_count": len(usable),
        "tab_bad": len(in_tab) - len(usable),
        "usable_chars": sum(int(f.get("chars") or 0) for f in usable),
        "sel_known": sel is not None,
        "selected_paths": list(sel or []),
        "scan_source": "core.data.scan_folder",
        "corpus": _file_fact(_corpus_path(proj, kind)),
    }


def _roots_detail(roots: list, files: list, proj: str = "") -> list:
    """每个扫描目录归属哪个页签（按目录内文件类型判定；两类都有 / 空目录 = 两个页签都列）。"""
    out = []
    for r in roots:
        rr = _norm_path(r)
        mine = [f for f in files if _norm_path(f.get("path", "")).startswith(rr)]
        keys = set(f.get("kind_key") for f in mine)
        tabs = set()
        for f in mine:
            for t in (f.get("tab") or []):
                tabs.add(t)
        if not tabs:
            # 空目录按工程内约定猜：data/article = 文章（预训练），data/response = 对话（微调）
            name = os.path.basename(r.rstrip("\\/")).lower()
            if name in ("article", "articles", "文章"):
                tabs = {"pretrain"}
            elif name in ("response", "responses", "chat", "sft", "问答", "响应"):
                tabs = {"sft"}
            else:
                tabs = {"pretrain", "sft"}
        out.append({
            "path": r,
            "missing": not os.path.isdir(r),
            # 默认引入的蒸馏目录：界面上不给 ✕（后端也会拒绝移出）
            "locked": bool(proj) and os.path.normcase(os.path.abspath(r))
                      in set(os.path.normcase(os.path.abspath(p)) for p in _required_roots(proj)),
            "files": len(mine),
            "kind": ("mixed" if len(keys) > 1 else (list(keys)[0] if keys else "empty")),
            "tab": sorted(tabs),
        })
    return out


def selection_get(query: dict) -> dict:
    """当前工程·当前数据集类型下，文件清单里勾选参与合并的文件（训练启动时按它选源）。"""
    import _corpus
    kind = _sel_kind(query)
    proj = _resolve_project(str(query.get("project") or ""))
    paths = _corpus.load_selection(proj, kind)
    return {"ok": True, "kind": kind, "project": proj, "file": _corpus.sel_file(proj),
            "known": paths is not None, "paths": list(paths or []), "count": len(paths or [])}


def selection_post(body: dict) -> dict:
    """写勾选清单：{"kind": "...", "paths": [绝对路径...]}（写盘，训练启动时按它选源）。"""
    import _corpus
    body = body or {}
    kind = _sel_kind({"kind": body.get("kind") or ""})
    query = body.get("_query") or {}
    proj = _resolve_project(str(body.get("project") or query.get("project") or ""))
    raw = body.get("paths")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return {"ok": False, "error": "paths 必须是数组", "kind": kind, "project": proj}
    paths = [str(p) for p in raw if str(p or "").strip()]
    try:
        path = _corpus.save_selection(proj, kind, paths)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "error": "保存勾选失败：%s" % exc, "kind": kind, "project": proj}
    return {"ok": True, "kind": kind, "project": proj, "file": path,
            "paths": paths, "count": len(paths)}


def check(query: dict) -> dict:
    kind = _norm_kind(query)
    try:
        D, proj, roots, audits = _scan(query)
    except Exception as exc:  # noqa: BLE001
        return {"error": "格式检查失败：%s" % exc, "kind": kind}
    try:
        report = D.format_check_report(audits, kind)
    except Exception as exc:  # noqa: BLE001
        return {"error": "生成报告失败：%s" % exc, "kind": kind}
    return {
        "kind": kind,
        "project": proj,
        "roots": roots,
        "total": len(audits),
        "report": report,
    }


# ---------------------------------------------------------------- 真合并（后台线程）

def _run_merge(emit, task, kind: str, proj: str, sources, out_path: str, allow_empty: bool) -> None:
    """后台线程里干的真活：扫描 → 交给 _corpus.merge（训练启动时自动合并用的是同一份实现）。"""
    import _corpus                                  # 同目录；合并原语
    D = _data()                                     # core.data 不可用 → 早失败（异常由任务框架转 error）

    roots = _roots(proj)
    emit("log", {"text": "扫描工程数据目录：" + " · ".join(roots)})
    audits: list = []
    for root in roots:
        if os.path.isdir(root):
            audits.extend(D.scan_folder(root))
    task.set_stats(scanned=len(audits))
    emit("log", {"text": "扫描到 %d 个文件。" % len(audits)})

    picked: list = []

    def on_selected(selected: list) -> None:
        picked[:] = selected                           # 留一份给最终结果（sources 清单）
        task.set_stats(selected=len(selected), sources=[
            {"name": a.name, "path": a.path, "chars": int(a.chars), "kind": _kind_label(a),
             "ok": bool(a.ok)}
            for a in selected
        ])

    emit("start", {"kind": kind, "out": out_path, "selected": len(audits)})
    prev_fact = _file_fact(out_path)                   # 合并前的真实事实（用于 prev_* 字段）
    res = _corpus.merge(kind, proj, sources=sources, allow_empty=allow_empty, emit=emit,
                        audits=audits, on_selected=on_selected)
    if not res.get("ok"):
        raise RuntimeError(str(res.get("error") or "合并失败"))

    now = _file_fact(out_path)
    source_list = [
        {"name": getattr(a, "name", ""), "path": getattr(a, "path", ""),
         "chars": int(getattr(a, "chars", 0)), "kind": _kind_label(a), "ok": bool(getattr(a, "ok", True))}
        for a in picked
    ]

    final = {
        "ok": True,
        "kind": kind,
        "project": proj,
        "out": res.get("out") or out_path,
        "replaced": bool(res.get("replaced")),
        "written": int(res.get("written") or 0),
        "skipped": int(res.get("skipped") or 0),
        "pairs": int(res.get("pairs") or 0),
        "merge_chars": int(res.get("merge_chars") or 0),
        "tmp_bytes": int(res.get("tmp_bytes") or 0),
        "out_bytes": now["size_bytes"],
        "out_chars": now["chars"],
        "out_lines": now["lines"],
        "head": _read_head(out_path, 200),
        "prev_bytes": int(prev_fact["size_bytes"]),
        "prev_chars": int(prev_fact["chars"]),
        "sources": source_list,
        "sources_count": len(source_list),
        "merged_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": res.get("note") or "",
    }
    task.result = final
    emit("result", final)


def merge(body: dict) -> dict:
    """真合并：起一个后台线程任务（_host.start_task），把工程数据写成固定语料。

    body: {"kind": "pretrain"|"sft", "sources": [abs, ...可选], "allow_empty": false}
    - sources 为空/缺省：扫描到的全部文件（与 Tk 版默认全选一致）
    - 返回 {ok, job, kind, project, out, running}，随后 GET /api/dataset/merge/status 取进度与结果
    """
    body = body or {}
    kind = _norm_kind(body)
    query = body.get("_query") or {}
    proj = _resolve_project(str(body.get("project") or query.get("project") or ""))
    raw_sources = body.get("sources")
    sources = raw_sources if isinstance(raw_sources, list) else None
    allow_empty = bool(body.get("allow_empty"))
    out_path = _corpus_path(proj, kind)
    job_name = "dataset:merge:%s" % kind
    try:
        _host.start_task(
            job_name,
            lambda emit, task, group: _run_merge(emit, task, kind, proj, sources, out_path, allow_empty),
            group="%s:%s" % (MERGE_GROUP, kind),
        )
    except RuntimeError as exc:                    # 同名任务已在跑（_host 互斥）
        return {"ok": False, "error": str(exc), "kind": kind, "job": job_name}
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "kind": kind}
    return {"ok": True, "job": job_name, "kind": kind, "project": proj,
            "out": out_path, "running": True,
            "note": "已启动合并任务，轮询 /api/dataset/merge/status 取进度与结果。"}


def merge_status(query: dict) -> dict:
    """后台合并任务的增量进度 + 结果（?since=<游标> 取新增事件）。"""
    kind = _norm_kind(query)
    try:
        since = int(float(str(query.get("since") or 0) or 0))
    except (TypeError, ValueError):
        since = 0
    name = "dataset:merge:%s" % kind
    proj = _resolve_project(str(query.get("project") or ""))
    out = _host.task_status(name, since)
    out["kind"] = kind
    out["project"] = proj
    out.setdefault("job", name)
    out["out"] = _corpus_path(proj, kind)
    if not out.get("exists"):
        out["running"] = False
        out["note"] = "该 kind 暂无合并任务记录（尚未合并或桥重启过）。"
    return out


# ---------------------------------------------------------------- 扫描目录增删

def dirs_get(query: dict) -> dict:
    """当前工程的数据目录清单（含是否工程内 / 是否存在 / 是否默认）。"""
    proj = _resolve_project(str(query.get("project") or ""))
    dirs = _load_dirs(proj)
    return {"ok": True, "project": proj, "file": _dirs_file(proj),
            "managed": os.path.isfile(_dirs_file(proj)),
            "defaults": _default_roots(proj), "dirs": _dir_rows(proj, dirs),
            "auto_distill": _auto_distill(), "required": _required_roots(proj),
            "count": len(dirs)}


def dirs_post(body: dict) -> dict:
    """增 / 删 / 复位扫描目录。

    body: {"action": "add"|"remove"|"reset", "dir": "<路径>"}
      - add：路径须是真实存在的文件夹；允许工程外；重复添加不报错（返回 note）；
      - remove：只改清单，绝不动磁盘上的文件；默认的三个目录也能删；
      - reset：删掉清单文件，回到三个默认目录。
    """
    body = body or {}
    action = str(body.get("action") or "").strip().lower()
    query = body.get("_query") or {}
    proj = _resolve_project(str(body.get("project") or query.get("project") or ""))
    cur = _load_dirs(proj)

    if action == "reset":
        try:
            os.remove(_dirs_file(proj))
        except FileNotFoundError:
            pass
        cur = _default_roots(proj)
        # 直接返回：复位＝删掉清单文件（回到「没配过」的状态），不要再写回去
        return {"ok": True, "action": "reset", "note": "已恢复为默认目录",
                "file": _dirs_file(proj), "project": proj,
                "dirs": _dir_rows(proj, cur), "count": len(cur)}
    elif action in ("add", "remove"):
        raw = str(body.get("dir") or "").strip()
        if not raw:
            return {"ok": False, "error": "没有收到文件夹路径",
                    "project": proj, "dirs": _dir_rows(proj, cur), "count": len(cur)}
        ap = os.path.abspath(os.path.expandvars(raw))
        if action == "add":
            if not os.path.isdir(ap):
                return {"ok": False, "error": "不是有效的文件夹：%s" % ap,
                        "project": proj, "dirs": _dir_rows(proj, cur), "count": len(cur)}
            if ap in cur:
                return {"ok": True, "action": "add", "note": "该文件夹已在清单里",
                        "project": proj, "dirs": _dir_rows(proj, cur), "count": len(cur)}
            cur = cur + [ap]
            note = "已加入扫描：%s" % ap
        else:
            if os.path.normcase(ap) in set(os.path.normcase(os.path.abspath(p)) for p in _required_roots(proj)):
                return {"ok": False,
                        "error": "该文件夹是工程内蒸馏数据目录，设置里「蒸馏数据集默认引入」已开启，不能移出。",
                        "project": proj, "dirs": _dir_rows(proj, cur), "count": len(cur)}
            if ap not in cur:
                return {"ok": False, "error": "该文件夹不在清单里：%s" % ap,
                        "project": proj, "dirs": _dir_rows(proj, cur), "count": len(cur)}
            cur = [d for d in cur if d != ap]
            note = "已移出扫描：%s" % ap
    else:
        return {"ok": False, "error": "未知操作：%s" % action,
                "project": proj, "dirs": _dir_rows(proj, cur), "count": len(cur)}

    path = _save_dirs(proj, cur)
    return {"ok": True, "action": action, "note": note, "file": path,
            "project": proj, "dirs": _dir_rows(proj, cur), "count": len(cur)}


def register(router) -> None:
    router.get("/api/dataset/scan", scan)
    router.get("/api/dataset/check", check)
    router.get("/api/dataset/dirs", dirs_get)
    router.get("/api/dataset/selection", selection_get)
    router.post("/api/dataset/selection", selection_post)
    router.post("/api/dataset/merge", merge)
    router.get("/api/dataset/merge/status", merge_status)
    router.post("/api/dataset/dirs", dirs_post)
