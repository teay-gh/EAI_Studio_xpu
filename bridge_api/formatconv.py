"""bridge_api/formatconv.py —— 数据中心「格式转换」：把下载来的各种 json 数据集转成 EAI 能读的格式（2026-10-17）。

页面：renderer/pages/formatconv.js（导航键 formatconv）
引擎：engine/core/format_convert.py（本地优先解析，读不出来才交给模型）
底层：engine/core/data.audit_file / extract_pairs / extract_texts（与数据集页同一套判定）

接口：
  GET  /api/formatconv/config        参数（config.json#formatconv）+ 转换模型 + 输出目录
  POST /api/formatconv/config/save   保存参数
  POST /api/formatconv/probe         识别源文件（记录数 / 本地能解析出多少 / 前 3 条样本）
  POST /api/formatconv/start         开始转换（body 覆盖参数）
  POST /api/formatconv/stop          停止
  GET  /api/formatconv/status?since= 增量事件 + 运行态
  GET  /api/formatconv/runs          转换记录（data/_format_conv.json）
  GET  /api/formatconv/preview?path= 读产物开头（页面预览）

铁律：只写 <工程>/data/corpora 与 config.json#formatconv；不编假数据，失败照实报。
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from typing import Any

import _host

KIND = "formatconv"
TASK_NAME = "formatconv"
TASK_GROUP = "convert"

DEFAULTS = {
    "src": "",
    "target": "auto",          # auto / sft / pretrain
    "out_name": "",
    "use_src_name": 0,         # 1 = 输出名用源文件名（用户 2026-10-21：删掉“留空=源文件名”兜底）
    "max_records": 200,
    "batch": 8,
}


def _as_int(v: Any, dflt: int) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return dflt


def _section(cfg: dict) -> dict:
    sec = cfg.get(KIND)
    return sec if isinstance(sec, dict) else {}


def _params(cfg: dict) -> dict:
    sec = _section(cfg)
    out = {}
    for k, dflt in DEFAULTS.items():
        v = sec.get(k, dflt)
        out[k] = _as_int(v, dflt) if isinstance(dflt, int) else str(v or "")
    if out["target"] not in ("auto", "sft", "pretrain"):
        out["target"] = "auto"
    out["max_records"] = min(20000, max(1, out["max_records"]))
    out["batch"] = min(20, max(1, out["batch"]))
    return out


def _assistant_profile(cfg: dict) -> dict:
    """转换用的模型 = AI 窗口（右侧助手）那套（用户 2026-10-18）。

    不再单独维护 config.article.writer：按 assistant → mimo llm-server → mimocode → 蒸馏 titler
    的回退顺序解析，和助手对话用的是同一个模型。
    """
    try:
        _host.ensure_ide_path()
        from core import assistant as A                       # noqa: PLC0415
        from bridge_api import assistant as _asst             # noqa: PLC0415
        prof, source = A.resolve_backend(_asst._Shim())
        out = dict(prof or {})
        out["_source"] = source
        return out
    except Exception:                                         # noqa: BLE001
        return {}


def _model(cfg: dict) -> dict:
    """上报给页面的模型信息（不暴露 api_key）。"""
    prof = _assistant_profile(cfg)
    return {
        "source": "AI 助手 · %s" % (prof.get("_source") or "未配置"),
        "model": str(prof.get("model") or ""),
        "base_url": str(prof.get("base_url") or ""),
        "ready": bool(prof.get("base_url") and prof.get("model") and prof.get("api_key")),
    }


def _project_root(cfg: dict) -> str:
    return _host.project_root(cfg)


def _corpora_dir(cfg: dict) -> str:
    return os.path.join(_project_root(cfg), "data", "corpora")


def _index_path(cfg: dict) -> str:
    return os.path.join(_project_root(cfg), "data", "_format_conv.json")


def _read_index(cfg: dict) -> list:
    p = _index_path(cfg)
    if not os.path.isfile(p):
        return []
    try:
        with io.open(p, encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, list) else []
    except (OSError, ValueError):
        return []


def _write_index(cfg: dict, items: list) -> None:
    p = _index_path(cfg)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump(items[:200], f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _ensure_scan_dir(cfg: dict, out_dir: str) -> bool:
    """把产物目录加进「数据集」扫描清单（data/.scan_dirs.json），否则转换出来的文件在数据集页看不见。
    用户自己删过 corpora 的话这里会补回来；已存在就什么都不做。"""
    proj = _project_root(cfg)
    p = os.path.join(proj, "data", ".scan_dirs.json")
    dirs = []
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                raw = json.load(f)
            dirs = [str(x) for x in (raw.get("dirs") if isinstance(raw, dict) else raw) or []]
        except (OSError, ValueError):
            dirs = []
    key = os.path.normcase(os.path.abspath(out_dir))
    if any(os.path.normcase(os.path.abspath(d)) == key for d in dirs):
        return False
    dirs.append(os.path.abspath(out_dir))
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump({"dirs": dirs}, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def _client(cfg: dict):
    """懒 import 引擎里的 ChatClient（桥启动时不要变重）；模型取 AI 助手那套。"""
    prof = _assistant_profile(cfg)
    prof.pop("_source", None)
    if not (prof.get("base_url") and prof.get("model") and prof.get("api_key")):
        return None
    from core.client import ChatClient          # noqa: PLC0415
    return ChatClient.from_profile(prof, timeout=120.0)


def _engine():
    from core.format_convert import FormatConverter, ConvertError   # noqa: PLC0415
    return FormatConverter, ConvertError


# ---------------------------------------------------------------- 接口

def config(_query: dict) -> dict:
    cfg = _host.load_cfg()
    return {
        "ok": True,
        "params": _params(cfg),
        "model": _model(cfg),
        "project": _project_root(cfg),
        "out_dir": _corpora_dir(cfg),
    }


def config_save(body: dict) -> dict:
    cfg = _host.load_cfg()
    sec = dict(_params(cfg))
    for k in DEFAULTS:
        if k in body:
            sec[k] = body[k]
    sec = {k: (sec[k] if isinstance(DEFAULTS[k], str) else _as_int(sec[k], DEFAULTS[k])) for k in DEFAULTS}
    cfg[KIND] = sec
    _host.save_cfg(cfg)
    return {"ok": True, "params": _params(_host.load_cfg())}


EXTS = (".json", ".jsonl", ".ndjson", ".txt")


def _expand_src(path: str) -> list:
    """源 = 单个文件或文件夹（用户 2026-10-21：「选择」菜单加选择文件夹）。

    文件夹取其中支持的数据文件（按文件名排序）；没有就返回空列表。"""
    if os.path.isdir(path):
        out: list = []
        try:
            names = sorted(os.listdir(path))
        except OSError:
            names = []
        for fn in names:
            p = os.path.join(path, fn)
            if os.path.isfile(p) and os.path.splitext(fn)[1].lower() in EXTS:
                out.append(p)
        return out
    return [path] if os.path.isfile(path) else []


def _stem_of(path: str) -> str:
    """产物 stem：与引擎 format_convert._write 同一套清洗。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", stem)[:70].strip("._") or "converted"


