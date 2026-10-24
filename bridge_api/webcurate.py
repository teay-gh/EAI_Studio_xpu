"""bridge_api/webcurate.py —— 数据中心「网页整理」：规划 AI → 检索整理 → 审阅 AI → 回头改查询（2026-10-17）。

页面：renderer/pages/webcurate.js（导航键 webcurate）
引擎：engine/core/web_curator.py（三个角色都用 config.json#article 的三张模型 profile）

接口：
  GET  /api/webcurate/config        当前参数（config.json#webcurate）+ 三张模型档案 + 可用预设
  POST /api/webcurate/config/save   保存参数（主题 / 搜索引擎网址 / 输出名 / 采集篇数 / 并发 / 每篇字数 / 检索轮次）
  GET  /api/webcurate/models        三张模型档案（规划 / 整理 / 审阅：model、base_url 是否已配）
  POST /api/webcurate/models/save   保存模型配置（只改 config.json#article 的 titler/writer/reviewer）
  POST /api/webcurate/models/test   真发一次最小请求测连通
  POST /api/webcurate/start         开始整理（body 覆盖参数；不写 config.json）
  POST /api/webcurate/stop          停止
  GET  /api/webcurate/status?since= 增量事件（log / progress / result）+ 运行态
  GET  /api/webcurate/runs          最近产出的纯文本文件（data/webcurate/*.txt）
  GET  /api/webcurate/preview?path= 读产物正文（前 4000 字，页面预览用）

铁律：只写工程 data/webcurate 下的产物与 config.json#webcurate（参数落盘是用户点「保存参数」才做），
      不编假数据；模型/网络失败照实返回。
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import time
from typing import Any, Callable

import _host

KIND = "webcurate"
TASK_NAME = "webcurate"
TASK_GROUP = "collect"

# 三个角色 → config.json#article 的 profile 键（与 core/web_curator.py 读法一致）
ROLE_TABLE = (
    ("planner", "titler", "规划模型"),
    ("extractor", "writer", "整理模型"),
    ("reviewer", "reviewer", "审阅模型"),
)
MODEL_FIELDS = ("base_url", "model", "temperature", "max_tokens", "system_prompt")

DEFAULTS = {
    "topic": "",
    "engine_url": "https://cn.bing.com",
    "out_name": "web_set",      # 用户 2026-10-20：输出文件名也要有默认值（原来空着）
    "pieces": 20,
    "concurrency": 3,
    "max_chars": 500,          # 用户 2026-10-20：默认最大 500 字（原来 2000）
    "queries_per_round": 4,
    "pages_per_query": 3,
    "max_rounds": 3,
    "dup_limit": 40,          # 重复率上限%（用户 2026-10-21：高级设置）；警告线取其一半
}


def _as_int(v: Any, dflt: int) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return dflt


def _as_float(v: Any, dflt: float) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return dflt


def _section(cfg: dict) -> dict:
    sec = cfg.get(KIND)
    return sec if isinstance(sec, dict) else {}


def _params(cfg: dict) -> dict:
    sec = _section(cfg)
    out = dict(DEFAULTS)
    for k, dflt in DEFAULTS.items():
        v = sec.get(k, dflt)
        if isinstance(dflt, int):
            out[k] = _as_int(v, dflt)
        elif isinstance(dflt, float):
            out[k] = _as_float(v, dflt)
        else:
            out[k] = str(v or "")
    # 默认搜索引擎从 360 换成 Bing（用户 2026-10-17）：旧默认值原样存着的话按新默认处理，
    # 用户自己改成别的地址则原样保留。
    # 2026-10-18：用户口径「我给的就是 https://cn.bing.com」——字段里只留首页地址，
    # /search?q= 由引擎按搜索引擎自动补（首页是 JS 壳，服务端 ?q= 抓不到结果）。
    _eng = out["engine_url"].strip()
    if _eng in ("https://www.so.com/s?q={q}", "https://cn.bing.com/search?q={q}"):
        out["engine_url"] = DEFAULTS["engine_url"]
    # 边界保护（页面也会限制，这里兜底）
    out["pieces"] = min(500, max(1, out["pieces"]))
    out["concurrency"] = min(8, max(1, out["concurrency"]))
    out["max_chars"] = min(20000, max(200, out["max_chars"]))
    out["queries_per_round"] = min(12, max(1, out["queries_per_round"]))
    out["pages_per_query"] = min(8, max(1, out["pages_per_query"]))
    out["max_rounds"] = min(6, max(1, out["max_rounds"]))
    out["dup_limit"] = min(95, max(5, out["dup_limit"]))
    return out


def _models(cfg: dict) -> dict:
    """三个角色用本页自己的档案（用户 2026-10-20：config.webcurate.models）。"""
    ms = _models_sec(cfg)
    out = {}
    for role, _key, _label in ROLE_TABLE:
        p = ms.get(role) if isinstance(ms.get(role), dict) else {}
        out[role] = {
            "source": "config.webcurate.models." + role,
            "model": str(p.get("model") or ""),
            "base_url": str(p.get("base_url") or ""),
            "ready": bool(p.get("base_url") and p.get("model")),
        }
    return out


def _models_sec(cfg: dict, seed: bool = True) -> dict:
    """网页整理的模型档案（用户 2026-10-20：独立存放，不再借用 config.article）。

    以前三张卡直接读写 config.article 的 titler / writer / reviewer —— 两页共用同三个槽位，
    网页整理一保存就把语料蒸馏的系统提示词覆盖掉（实测发生过）。现在各自独立：
    config.webcurate.models.{planner,extractor,reviewer}。

    首次读取时（还没有这一段）把 article 同名槽位的 base_url / model / api_key /
    temperature / max_tokens 搬过来，提示词用本页出厂值，不搬（避免把语料蒸馏的提示词带过来）。
    """
    sec = cfg.get(KIND)
    if not isinstance(sec, dict):
        sec = {}
        cfg[KIND] = sec
    models = sec.get("models")
    if isinstance(models, dict) and models:
        return models
    if not seed:
        return {}
    try:
        from core.config import DEFAULT_CONFIG          # type: ignore
        dflt = DEFAULT_CONFIG.get(KIND) or {}
    except Exception:                                    # noqa: BLE001
        dflt = {}
    art = cfg.get("article") if isinstance(cfg.get("article"), dict) else {}
    models = {}
    for role, key, _label in ROLE_TABLE:
        src = art.get(key) if isinstance(art.get(key), dict) else {}
        base = dict(dflt.get(role) or {})
        for f in ("base_url", "model", "api_key", "temperature", "max_tokens"):
            v = src.get(f)
            if v not in (None, ""):
                base[f] = v
        base.setdefault("system_prompt", "")
        base.setdefault("system_prompt_en", base.get("system_prompt", ""))
        models[role] = base
    sec["models"] = models
    return models


def _article(cfg: dict) -> dict:
    art = cfg.get("article")
    return art if isinstance(art, dict) else {}


def _models_full(cfg: dict) -> list:
    """模型配置页要的三张卡：完整 profile（api_key 只给「有没有」+ 掩码，不下发明文）。"""
    ms = _models_sec(cfg)
    out = []
    for role, key, label in ROLE_TABLE:
        prof = ms.get(role) if isinstance(ms.get(role), dict) else {}
        raw = str(prof.get("api_key") or "")
        out.append({
            "role": role,
            "key": key,
            "label": label,
            "source": "config.webcurate.models." + role,
            "profile": {
                "base_url": prof.get("base_url", ""),
                "api_key_set": bool(raw),
                "api_key_mask": ("•" * min(len(raw), 24)) if raw else "",
                "model": prof.get("model", ""),
                "temperature": prof.get("temperature", ""),
                "max_tokens": prof.get("max_tokens", ""),
                "system_prompt": prof.get("system_prompt", ""),
                "system_prompt_en": prof.get("system_prompt_en", ""),
            },
        })
    return out


def _project_root(cfg: dict) -> str:
    return _host.project_root(cfg)


def _corpora_dir(cfg: dict) -> str:
    """网页采集产物目录（用户 2026-10-21：网页采集的数据单独放一个文件夹）。"""
    return os.path.join(_project_root(cfg), "data", "webcurate")


def _legacy_dir(cfg: dict) -> str:
    return os.path.join(_project_root(cfg), "data", "corpora")


def _migrate_legacy(cfg: dict) -> None:
    """旧产物搬家：data/corpora 里带索引的网页采集 .txt 挪进 data/webcurate。

    同名已存在就不动；失败就地保留（绝不删）。"""
    idx = _read_index(cfg)
    if not idx:
        return
    src_dir = _legacy_dir(cfg)
    dst_dir = _corpora_dir(cfg)
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except OSError:
        return
    for fn in list(idx):
        if not str(fn).lower().endswith(".txt"):
            continue
        src = os.path.join(src_dir, fn)
        dst = os.path.join(dst_dir, fn)
        if not os.path.isfile(src) or os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
        except OSError:
            continue


def _safe_name(name: str, topic: str) -> str:
    base = (name or "").strip() or (topic or "").strip() or "web"
    base = re.sub(r"[\\/:*?\"<>|\s]+", "_", base)[:60].strip("._") or "web"
    if not base.lower().endswith(".txt"):
        base += ".txt"
    return base


def _stamped_name(name: str, topic: str = "") -> str:
    """网页整理产物命名（用户 2026-10-22）：<名字>_<YYYYmmdd_HHMMSS>_web.txt。

    和语料蒸馏的 <名字>_<时间戳>_articles.txt 同一套读法（时间戳在尾段前），
    结尾用 _web 标明来源是网页整理。多次跑不再互相覆盖，也不用 _2 / _3 兜底。
    名字里已经带过「_时间戳_web」的不再叠一层（老产物改名后重跑不重复加）。
    """
    full = _safe_name(name, topic)                     # 已保证 .txt 结尾
    stem = full[:-len(".txt")]
    if re.search(r"_\d{8}_\d{6}_web$", stem):
        return full
    return "%s_%s_web.txt" % (stem, time.strftime("%Y%m%d_%H%M%S"))


def _index_path(cfg: dict) -> str:
    return os.path.join(_project_root(cfg), "data", "_web_curate.json")


def _read_index(cfg: dict) -> dict:
    p = _index_path(cfg)
    try:
        with io.open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


def _write_index(cfg: dict, name: str, meta: dict) -> None:
    d = _read_index(cfg)
    d[name] = meta
    p = _index_path(cfg)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# ------------------------------------------------------------------ 读接口

def api_config(query: dict[str, Any]) -> dict[str, Any]:
    cfg = _host.load_cfg()
    return {
        "ok": True,
        "params": _params(cfg),
        "models": _models(cfg),
        "models_full": _models_full(cfg),
        "defaults": dict(DEFAULTS),
        # 模型卡出厂值（重置用，用户 2026-10-20）：中文 / 英文提示词各一份
        "model_defaults": _host.model_defaults(KIND, [r for r, _k, _l in ROLE_TABLE]),
        "project": _project_root(cfg),
        "output_dir": _corpora_dir(cfg),
        "engine_hint": "填搜索引擎首页即可（如 https://cn.bing.com），检索路径与检索词自动补；也可直接写带 {q} 的完整地址。",
    }


def api_models(query: dict[str, Any]) -> dict[str, Any]:
    cfg = _host.load_cfg()
    return {"ok": True, "models": _models(cfg)}


def api_status(query: dict[str, Any]) -> dict[str, Any]:
    since = _as_int(query.get("since"), 0)
    out: dict[str, Any] = {"ok": True, "exists": False, "running": False,
                           "events": [], "next": 0, "total_events": 0}
    try:
        st = _host.task_status(TASK_NAME, since)
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    out.update(st or {})
    out["ok"] = True
    return out


def api_runs(query: dict[str, Any]) -> dict[str, Any]:
    cfg = _host.load_cfg()
    _migrate_legacy(cfg)          # 旧产物先搬进新家（用户 2026-10-21）
    d = _corpora_dir(cfg)
    idx = _read_index(cfg)
    items: list = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        names = []
    for fn in names:
        if not fn.lower().endswith(".txt"):
            continue
        p = os.path.join(d, fn)
        try:
            stt = os.stat(p)
        except OSError:
            continue
        meta = idx.get(fn) or {}
        chars = meta.get("chars", 0)
        if not chars and stt.st_size < 2_000_000:
            # 索引里没有（手工放进来 / 索引被清过）的文本，就地数一遍字数（2026-10-17）
            try:
                with io.open(p, encoding="utf-8", errors="replace") as fh:
                    chars = len(fh.read())
            except OSError:
                chars = 0
        items.append({
            "name": fn,
            "path": p,
            "size": stt.st_size,
            "mtime": int(stt.st_mtime),
            "chars": chars,
            "dup_rate": meta.get("dup_rate"),
            "topic": meta.get("topic", ""),
            "from_web": bool(meta),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"ok": True, "dir": d, "items": items[:40]}


def api_preview(query: dict[str, Any]) -> dict[str, Any]:
    cfg = _host.load_cfg()
    path = str(query.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "缺少 path"}
    root = os.path.abspath(_project_root(cfg))
    abspath = os.path.abspath(path)
    if not abspath.startswith(root):
        return {"ok": False, "error": "只能预览工程内的文件"}
    if not os.path.isfile(abspath):
        return {"ok": False, "error": "文件不存在：%s" % abspath}
    try:
        with io.open(abspath, encoding="utf-8", errors="replace") as f:
            text = f.read(4200)
    except OSError as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "path": abspath, "text": text, "truncated": len(text) >= 4200}


# ------------------------------------------------------------------ 写接口

def api_save(body: dict[str, Any]) -> dict[str, Any]:
    """保存参数到 config.json#webcurate（只有点「保存参数」才落盘）。"""
    cfg = _host.load_cfg()
    cur = _params(cfg)
    for k in DEFAULTS:
        if k in body:
            dflt = DEFAULTS[k]
            if isinstance(dflt, int):
                cur[k] = _as_int(body.get(k), cur[k])
            elif isinstance(dflt, float):
                cur[k] = _as_float(body.get(k), cur[k])
            else:
                cur[k] = str(body.get(k) or "")
    sec = cfg.get(KIND) if isinstance(cfg.get(KIND), dict) else {}
    sec.update(cur)
    cfg[KIND] = sec
    _host.save_cfg(cfg)
    return {"ok": True, "params": _params(cfg)}