def name_check(body: dict) -> dict:
    """「使用源文件名」的重名检查（用户 2026-10-21）：重名 → 页面弹窗警告并取消勾选。

    按源文件名输出会得到 <stem>.jsonl / <stem>.txt（后缀由转换结果定），
    任一在产物目录里已存在、或本批内部同名，都算重名。"""
    cfg = _host.load_cfg()
    files = _expand_src(str(body.get("src") or "").strip())
    if not files:
        return {"ok": False, "error": "先选择要转换的文件或文件夹"}
    out_dir = _corpora_dir(cfg)
    try:
        existing = set(os.listdir(out_dir))
    except OSError:
        existing = set()
    names, dups, seen = [], [], set()
    for f in files:
        stem = _stem_of(f)
        names.append(stem)
        hit = [n for n in (stem + ".jsonl", stem + ".txt") if n in existing]
        if hit:
            dups.append("、".join(hit))
        elif stem in seen:
            dups.append(stem + "（本批重名）")
        seen.add(stem)
    return {"ok": True, "names": names, "dups": dups}


def probe(body: dict) -> dict:
    path = str(body.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "先选择要转换的文件或文件夹"}
    cfg = _host.load_cfg()
    Fmt, Err = _engine()
    files = _expand_src(path)
    if not files:
        return {"ok": False, "error": "源不存在，或文件夹里没有数据文件（.json/.jsonl/.ndjson/.txt）：%s" % path}
    target = str(body.get("target") or _params(cfg)["target"])
    max_records = _as_int(body.get("max_records"), _params(cfg)["max_records"])
    try:
        infos = [Fmt(src=f, target=target, max_records=max_records).probe() for f in files]
        if len(infos) == 1:
            return {"ok": True, "info": infos[0]}
        kinds = {str(i.get("local_kind") or "") for i in infos}
        info = {
            "path": path,
            "ext": "folder",
            "files": len(infos),
            "size_bytes": sum(int(i.get("size_bytes") or 0) for i in infos),
            "records": sum(int(i.get("records") or 0) for i in infos),
            "pairs": sum(int(i.get("pairs") or 0) for i in infos),
            "texts": sum(int(i.get("texts") or 0) for i in infos),
            "looks_chat": any(i.get("looks_chat") for i in infos),
            "looks_text_json": any(i.get("looks_text_json") for i in infos),
            "issues": [],
            "local_kind": (kinds.pop() if len(kinds) == 1 else "mixed"),
            "samples": [s for i in infos for s in (i.get("samples") or [])][:3],
            "first_pairs": [p for i in infos for p in (i.get("first_pairs") or [])][:3],
            "first_texts": [t for i in infos for t in (i.get("first_texts") or [])][:2],
        }
        return {"ok": True, "info": info}
    except Err as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def start(body: dict) -> dict:
    cfg = _host.load_cfg()
    prm = _params(cfg)
    src = str(body.get("src") or prm["src"] or "").strip()
    if not src:
        return {"ok": False, "error": "先选择要转换的文件或文件夹"}
    files = _expand_src(src)
    if not files:
        return {"ok": False, "error": "源不存在，或文件夹里没有数据文件（.json/.jsonl/.ndjson/.txt）：%s" % src}
    running = _host.task_status(TASK_NAME)
    if running.get("running"):
        return {"ok": False, "error": "已有转换任务在运行，先停止"}

    target = str(body.get("target") or prm["target"])
    out_name = str(body.get("out_name") or prm["out_name"] or "")
    use_src_name = _as_int(body.get("use_src_name", prm["use_src_name"]), 0)
    if not use_src_name and not out_name:
        return {"ok": False, "error": "请填输出文件名，或勾选「使用源文件名」"}
    max_records = _as_int(body.get("max_records"), prm["max_records"])
    batch = _as_int(body.get("batch"), prm["batch"])
    out_dir = _corpora_dir(cfg)
    client = _client(cfg)
    stop_evt = threading.Event()
    Fmt, ConvertError = _engine()

    def job(emit, task, group):
        multi = len(files) > 1
        for f in files:
            if stop_evt.is_set():
                emit("log", {"text": "已停止（还剩 %d 个文件没转）" % (len(files) - files.index(f)),
                             "level": "warn"})
                break
            stem = os.path.splitext(os.path.basename(f))[0]
            if use_src_name:
                name = ""                      # 引擎按源文件名命名（后缀 .jsonl/.txt 由结果定）
            elif multi:
                name = out_name + "_" + stem
            else:
                name = out_name
            if multi:
                emit("log", {"text": "———— %s ————" % os.path.basename(f)})
            conv = Fmt(src=f, target=target, out_name=name, out_dir=out_dir,
                       client=client, on_event=lambda ev: emit(ev.get("kind", "log"), ev),
                       stop=stop_evt, max_records=max_records, batch=batch)
            try:
                rep = conv.run()
            except ConvertError as exc:
                emit("error", {"text": "%s：%s" % (os.path.basename(f), exc)})
                continue
            emit("result", rep)
            items = _read_index(cfg)
            items.insert(0, {
                "name": os.path.basename(rep.get("out") or ""),
                "out": rep.get("out"),
                "src": f,
                "target": rep.get("target"),
                "mode": rep.get("mode"),
                "pairs": rep.get("pairs"),
                "chars": rep.get("chars"),
                "bytes": rep.get("bytes"),
                "failed": rep.get("failed"),
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            _write_index(cfg, items)
        added = _ensure_scan_dir(cfg, out_dir)
        if added:
            emit("log", {"text": "已把 %s 加入「数据集」扫描目录" % out_dir, "level": "ok"})

    _host.start_task(TASK_NAME, job, stop=lambda: stop_evt.set(), group=TASK_GROUP)
    return {"ok": True, "task": TASK_NAME}


def stop(_body: dict) -> dict:
    return _host.stop_task(TASK_NAME)


def status(query: dict) -> dict:
    since = _as_int(query.get("since"), 0)
    st = _host.task_status(TASK_NAME, since)
    st["ok"] = True
    st["model"] = _model(_host.load_cfg())
    return st


def runs(_query: dict) -> dict:
    cfg = _host.load_cfg()
    items = _read_index(cfg)
    alive = []
    for it in items:
        p = it.get("out") or ""
        it["exists"] = bool(p and os.path.isfile(p))
        if it["exists"]:
            try:
                it["size_mb"] = round(os.path.getsize(p) / 1e6, 2)
            except OSError:
                it["size_mb"] = 0
        alive.append(it)
    return {"ok": True, "items": alive, "out_dir": _corpora_dir(cfg)}


def preview(query: dict) -> dict:
    path = str(query.get("path") or "").strip()
    cfg = _host.load_cfg()
    root = os.path.abspath(_project_root(cfg))
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在"}
    if os.path.commonpath([os.path.abspath(path), root]) != root:
        return {"ok": False, "error": "只能预览工程目录里的文件"}
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(4000)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "text": text, "truncated": os.path.getsize(path) > 4000}


# ---------------------------------------------------------------- 模型配置（单模型）
# 用户 2026-10-17：格式转换页也照蒸馏/整理页分三个页签，但模型配置**只有一个模型**
def register(router) -> None:
    router.get("/api/formatconv/config", config)
    router.post("/api/formatconv/config/save", config_save)
    router.post("/api/formatconv/probe", probe)
    router.post("/api/formatconv/name_check", name_check)
    router.post("/api/formatconv/start", start)
    router.post("/api/formatconv/stop", stop)
    router.get("/api/formatconv/status", status)
    router.get("/api/formatconv/runs", runs)
    router.get("/api/formatconv/preview", preview)