def api_start(body: dict[str, Any]) -> dict[str, Any]:
    """开始整理：真起宿主线程跑 core.web_curator.WebCurator。参数来自 body，不写 config。"""
    try:
        _host.ensure_ide_path()
        from core.client import ChatClient                  # type: ignore
        from core.web_curator import WebCurator             # type: ignore
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "error": "导入引擎失败：%s: %s" % (type(exc).__name__, exc)}

    busy = _host.running_task(TASK_GROUP)
    if busy:
        return {"ok": False, "error": "已有采集/整理任务在运行（%s），请先停止。" % busy}

    cfg = _host.load_cfg()
    cur = _params(cfg)
    topic = str(body.get("topic", cur["topic"]) or "").strip()
    engine_url = str(body.get("engine_url", cur["engine_url"]) or "").strip()
    if not topic:
        return {"ok": False, "error": "请先填主题"}
    if not engine_url:
        return {"ok": False, "error": "请先填搜索引擎网址"}
    # 首页地址（https://cn.bing.com）也放行：引擎会补成 /search?q=（2026-10-18）。
    # 只拦「根本不是网址」的输入。
    if not re.match(r"^https?://[^\s/]+", engine_url):
        return {"ok": False, "error": "搜索引擎网址要写成完整地址，例如 https://cn.bing.com"}

    art = cfg.get("article") if isinstance(cfg.get("article"), dict) else {}
    models = _models(cfg)
    missing = [k for k, v in models.items() if not v["ready"]]
    if missing:
        return {"ok": False,
                "error": "模型未配置（%s）。请先在本页「模型配置」页签填好 base_url / model。"
                         % "、".join(missing)}

    try:
        _ms = _models_sec(cfg)
        planner = ChatClient.from_profile(_ms.get("planner") or {}, timeout=120)
        extractor = ChatClient.from_profile(_ms.get("extractor") or {}, timeout=120)
        reviewer = ChatClient.from_profile(_ms.get("reviewer") or {}, timeout=120)
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "error": "模型配置无效：%s: %s" % (type(exc).__name__, exc)}

    out_name = _stamped_name(str(body.get("out_name", cur["out_name"]) or ""), topic)
    root = _project_root(cfg)
    _migrate_legacy(cfg)          # 旧产物先搬进新家（用户 2026-10-21）
    out_dir = _corpora_dir(cfg)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": "创建目录失败：%s: %s" % (type(exc).__name__, exc)}
    out_path = os.path.join(out_dir, out_name)
    if not out_name or os.path.basename(out_path) != out_name:
        return {"ok": False, "error": "输出文件名不合法"}
    # 同名文件已存在就加 _2 / _3…（用户 2026-10-20：默认文件名固定，重复跑别把上一次的覆盖掉）
    if os.path.exists(out_path):
        stem, ext = os.path.splitext(out_name)
        for k in range(2, 1000):
            cand = "%s_%d%s" % (stem, k, ext)
            if not os.path.exists(os.path.join(out_dir, cand)):
                out_name = cand
                out_path = os.path.join(out_dir, out_name)
                break

    params = {
        "pieces": min(500, max(1, _as_int(body.get("pieces"), cur["pieces"]))),
        "concurrency": min(8, max(1, _as_int(body.get("concurrency"), cur["concurrency"]))),
        "max_chars": min(20000, max(200, _as_int(body.get("max_chars"), cur["max_chars"]))),
        "queries_per_round": min(12, max(1, _as_int(body.get("queries_per_round"), cur["queries_per_round"]))),
        "pages_per_query": min(8, max(1, _as_int(body.get("pages_per_query"), cur["pages_per_query"]))),
        "max_rounds": min(6, max(1, _as_int(body.get("max_rounds"), cur["max_rounds"]))),
        "dup_limit": min(95, max(5, _as_int(body.get("dup_limit"), cur["dup_limit"]))),
    }

    holder: dict[str, Any] = {}

    def do_stop() -> None:
        c = holder.get("curator")
        if c is not None:
            c.stop()

    def run(emit, _task, _group) -> None:
        emit("log", {"text": "[宿主] 开始整理 · 主题=%s" % topic})
        dup_limit = int(params.get("dup_limit", 40))
        cur_kwargs = {k: v for k, v in params.items() if k != "dup_limit"}   # dup_limit 换算成阈值显式传
        _lang = str(body.get("lang") or "").strip() or "zh"       # 界面语言（用户 2026-10-20）
        _mp = _models_sec(cfg)
        curator = WebCurator(
            planner, extractor, reviewer,
            lang=_lang,
            planner_sys=str((_mp.get("planner") or {}).get("system_prompt") or ""),
            extractor_sys=str((_mp.get("extractor") or {}).get("system_prompt") or ""),
            reviewer_sys=str((_mp.get("reviewer") or {}).get("system_prompt") or ""),
            topic=topic, engine_url=engine_url, out_path=out_path,
            on_event=lambda kind, payload: emit(kind, payload or {}),
            dup_warn=dup_limit / 200.0, dup_abort=dup_limit / 100.0,
            **cur_kwargs)
        holder["curator"] = curator
        try:
            res = curator.run()
        except Exception as exc:                            # noqa: BLE001
            emit("log", {"text": "[宿主] 整理失败：%s: %s" % (type(exc).__name__, exc)})
            raise
        _write_index(cfg, os.path.basename(res["path"]), {
            "topic": res["topic"], "engine_url": res["engine_url"], "chars": res["chars"],
            "paras": res["paras"], "dup_rate": res["dup_rate"],
            "rounds": len(res["rounds"]), "sources": len(res["sources"]),
            "at": int(time.time()),
        })

    try:
        _host.start_task(TASK_NAME, run, stop=do_stop, group=TASK_GROUP)
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "error": "启动失败：%s: %s" % (type(exc).__name__, exc)}

    return {
        "ok": True,
        "job": TASK_NAME,
        "project": root,
        "path": out_path,
        "out_name": out_name,
        "topic": topic,
        "engine_url": engine_url,
        "models": {k: v["model"] for k, v in models.items()},
        "params": params,
        "note": "参数来自本次请求，未写入 config.json。",
    }


def api_stop(_body: dict[str, Any]) -> dict[str, Any]:
    try:
        return _host.stop_task(TASK_NAME)
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def api_models_save(body: dict[str, Any]) -> dict[str, Any]:
    """保存模型配置：只改 config.json#article 的 titler / writer / reviewer。

    api_key 不传明文（留空或只有掩码点）就保留原值。
    """
    body = body or {}
    saved: list = []
    try:
        cfg = _host.load_cfg()
        models = _models_sec(cfg)          # 用户 2026-10-20：独立存档，不再写 config.article
        for role, key, _label in ROLE_TABLE:
            incoming = body.get(role)
            if not isinstance(incoming, dict):
                continue
            prof = models.get(role)
            if not isinstance(prof, dict):
                prof = {}
                models[role] = prof
            for field_name in MODEL_FIELDS:
                if field_name not in incoming:
                    continue
                val = incoming[field_name]
                if field_name == "temperature":
                    try:
                        prof[field_name] = float(val)
                    except (TypeError, ValueError):
                        prof[field_name] = val
                elif field_name == "max_tokens":
                    try:
                        prof[field_name] = int(float(val))
                    except (TypeError, ValueError):
                        prof[field_name] = val
                else:
                    prof[field_name] = str(val if val is not None else "")
            ak = incoming.get("api_key")
            if isinstance(ak, str) and ak.strip() and (set(ak) - set("•")):
                prof["api_key"] = ak.strip()          # 只覆盖真实明文
            saved.append(role)
        _host.save_cfg(cfg)
        return {"ok": True, "saved_roles": saved}
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def api_models_test(body: dict[str, Any]) -> dict[str, Any]:
    """真发一次最小请求（max_tokens<=16）测连通；body.profile 可覆盖表单里未保存的值。"""
    body = body or {}
    role = str(body.get("role") or "").strip()
    key = ""
    for r, k, _label in ROLE_TABLE:
        if r == role:
            key = k
    if not key:
        return {"ok": False, "error": "未知模型角色：%s" % (role or "(空)")}

    try:
        cfg = _host.load_cfg()
        _host.ensure_ide_path()
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "role": role, "error": "%s: %s" % (type(exc).__name__, exc)}

    prof = _models_sec(cfg).get(role) or {}
    prof = dict(prof) if isinstance(prof, dict) else {}
    override = body.get("profile")
    if isinstance(override, dict):
        for k in ("base_url", "model"):
            if str(override.get(k) or "").strip():
                prof[k] = str(override[k]).strip()
        ak = str(override.get("api_key") or "").strip()
        if ak and (set(ak) - set("•")):
            prof["api_key"] = ak

    base_url = str(prof.get("base_url") or "").strip()
    model = str(prof.get("model") or "").strip()
    if not base_url or not model:
        return {"ok": False, "role": role, "base_url": base_url, "model": model,
                "error": "base_url 或 model 未配置，无法测试连通。"}
    try:
        from core.client import ChatClient                  # type: ignore
        client = ChatClient.from_profile(prof, timeout=10.0)
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "role": role, "base_url": base_url, "model": model,
                "error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        reply = client.chat([{"role": "user", "content": "请只回复两个字：在线"}],
                            temperature=0.0, max_tokens=16)
        return {"ok": True, "role": role, "base_url": base_url, "model": model,
                "reply": (reply or "").strip()[:120]}
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "role": role, "base_url": base_url, "model": model,
                "error": "%s: %s" % (type(exc).__name__, exc)}


# ------------------------------------------------------------------ 数据管理（用户 2026-10-21：与其他蒸馏页一致）

def _drop_index(cfg: dict, names: list) -> None:
    """删掉索引里的条目（产物文件被删/合并后不再挂账）。"""
    d = _read_index(cfg)
    for n in names:
        d.pop(n, None)
    p = _index_path(cfg)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _dm_names(body: dict) -> list:
    names: list = []
    for n in (body.get("names") or []):
        nm = os.path.basename(str(n).strip())
        if nm and nm.lower().endswith(".txt") and nm not in names:
            names.append(nm)
    return names


def api_dm_delete(body: dict[str, Any]) -> dict[str, Any]:
    """删除选中的产物（数据管理「删除所选」）——移入回收站，可恢复（同仓库口径）。"""
    import _trash
    cfg = _host.load_cfg()
    d = _corpora_dir(cfg)
    proj = _project_root(cfg)
    names = _dm_names(body or {})
    if not names:
        return {"ok": False, "error": "没有选中要删除的产物"}
    removed: list = []
    missing: list = []
    for n in names:
        p = os.path.join(d, n)
        if os.path.isfile(p):
            moved = _trash.file_add(proj, p, "dataset")
            if not moved.get("ok"):
                return {"ok": False, "error": "%s: %s" % (n, moved.get("error") or "移入回收站失败")}
            removed.append(n)
        else:
            missing.append(n)
    _drop_index(cfg, removed)
    return {"ok": True, "removed": removed, "missing": missing}


def api_dm_export(body: dict[str, Any]) -> dict[str, Any]:
    """导出选中的产物原文到「另存为」给的路径（多个按列表序拼接，空行分隔）。"""
    b = body or {}
    target = str(b.get("path") or "").strip()
    if not target:
        return {"ok": False, "error": "缺少导出路径（应由「另存为」对话框给出）"}
    if os.path.isdir(target):
        return {"ok": False, "error": "导出路径指向一个目录：%s" % target}
    cfg = _host.load_cfg()
    d = _corpora_dir(cfg)
    names = _dm_names(b)
    if not names:
        return {"ok": False, "error": "没有选中要导出的产物"}
    parts: list = []
    used: list = []
    for n in names:
        p = os.path.join(d, n)
        if not os.path.isfile(p):
            continue
        try:
            with io.open(p, encoding="utf-8", errors="replace") as f:
                parts.append(f.read().rstrip("\r\n"))
            used.append(n)
        except OSError:
            continue
    if not used:
        return {"ok": False, "error": "选中的产物已经不存在（列表可能刚变过，请刷新列表）"}
    text = "\n\n".join(parts) + "\n"
    try:
        with io.open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except OSError as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "path": target, "files": len(used), "chars": len(text)}


def api_dm_merge(body: dict[str, Any]) -> dict[str, Any]:
    """产物合并：选中 ≥2 个 txt 合成一个新的产物（原文件保留，同名拒绝）。"""
    b = body or {}
    cfg = _host.load_cfg()
    d = _corpora_dir(cfg)
    names = _dm_names(b)
    if len(names) < 2:
        return {"ok": False, "error": "至少选两个产物才能合并"}
    raw_name = os.path.basename(str(b.get("name") or "").strip())
    stem = raw_name[:-4] if raw_name.lower().endswith(".txt") else raw_name
    if not stem:
        return {"ok": False, "error": "缺少合并后的文件名"}
    if any(ch in stem for ch in '\\/:*?"<>|'):
        return {"ok": False, "error": "文件名含非法字符"}
    out_name = _stamped_name(stem)                      # 合并产物同样带时间戳 + _web（用户 2026-10-22）
    stem = out_name[:-len(".txt")]
    out_path = os.path.join(d, out_name)
    if os.path.exists(out_path):
        return {"ok": False, "error": "%s：已存在同名产物，不能合并" % out_name}
    parts: list = []
    used: list = []
    idx = _read_index(cfg)
    for n in names:
        p = os.path.join(d, n)
        if not os.path.isfile(p):
            continue
        try:
            with io.open(p, encoding="utf-8", errors="replace") as f:
                parts.append(f.read().rstrip("\r\n"))
            used.append(n)
        except OSError:
            continue
    if len(used) < 2:
        return {"ok": False, "error": "选中的产物文件已不存在，请刷新列表"}
    text = "\n\n".join(parts) + "\n"
    try:
        with io.open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except OSError as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    first = idx.get(used[0]) or {}
    meta: dict = {"chars": len(text)}
    if first.get("topic"):
        meta["topic"] = first.get("topic")
    _write_index(cfg, out_name, meta)
    return {"ok": True, "name": out_name, "files": len(used), "chars": len(text)}


def register(router) -> None:
    router.get("/api/webcurate/config", api_config)
    router.get("/api/webcurate/models", api_models)
    router.get("/api/webcurate/status", api_status)
    router.get("/api/webcurate/runs", api_runs)
    router.get("/api/webcurate/preview", api_preview)

    router.post("/api/webcurate/config/save", api_save)
    router.post("/api/webcurate/models/save", api_models_save)
    router.post("/api/webcurate/models/test", api_models_test)
    router.post("/api/webcurate/start", api_start)
    router.post("/api/webcurate/stop", api_stop)
    router.post("/api/webcurate/delete", api_dm_delete)
    router.post("/api/webcurate/export", api_dm_export)
    router.post("/api/webcurate/merge", api_dm_merge)
