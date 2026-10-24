"""bridge_api/article.py —— 「文章蒸馏」页（导航键 article）的真实数据接口。

对应 Tk 页 <项目目录>\\（Tk 版已删除）distill.py 的 DistillPage(kind="article")：
三个标签页 = 模型配置 / 采集中心 / 数据管理。

本轮已接真（全部复用 ENGINE_DIR 自己的引擎，见 bridge_api/_host.py，不另写一套）：

  GET  /api/article/config                 模型配置（读 config.json#article，api_key 明文不外发）
  GET  /api/article/collection             采集参数（读 config.json#article.collection，可被 start 的 body 覆盖）
  GET  /api/article/list                   数据管理样本列表（读工程 data/article/*.jsonl）
  GET  /api/article/collect/status?since=N 采集任务实时状态：运行态 + 增量事件（log/progress/done）+ 输出目录真实统计
  POST /api/article/models/save            真写 config.json#article.<role>（base_url/model/temperature/max_tokens/system_prompt；
                                           不传 api_key 即保留原值）
  POST /api/article/models/test            真发一次最小请求（max_tokens=16）测试该角色模型连通，成功/失败原文返回
  POST /api/article/collect/start          **真采集**：_host.start_task("collect:article", …) 跑 core.article_collector.Collector
                                           参数一律走 body 覆盖，**不写 config.json**
  POST /api/article/collect/stop           优雅停止（Collector.stop() → 已入队样本写完再退出）
  POST /api/article/export   导出数据集：本工程该板块多个样本文件合并成一个文件（路径由用户「另存为」给出）
  POST /api/article/delete   删除所选样本（按来源文件+行号精确删行，未选中的行原样保留）
  （导入已取消：数据管理只能导出、不能导入 —— 用户 2026-10-13）

铁律（本模块）：
  * 只读本地真实数据；读不到就照实返回 {"error": "..."}，**绝不编造**样本 / 计数 / 配置值。
  * 采集参数一律从 start 的 body 取，不落 config.json；models/save 才写 config.json（且不动 api_key 除非显式传入）。
  * 不写 EAI_Studio/engine 下任何文件（config.json 仅由 models/save 显式写入，与 App 的保存行为一致）。

样本计数与解析规则与 core/storage.py 的 DatasetStore.import_jsonl 完全一致：
逐行 json.loads，只收录「非空且是 dict，且含 article / question / title 之一」的行；
排除文件名含 "rejected" 的 *_rejected.jsonl。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Callable

import _host                                     # 引擎宿主（同目录，bridge_api 已在 sys.path）
import _trash                                    # 回收站（同目录共用实现）

# ---------------------------------------------------------------- 路径

HERE = os.path.dirname(os.path.abspath(__file__))          # ...\EAI\EAI_Studio\bridge_api
WEB_DIR = os.path.dirname(HERE)                            # ...\EAI\EAI_Studio
EAI_ROOT = os.path.dirname(WEB_DIR)                        # ...\EAI
IDE_DIR = os.path.join(WEB_DIR, "engine")                # 2026-10-23：不再假设目录名叫 EAI_Studio，改名/换盘都成立                # ...\EAI\EAI_Studio/engine
CONFIG_PATH = os.path.join(IDE_DIR, "config.json")
KIND = "article"

# 采集任务在宿主里的名字 / 分组
TASK_NAME = "collect:article"
TASK_GROUP = "article"

# 与 distill.py 的 ARTICLE_MODELS 逐字对齐：(key, 卡片标题, 顶色条 tone)
ARTICLE_MODELS = (
    ("titler", "题目模型", "accent"),
    ("writer", "写作模型", "ink3"),
    ("reviewer", "审阅模型", "ink3"),
)
ROLE_KEYS = ("titler", "writer", "reviewer")

# 与 distill.py _fill_topics(kind="article") 同一组示例主题（本地 UI 便捷填充，非样本数据）
# 示例主题（用户 2026-10-18）：唐朝只给笼统说法、不给具体事件；其余示例不绑定任何国家/文化
EXAMPLE_THEMES = "唐朝历史\n机器学习基础\n数据库原理\n线性代数\n计算机网络基础"

# 数据管理表格：Tk 版标题截断到 40 字
TITLE_LIMIT = 40


# ---------------------------------------------------------------- 配置读取

def _load_config() -> dict[str, Any]:
    """读 EAI_Studio/engine/config.json；缺失 / 损坏返回 {}（不抛）。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_project(cfg: dict[str, Any], query: dict[str, Any] | None) -> tuple[str, str]:
    """当前工程根目录：?project= 覆盖 > config.app.recent_projects[0]（存在才用）。

    返回 (绝对路径, 来源说明)。2026-10-21 撤兜底：没有工程直接报「请先创建工程」。
    """
    q = query or {}
    ov = str(q.get("project") or "").strip()
    if ov:
        # 2026-10-23（用户）：?project= 必须是带 .eproj 的工程目录
        return _host.require_project(ov), "查询参数 project"
    recent = (cfg.get("app") or {}).get("recent_projects") or []
    if isinstance(recent, list):
        for item in recent[:1]:   # 只认当前工程（2026-10-23）：不回落到第 2 个，防工程串
            if isinstance(item, str) and _host.is_project_dir(item):
                return os.path.abspath(item), "config.app.recent_projects[0]"
    raise RuntimeError("请先创建工程")   # 撤兜底（2026-10-21）：不再回落默认工程


def _migrate_txt_flat(out_dir: str) -> None:
    """老布局 <板块>/txt/*.txt → 抬到 <板块>/（用户 2026-10-20：文章产物只有 txt，别套一层文件夹）。

    只移动、不删内容；移完 txt/ 空了就把它删掉。幂等，每个接口进来都顺手跑一次。
    """
    legacy = os.path.join(out_dir, "txt")
    if not os.path.isdir(legacy):
        return
    try:
        for fn in os.listdir(legacy):
            src = os.path.join(legacy, fn)
            dst = os.path.join(out_dir, fn)
            if os.path.isfile(src) and not os.path.exists(dst):
                os.replace(src, dst)
        if not os.listdir(legacy):
            os.rmdir(legacy)
    except OSError:
        pass


def _article_dir(project: str) -> str:
    """工程内的固定文章样本目录（与 Tk 版 proj.article_dir 一致：<工程>/data/article）。"""
    out = os.path.join(project, "data", KIND)
    _migrate_txt_flat(out)
    return out


# ---------------------------------------------------------------- 样本解析

def _records_from_jsonl(path: str) -> list[dict[str, Any]]:
    """逐行解析，规则同 DatasetStore.import_jsonl。"""
    out: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and (obj.get("article") or obj.get("question") or obj.get("title")):
                    out.append(obj)
    except Exception:
        return []
    return out


def _scan_records(out_dir: str) -> tuple[list[dict[str, Any]], list[str]]:
    """扫描文章目录：按 mtime 倒序取最近 3 个主 JSONL（排除 *rejected*），合并记录。

    与 Tk 版 DistillPage._refresh_data 同规则，返回 (记录列表, 来源文件名列表)。
    """
    if not out_dir or not os.path.isdir(out_dir):
        return [], []
    # 文章线的完整记录在 <out>/.meta/ 下（扫描器跳过那里，数据管理照读）；旧目录回退到顶层
    meta_dir = os.path.join(out_dir, ".meta")
    src_dir = meta_dir if os.path.isdir(meta_dir) else out_dir
    files = [
        os.path.join(src_dir, f)
        for f in os.listdir(src_dir)
        if f.endswith(".jsonl") and "rejected" not in f
    ]
    try:
        files.sort(key=os.path.getmtime, reverse=True)
    except Exception:
        files.sort()
    records: list[dict[str, Any]] = []
    sources: list[str] = []
    for path in files[:3]:
        records.extend(_records_from_jsonl(path))
        sources.append(os.path.basename(path))
    return records, sources


def meta_path_for(out_dir: str, txt_name: str) -> str:
    """文章线 txt 的配对记录文件（<板块目录>/.meta/<stem>.jsonl）。

    2026-10-22（用户）：老工程里改过名的 txt 会和 sidecar 名字对不上（实测 data/article/
    TC_20261020_095437_articles.txt 的 sidecar 还叫 TC2_20261020_095437.jsonl）——改名时
    只搬了 txt。精确名找不到时按尾部时间戳（YYYYMMDD_HHMMSS）在同一 .meta 里找唯一一个，
    找不到或撞车就返回空，宁可没主题也不张冠李戴。
    """
    base = os.path.basename(str(txt_name or ""))
    if base.endswith(".jsonl"):
        return os.path.join(out_dir, ".meta", base)
    stem = base[:-len("_articles.txt")] if base.endswith("_articles.txt") else (
        base[:-4] if base.endswith(".txt") else base)
    meta_dir = os.path.join(out_dir, ".meta")
    cand = os.path.join(meta_dir, stem + ".jsonl")
    if os.path.isfile(cand):
        return cand
    m = re.search(r"(\d{8}_\d{6})$", stem)
    if not m or not os.path.isdir(meta_dir):
        return ""
    stamp = m.group(1)
    try:
        hit = [n for n in os.listdir(meta_dir)
               if n.endswith(".jsonl") and n[:-len(".jsonl")].endswith(stamp)]
    except OSError:
        return ""
    return os.path.join(meta_dir, hit[0]) if len(hit) == 1 else ""


def _dir_stats(out_dir: str) -> dict[str, Any]:
    """输出目录的真实统计：每个 *.jsonl 的条数 / 大小 / 修改时间。只读。"""
    info: dict[str, Any] = {
        "dir": out_dir,
        "dir_exists": os.path.isdir(out_dir),
        "files": [],
        "total": 0,            # 主样本（排除 rejected）条数
        "main_files": 0,
        "rejected_files": 0,
        "rejected_records": 0,
    }
    if not info["dir_exists"]:
        return info
    meta_dir = os.path.join(out_dir, ".meta")
    src_dir = meta_dir if os.path.isdir(meta_dir) else out_dir
    try:
        names = os.listdir(src_dir)
        names += [n for n in os.listdir(out_dir) if "rejected" in n and n.endswith(".jsonl")]
    except OSError:
        return info
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(src_dir if "rejected" not in name else out_dir, name)
        rejected = "rejected" in name
        n = len(_records_from_jsonl(path))
        try:
            size = os.path.getsize(path)
            mt = os.path.getmtime(path)
        except OSError:
            size, mt = 0, None
        info["files"].append({
            "name": name,
            "records": n,
            "size_bytes": size,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mt)) if mt else None,
            "rejected": rejected,
        })
        if rejected:
            info["rejected_files"] += 1
            info["rejected_records"] += n
        else:
            info["main_files"] += 1
            info["total"] += n
    info["files"].sort(key=lambda x: (x.get("mtime") or ""), reverse=True)
    return info


# ---------------------------------------------------------------- GET 处理器

def api_config(query: dict[str, Any]) -> dict[str, Any]:
    """模型配置：返回真实 config.json 中 article 段的三张模型卡。

    api_key 出于安全不以明文下发：给 api_key_set 布尔 + 按真实长度生成的掩码。
    """
    cfg = _load_config()
    project, source = _resolve_project(cfg, query)
    section = cfg.get(KIND) or {}
    models = []
    for key, label, tone in ARTICLE_MODELS:
        prof = section.get(key) or {}
        if not isinstance(prof, dict):
            prof = {}
        raw_key = str(prof.get("api_key") or "")
        models.append({
            "key": key,
            "label": label,
            "tone": tone,
            "profile": {
                "base_url": prof.get("base_url", ""),
                "api_key_set": bool(raw_key),
                "api_key_mask": ("\u2022" * min(len(raw_key), 24)) if raw_key else "",
                "model": prof.get("model", ""),
                "temperature": prof.get("temperature", ""),
                "max_tokens": prof.get("max_tokens", ""),
                "system_prompt": prof.get("system_prompt", ""),
            },
        })
    return {
        "project": project,
        "project_source": source,
        "config_path": CONFIG_PATH,
        "config_exists": os.path.isfile(CONFIG_PATH),
        "models": models,
        # 出厂默认值：「填充示例」= 恢复默认（用户 2026-10-18）
        "defaults": _host.model_defaults(KIND, [m[0] for m in ARTICLE_MODELS]),
    }


def api_collection(query: dict[str, Any]) -> dict[str, Any]:
    """采集参数：本工程上次填过的表单快照 > config.json#article.collection > 出厂值。

    用户 2026-10-19：原来只读 config（采集页从不回写 config），切页回来填过的东西全没了。
    表单快照存在 <工程>/state/ui_state.json 的 collect_form.article，输入即存。
    """
    cfg = _load_config()
    project, source = _resolve_project(cfg, query)
    coll = ((cfg.get(KIND) or {}).get("collection") or {})
    if not isinstance(coll, dict):
        coll = {}
    out_dir = _article_dir(project)
    fields = {
        "themes": coll.get("themes", ""),
        "count": coll.get("count", 20),
        "concurrency": coll.get("concurrency", 2),
        "min_words": coll.get("min_words", 200),
        "max_words": coll.get("max_words", 1000),
        "dataset_name": coll.get("dataset_name", "article_set"),
        "discard_on_reject": bool(coll.get("discard_on_reject", True)),
        "save_rejected": bool(coll.get("save_rejected", False)),
        "mode": "stream",   # 固定流式（采集器无批量路径）
        "stage": ("continue" if coll.get("stage") == "continue" else "pretrain"),
    }
    saved = _host.load_collect_form(project, KIND)
    for k in list(fields.keys()):
        if k in saved and saved[k] is not None:
            fields[k] = saved[k]
    return {
        "project": project,
        "project_source": source,
        "output_dir": out_dir,
        "output_dir_exists": os.path.isdir(out_dir),
        "config_output_dir": coll.get("output_dir", ""),
        "example_themes": EXAMPLE_THEMES,
        "fields": fields,
    }


def api_save_collection(body: dict[str, Any]) -> dict[str, Any]:
    """保存采集表单（用户 2026-10-19）：切页 / 切工程 / 重开应用回来，已填内容要还在。"""
    cfg = _load_config()
    project, _src = _resolve_project(cfg, body)
    keys = ("themes", "count", "concurrency", "min_words", "max_words",
            "dataset_name", "discard_on_reject", "save_rejected", "stage", "mode")
    fields = {k: body[k] for k in keys if k in body}
    _host.save_collect_form(project, KIND, fields)
    return {"ok": True, "project": project, "saved": fields}


def _collect_date(rec: dict[str, Any], fallback: Any = "") -> str:
    """样本的采集日期（精确到日）：优先记录里的 created_at；老记录退回按 id 里的毫秒时间戳换算。"""
    ca = str(rec.get("created_at") or "").strip()
    if len(ca) >= 10 and ca[4] == "-" and ca[7] == "-":
        return ca[:10]
    raw = str(fallback or "").split("-")[0].strip()
    if raw.isdigit() and len(raw) >= 10:
        try:
            return time.strftime("%Y-%m-%d", time.localtime(int(raw) / 1000.0))
        except (ValueError, OSError, OverflowError):
            pass
    return ""


def _sample_files(out_dir: str) -> list[str]:
    """数据管理列表看的样本文件（与 _scan_records 同规则）：*.jsonl、排除 rejected，按 mtime 倒序。
    文件视图进来后每个文件都要能翻到逐条，不再只取最近 3 个。"""
    if not out_dir or not os.path.isdir(out_dir):
        return []
    src_dir = _sample_source_dir(out_dir)
    try:
        files = [os.path.join(src_dir, f) for f in os.listdir(src_dir)
                 if f.endswith(".jsonl") and "rejected" not in f]
    except OSError:
        return []
    try:
        files.sort(key=os.path.getmtime, reverse=True)
    except Exception:
        files.sort()
    return files


FILE_TEXT_LIMIT = 2_000_000      # 整文件预览读取上限（2MB，超出截断并标注）


def _file_state_map(out_dir: str) -> dict:
    """文件启用状态注册表：<输出目录>/../_file_state.json（没记录 = 默认启用）。"""
    p = os.path.join(os.path.dirname(out_dir), "_file_state.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def file_state(body: dict[str, Any]) -> dict[str, Any]:
    """改单个文件的启用状态（数据管理文件视图的开关；当前是记录性质，用户 2026-10-14）。"""
    cfg = _load_config()
    project, _source = _resolve_project(cfg, body or {})
    out_dir = _article_dir(project)
    name = os.path.basename(str((body or {}).get("name") or "").strip())
    if not name:
        return {"ok": False, "error": "缺少文件名"}
    enabled = bool((body or {}).get("enabled"))
    p = os.path.join(os.path.dirname(out_dir), "_file_state.json")
    try:
        data = _file_state_map(out_dir)
        data[name] = {"enabled": enabled}
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": "保存失败：%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "name": name, "enabled": enabled}


def _file_rows(out_dir: str) -> list[dict[str, Any]]:
    """数据管理「文件视图」的行：txt/ 下的产物文件（整文件 = 用户口径里的 jsonl/txt）。

    逐条数据来自 .meta/<stem>.jsonl（meta 字段 = 记录里 src 的文件名，前端按它对位）；
    meta_path 是这对产物里 jsonl 的绝对路径 —— 删文件时两条一起进回收站，不留孤儿记录。
    """
    txt_dir = out_dir   # 2026-10-20（用户）：正文直接放板块目录，不再有 txt/ 子文件夹
    rows: list[dict[str, Any]] = []
    if not os.path.isdir(txt_dir):
        return rows
    state = _file_state_map(out_dir)
    for fn in sorted(os.listdir(txt_dir)):
        if not fn.endswith(".txt") or "rejected" in fn.lower():
            continue
        p = os.path.join(txt_dir, fn)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        stem = fn[:-len("_articles.txt")] if fn.endswith("_articles.txt") else fn[:-4]
        meta_path = meta_path_for(out_dir, fn)      # 改名过的老工程按时间戳兜底（用户 2026-10-22）
        meta_name = os.path.basename(meta_path) if meta_path else (stem + ".jsonl")
        rows.append({
            "name": fn,
            "stem": stem,
            "meta": meta_name,
            "meta_path": meta_path if os.path.isfile(meta_path) else "",
            "path": p,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "enabled": bool((state.get(fn) or {}).get("enabled", True)),
        })
    rows.sort(key=lambda r: r.get("mtime") or 0, reverse=True)
    return rows


def api_file_text(query: dict[str, Any]) -> dict[str, Any]:
    """单个产物文件的全文（文件视图单击行 → 下方栏显示整个文件）。只认 txt/ 下的裸文件名，防目录穿越。"""
    cfg = _load_config()
    project, _source = _resolve_project(cfg, query)
    out_dir = _article_dir(project)
    name = os.path.basename(str(query.get("name") or "").strip())
    if not name:
        return {"ok": False, "error": "缺少文件名"}
    path = os.path.join(out_dir, name)
    if not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在：%s" % name}
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read(FILE_TEXT_LIMIT + 1)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": "读取失败：%s: %s" % (type(exc).__name__, exc)}
    truncated = len(text) > FILE_TEXT_LIMIT
    return {"ok": True, "name": name,
            "text": text[:FILE_TEXT_LIMIT], "truncated": truncated}


def _records_with_pos(out_dir: str) -> tuple[list[dict[str, Any]], list[tuple[str, int]]]:
    """列表用的记录 + 每条记录的 (来源文件名, 文件内物理行号 1 起)；删除时按它对位。"""
    records: list[dict[str, Any]] = []
    pos: list[tuple[str, int]] = []
    for path in _sample_files(out_dir):
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(obj, dict) and (obj.get("article") or obj.get("question") or obj.get("title")):
                        records.append(obj)
                        pos.append((name, i, _line_key(raw)))
        except OSError:
            continue
    return records, pos


def _line_key(raw: str) -> str:
    """一行样本内容的指纹（sha1 前 12 位）：删除时用它确认「这一行还是原来那条样本」，
    否则行号会因为别的删除/新增而移动，拿着旧列表重复删就会误伤其它样本。"""
    return hashlib.sha1(raw.strip().encode("utf-8")).hexdigest()[:12]


def _target_map(body: dict[str, Any], src_dir: str) -> dict[str, dict[int, str]]:
    """把前端给的 [{src, line, key}] 收成 {文件名: {行号: 指纹}}；文件名必须就在样本目录里（防路径逃逸）。"""
    want: dict[str, dict[int, str]] = {}
    for t in (body.get("targets") or []):
        if not isinstance(t, dict):
            continue
        name = os.path.basename(str(t.get("src") or "").strip())
        if (not name.endswith(".jsonl")) or ("rejected" in name) or (".." in name):
            continue
        try:
            ln = int(t.get("line"))
        except (TypeError, ValueError):
            continue
        if ln <= 0:
            continue
        path = os.path.join(src_dir, name)
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(src_dir):
            continue
        if not os.path.isfile(path):
            continue
        want.setdefault(name, {})[ln] = str(t.get("key") or "").strip()
    return want


def _drop_lines(path: str, drop: dict[int, str]) -> tuple[int, int, list[str]]:
    """按物理行号删行写回，返回 (删掉几条, 因指纹对不上被跳过几条, 删下来的原始行)。

    未命中的行逐字节原样保留（含原换行风格）；带了指纹但当前行内容对不上 → 不删（说明行号已过期）。
    删下来的原始行（含原换行符）交给回收站留档，恢复时逐字节写回。
    """
    with open(path, "r", encoding="utf-8", newline="") as f:
        lines = f.read().splitlines(keepends=True)
    kept: list[str] = []
    dropped: list[str] = []
    removed = stale = 0
    for i, ln in enumerate(lines, 1):
        want = drop.get(i)
        if want is None:
            kept.append(ln)
            continue
        if want and _line_key(ln) != want:
            stale += 1                  # 行号已过期（列表是旧的）→ 这条不动
            kept.append(ln)
            continue
        removed += 1
        dropped.append(ln)
    if not removed:
        return 0, stale, dropped
    tmp = path + ".eai-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write("".join(kept))
    os.replace(tmp, path)
    return removed, stale, dropped


def _read_records(path: str) -> list[dict[str, Any]]:
    """读一个样本文件里的记录（跳过空行/坏行）。"""
    out: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def _sync_article_txt(out_dir: str, name: str, force: bool = False) -> None:
    """文章线：按剩下的记录重建 txt/<stem>_articles.txt（与采集时同规则）；没记录了就把该 txt 删掉。

    force=True（回收站恢复时用）：派生文本不存在也要重建；默认只在已存在时更新。
    """
    src_path = os.path.join(_sample_source_dir(out_dir), name)
    txt_dir = out_dir   # 2026-10-20（用户）：正文直接放板块目录，不再有 txt/ 子文件夹
    txt_path = os.path.join(txt_dir, name[:-len(".jsonl")] + "_articles.txt")
    if not force and not os.path.exists(txt_path):
        return
    try:
        from core.storage import article_to_txt          # 惰性导入，别让桥启动就带上引擎
    except Exception:
        return
    recs = [r for r in _read_records(src_path) if r.get("article")]
    body = "".join(article_to_txt(r) + "\n\n" for r in recs)
    if not body.strip():
        try:
            os.remove(txt_path)
        except OSError:
            pass
        return
    os.makedirs(txt_dir, exist_ok=True)
    tmp = txt_path + ".eai-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    os.replace(tmp, txt_path)

def merge_datasets(body: dict[str, Any]) -> dict[str, Any]:
    """数据集合并（用户 2026-10-15）：把选中的多个数据集文件合成一个新的数据集（原文件保留）。

    产物对与采集同构：.meta/<stem>.jsonl（记录）+ txt/<stem>_articles.txt（派生文本，走
    _sync_article_txt）；记录按选中顺序逐行原样拼接（行键不变）；没有记录文件的旧产物按
    空行拆伪记录，内容不丢；同名直接拒绝（与仓库权重同口径）。"""
    b = body or {}
    cfg = _load_config()
    project, _source = _resolve_project(cfg, b)
    out_dir = _article_dir(project)
    txt_dir = out_dir   # 2026-10-20（用户）：正文直接放板块目录，不再有 txt/ 子文件夹
    meta_dir = os.path.join(out_dir, ".meta")
    names: list[str] = []
    for n in (b.get("names") or []):
        nm = os.path.basename(str(n).strip())
        if nm and nm not in names:
            names.append(nm)
    if len(names) < 2:
        return {"ok": False, "error": "至少选两个数据集才能合并"}
    raw_name = os.path.basename(str(b.get("name") or "").strip())
    stem = raw_name[:-4] if raw_name.lower().endswith(".txt") else raw_name
    if stem.endswith("_articles"):
        stem = stem[: -len("_articles")]
    if not stem:
        return {"ok": False, "error": "缺少合并后的数据集名称"}
    if any(ch in stem for ch in '\\/:*?"<>|'):
        return {"ok": False, "error": "名称含非法字符"}
    out_txt = os.path.join(txt_dir, stem + "_articles.txt")
    out_meta = os.path.join(meta_dir, stem + ".jsonl")
    if os.path.exists(out_txt) or os.path.exists(out_meta):
        return {"ok": False, "error": "%s：已存在同名数据集，不能合并" % stem}
    src_dir = _sample_source_dir(out_dir)
    merged: list[str] = []
    used = 0
    for fn in names:
        p = os.path.join(txt_dir, fn)
        if not os.path.isfile(p):
            continue
        stem_i = fn[: -len("_articles.txt")] if fn.endswith("_articles.txt") else fn[:-4]
        meta_path = os.path.join(src_dir, stem_i + ".jsonl")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            merged.append(line.rstrip("\r\n"))
            except OSError:
                continue
        else:
            # 旧产物没记录文件：按空行拆伪记录，内容一字不丢
            try:
                with open(p, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            for chunk in text.split("\n\n"):
                chunk = chunk.strip()
                if chunk:
                    merged.append(json.dumps({"title": "", "theme": "", "article": chunk}, ensure_ascii=False))
        used += 1
    if used < 2:
        return {"ok": False, "error": "选中的数据集文件已不存在，请刷新列表"}
    if not merged:
        return {"ok": False, "error": "选中的数据集里没有可合并的内容"}
    os.makedirs(meta_dir, exist_ok=True)
    os.makedirs(txt_dir, exist_ok=True)
    tmp = out_meta + ".eai-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(merged) + "\n")
    os.replace(tmp, out_meta)
    _sync_article_txt(out_dir, stem + ".jsonl", force=True)
    return {
        "ok": True,
        "name": stem + "_articles.txt",
        "files": used,
        "records": len(merged),
        "dir": out_dir,
        "project": project,
    }


def delete_records(body: dict[str, Any]) -> dict[str, Any]:
    """删除所选样本 = 移入回收站：按 (来源文件, 物理行号) 精确删行，删下来的原始行留档
    <板块>/.trash/（可恢复），并同步该文件的派生文本。

    行级删除 —— 没被选中的行原样保留（不去重、不重排、不改字段），所以不会动到别的样本。
    """
    b = body or {}
    cfg = _load_config()
    project, _source = _resolve_project(cfg, b)
    out_dir = _article_dir(project)
    src_dir = _sample_source_dir(out_dir)
    want = _target_map(b, src_dir)
    if not want:
        return {"ok": False, "error": "没有指定要删除的样本（或样本已不存在）"}

    removed = 0
    stale = 0
    trashed = 0
    touched: list[str] = []
    for name in sorted(want):
        n, sk, dropped = _drop_lines(os.path.join(src_dir, name), want[name])
        stale += sk
        if n:
            removed += n
            touched.append(name)
            trashed += _trash.add(out_dir, name, dropped)   # 原始行先进回收站（可恢复）
            _sync_article_txt(out_dir, name)
    if not removed:
        return {"ok": False,
                "error": "这些样本已经不存在（列表可能刚变过，请刷新列表）" if stale else "没有可删除的样本"}
    return {"ok": True, "removed": removed, "stale": stale, "trashed": trashed, "files": touched,
            "dir": out_dir, "project": project}


def trash_list(query: dict[str, Any]) -> dict[str, Any]:
    """回收站列表（最新删除的在前）。key = 回收站条目 id（恢复 / 彻底删除按它选）。"""
    cfg = _load_config()
    project, _source = _resolve_project(cfg, query)
    out_dir = _article_dir(project)
    items = []
    for e in _trash.items(out_dir):
        rec_any = e.get("rec")
        rec: dict[str, Any] = rec_any if isinstance(rec_any, dict) else {}
        title = str(rec.get("title") or "")
        items.append({
            "key": str(e.get("tid") or ""),
            "id": _collect_date(rec, rec.get("id", "")),
            "title": title,
            "title_short": title[:TITLE_LIMIT],
            "theme": str(rec.get("theme") or ""),
            "deleted_at": str(e.get("deleted_at") or ""),
            "src": str(e.get("src") or ""),
            "article": str(rec.get("article") or ""),
        })
    return {"ok": True, "total": len(items), "items": items,
            "dir": _trash.trash_dir(out_dir), "project": project}


def _trash_sync(out_dir: str, name: str) -> None:
    _sync_article_txt(out_dir, name, force=True)


def trash_restore(body: dict[str, Any]) -> dict[str, Any]:
    """恢复回收站里选中的样本：原始行按原字节写回来源文件（文件没了就重建）+ 重建派生文本。"""
    b = body or {}
    cfg = _load_config()
    project, _source = _resolve_project(cfg, b)
    out_dir = _article_dir(project)
    src_dir = _sample_source_dir(out_dir)
    return _trash.restore(out_dir, b.get("keys"), src_dir, _trash_sync)


def trash_purge(body: dict[str, Any]) -> dict[str, Any]:
    """彻底删除回收站里选中的条目（永久删，不再留档）。"""
    b = body or {}
    cfg = _load_config()
    project, _source = _resolve_project(cfg, b)
    out_dir = _article_dir(project)
    return _trash.purge(out_dir, b.get("keys"))


def trash_clear(body: dict[str, Any]) -> dict[str, Any]:
    """清空回收站（全部彻底删除，不回收）。"""
    b = body or {}
    cfg = _load_config()
    project, _source = _resolve_project(cfg, b)
    out_dir = _article_dir(project)
    return _trash.purge(out_dir, None)


def api_list(query: dict[str, Any]) -> dict[str, Any]:
    """数据管理：返回真实样本列表（含完整正文，供本地预览），无数据即 total=0。"""
    cfg = _load_config()
    project, _source = _resolve_project(cfg, query)
    out_dir = _article_dir(project)
    records, pos = _records_with_pos(out_dir)
    sources = sorted({p[0] for p in pos})
    items = []
    for i, r in enumerate(records):
        review = r.get("review")
        score = review.get("score", "") if isinstance(review, dict) else ""
        title = str(r.get("title") or "")
        items.append({
            "id": _collect_date(r, r.get("id", i)),
            "src": pos[i][0],          # 来源文件名（删除时按它对位）
            "line": pos[i][1],         # 该文件里的物理行号（1 起）
            "key": pos[i][2],          # 该行内容指纹（删除时校验，防止行号过期误删）
            "title": title,
            "title_short": title[:TITLE_LIMIT],
            "theme": str(r.get("theme") or ""),
            "score": score,
            "chars": len(str(r.get("article") or "")),
            "article": str(r.get("article") or ""),
        })
    return {
        "dir": out_dir,
        "dir_exists": os.path.isdir(out_dir),
        "total": len(items),
        "items": items,
        "files": _file_rows(out_dir),
        "sources": sources,
        "count_source": "lineparse (规则同 core.storage.DatasetStore.import_jsonl)",
    }


def collect_status(query: dict[str, Any]) -> dict[str, Any]:
    """采集任务实时状态：运行态 + 增量事件（游标）+ 输出目录真实统计。刷新 / 轮询用。

    ?since=<游标> 取宿主任务自该游标起的新增事件（返回的 next 作新游标）。
    """
    try:
        since = int(float(str(query.get("since") or "0") or "0"))
    except (TypeError, ValueError):
        since = 0

    cfg = _load_config()
    project, source = _resolve_project(cfg, query)
    out_dir = _article_dir(project)

    out: dict[str, Any] = {
        "ok": True,
        "project": project,
        "project_source": source,
        "output_dir": out_dir,
        "output_dir_exists": os.path.isdir(out_dir),
        "output": _dir_stats(out_dir),
        "exists": False,
        "running": False,
        "events": [],
        "next": 0,
        "total_events": 0,
        "elapsed": None,
        "error": "",
    }
    try:
        st = _host.task_status(TASK_NAME, since)
        out.update(st)
    except Exception as exc:                       # noqa: BLE001  宿主异常不拖垮只读状态
        out["host_error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


# ---------------------------------------------------------------- POST：真动作

def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ---- 模型配置：保存 / 测试连通 ----

def models_save(body: dict[str, Any]) -> dict[str, Any]:
    """保存模型配置：只改 config.json#article.<role> 的
    base_url / model / temperature / max_tokens / system_prompt；**不传 api_key 即保留原值**。

    body 形状（二选一）：
      {"models": {"titler": {...}, "writer": {...}, "reviewer": {...}}}
      {"role": "titler", "profile": {...}}   （或 {"model": "titler", "profile": {...}}）
    """
    models = body.get("models")
    if not isinstance(models, dict):
        role = str(body.get("role") or body.get("model") or "").strip()
        prof = body.get("profile")
        if role in ROLE_KEYS and isinstance(prof, dict):
            models = {role: prof}
        else:
            models = {k: v for k, v in body.items() if k in ROLE_KEYS and isinstance(v, dict)}
    if not models:
        return {"ok": False, "error": "没有可保存的模型配置（缺少 models / role+profile）。"}

    try:
        cfg = _host.load_cfg()
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": "读取配置失败：%s: %s" % (type(exc).__name__, exc)}

    section = cfg.get(KIND)
    if not isinstance(section, dict):
        section = {}
        cfg[KIND] = section

    saved: dict[str, Any] = {}
    for role, prof in models.items():
        if role not in ROLE_KEYS or not isinstance(prof, dict):
            continue
        tgt = section.get(role)
        if not isinstance(tgt, dict):
            tgt = {}
            section[role] = tgt
        for name in ("base_url", "model", "system_prompt"):
            if name in prof and prof[name] is not None:
                tgt[name] = str(prof[name]).strip()
        if "temperature" in prof and prof["temperature"] not in (None, ""):
            try:
                tgt["temperature"] = float(prof["temperature"])
            except (TypeError, ValueError):
                tgt["temperature"] = prof["temperature"]
        if "max_tokens" in prof and prof["max_tokens"] not in (None, ""):
            try:
                tgt["max_tokens"] = int(float(prof["max_tokens"]))
            except (TypeError, ValueError):
                tgt["max_tokens"] = prof["max_tokens"]
        # api_key：只有显式传入非空才覆盖，否则保留 config.json 里的原值
        if "api_key" in prof and str(prof.get("api_key") or "").strip():
            tgt["api_key"] = str(prof["api_key"]).strip()
        saved[role] = {k: tgt.get(k) for k in
                       ("base_url", "model", "temperature", "max_tokens", "system_prompt")}

    try:
        _host.save_cfg(cfg)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": "写入 config.json 失败：%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "config_path": _host.CONFIG_PATH, "config_exists": os.path.isfile(_host.CONFIG_PATH),
            "saved": saved}


def models_test(body: dict[str, Any]) -> dict[str, Any]:
    """测试连通：真发一次最小请求（max_tokens=16），成功 / 失败原文返回。

    body：{"role": "titler", "base_url"?, "model"?, "api_key"?}（可选覆盖当前 config 的值；
    api_key 只在显式传入非空时覆盖，与保存同口径）。**不写任何文件。**
    """
    role = str(body.get("role") or body.get("model") or "").strip()
    if role not in ROLE_KEYS:
        return {"ok": False, "error": "未知模型角色：%r（可选 %s）" % (role, "/".join(ROLE_KEYS))}

    try:
        _host.ensure_ide_path()
        from core.client import ChatClient  # type: ignore
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "role": role, "error": "导入 ChatClient 失败：%s: %s" % (type(exc).__name__, exc)}

    try:
        cfg = _host.load_cfg()
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "role": role, "error": "读取配置失败：%s: %s" % (type(exc).__name__, exc)}
    prof = dict(((cfg.get(KIND) or {}).get(role) or {}))
    for k in ("base_url", "model", "api_key"):
        v = body.get(k)
        if v not in (None, ""):
            prof[k] = v

    try:
        client = ChatClient.from_profile(prof, timeout=10)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "role": role, "error": "模型配置无效：%s: %s" % (type(exc).__name__, exc)}
    if not client.base_url:
        return {"ok": False, "role": role, "error": "base_url 未配置"}
    if not client.model:
        return {"ok": False, "role": role, "error": "model 未配置"}

    try:
        reply = client.chat(
            [{"role": "user", "content": "请只回复两个字：在线"}],
            max_tokens=16, temperature=0,
        )
        return {"ok": True, "role": role, "base_url": client.base_url, "model": client.model,
                "max_tokens": 16, "reply": reply}
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "role": role, "base_url": client.base_url, "model": client.model,
                "max_tokens": 16, "error": "%s: %s" % (type(exc).__name__, exc)}


# ---- 采集：start / stop（真跑 core.article_collector.Collector） ----

def collect_start(body: dict[str, Any]) -> dict[str, Any]:
    """开始采集：真起宿主线程作业跑 core.article_collector.Collector。

    参数一律从 body 覆盖（themes / count / concurrency / min_words / max_words /
    dataset_name / discard_on_reject / save_rejected），**不写 config.json**；
    三张模型 profile（api_key / base_url / model / 温度 / max_tokens）用 config.json#article 的当前值。
    on_event → emit（log / progress / record / started / finished / error / done）。
    """
    try:
        _host.ensure_ide_path()
        from core.article_collector import Collector  # type: ignore
        from core.client import ChatClient            # type: ignore
        from core.storage import DatasetStore         # type: ignore
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": "导入引擎失败：%s: %s" % (type(exc).__name__, exc)}

    busy = _host.running_task(TASK_GROUP)
    if busy:
        return {"ok": False, "error": "已有采集任务在运行（%s），请先停止。" % busy}

    try:
        cfg = _host.load_cfg()
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": "读取配置失败：%s: %s" % (type(exc).__name__, exc)}

    section = cfg.get(KIND)
    if not isinstance(section, dict):
        section = {}
    coll = section.get("collection")
    if not isinstance(coll, dict):
        coll = {}

    # ---- 参数：body 覆盖 > config.json#article.collection ----
    themes = str(body.get("themes", coll.get("themes", "")) or "")
    count = max(1, _as_int(body.get("count", coll.get("count", 1)), 1))
    concurrency = min(5, max(1, _as_int(body.get("concurrency", coll.get("concurrency", 1)), 1)))   # 并发上限 5（用户 2026-10-14）
    min_words = max(0, _as_int(body.get("min_words", coll.get("min_words", 0)), 0))
    max_words = max(min_words, _as_int(body.get("max_words", coll.get("max_words", 0)), 0))
    dataset_name = str(body.get("dataset_name", coll.get("dataset_name", "article_set")) or "").strip() or "article_set"
    discard = _as_bool(body.get("discard_on_reject", coll.get("discard_on_reject", True)), True)
    save_rej = _as_bool(body.get("save_rejected", coll.get("save_rejected", False)), False)
    # 训练用途（用户 2026-10-14，数据集名称右侧下拉）：预训练 / 继续预训练
    stage = str(body.get("stage", coll.get("stage", "pretrain")) or "pretrain")
    if stage not in ("pretrain", "continue"):
        stage = "pretrain"

    # ---- 模型客户端（不修改配置，只读当前值） ----
    try:
        titler = ChatClient.from_profile(section.get("titler") or {})
        writer = ChatClient.from_profile(section.get("writer") or {})
        reviewer = ChatClient.from_profile(section.get("reviewer") or {})
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": "模型配置无效：%s: %s" % (type(exc).__name__, exc)}

    proj = _host.project(cfg)
    out_dir = os.path.join(proj.root, "data", KIND)
    os.makedirs(out_dir, exist_ok=True)

    try:
        store = DatasetStore(out_dir, dataset_name, save_rejected=save_rej, kind=KIND)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": "创建数据集失败：%s: %s" % (type(exc).__name__, exc)}
    # 总览日志行要显示这条产物的训练方式（语料 · 预训练/继续预训练 · 文件名）
    _host.record_collect_use(proj.root, KIND, store.main_path, stage)

    holder: dict[str, Any] = {}

    def do_stop() -> None:
        c = holder.get("collector")
        if c is not None:
            c.stop()

    def run(emit, _task, _group) -> None:
        emit("log", {"text": "[宿主] 采集任务启动 · 输出 %s" % store.main_path})
        emit("log", {"text": "[宿主] 主题=%s · 数量=%d · 并发=%d · 字数 %d-%d · 丢弃未过=%s · 训练方式=%s"
                          % (themes.replace("\n", " / ") or "（自由主题）", count, concurrency,
                             min_words, max_words, discard,
                             "预训练" if stage == "pretrain" else "继续预训练")})
        collector = Collector(
            titler, writer, reviewer, store,
            themes=themes,
            count=count,
            concurrency=concurrency,
            min_words=min_words,
            max_words=max_words,
            discard_on_reject=discard,
            titler_system=str((section.get("titler") or {}).get("system_prompt") or ""),
            writer_system=str((section.get("writer") or {}).get("system_prompt") or ""),
            reviewer_system=str((section.get("reviewer") or {}).get("system_prompt") or ""),
            lang=str(body.get("lang") or (coll.get("lang") if isinstance(coll, dict) else "") or "zh"),
            on_event=lambda kind, payload: emit(kind, payload or {}),
        )
        holder["collector"] = collector
        stats = collector.run()
        emit("log", {"text": "[宿主] 采集线程结束：入库 %d/%d · 丢弃 %d · 失败 %d"
                          % (stats.done, stats.total, stats.discarded, stats.failed)})

    try:
        _host.start_task(TASK_NAME, run, stop=do_stop, group=TASK_GROUP)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": "启动采集失败：%s: %s" % (type(exc).__name__, exc)}

    return {
        "ok": True,
        "job": TASK_NAME,
        "project": proj.root,
        "output_dir": out_dir,
        "dataset_path": store.main_path,
        "themes": themes,
        "count": count,
        "concurrency": concurrency,
        "min_words": min_words,
        "max_words": max_words,
        "dataset_name": dataset_name,
        "discard_on_reject": discard,
        "save_rejected": save_rej,
        "stage": stage,
        "mode": "stream",   # 采集器本体即流式流水线（无批量路径），口径固定流式（用户 2026-10-14）
        "models": {"titler": titler.model, "writer": writer.model, "reviewer": reviewer.model},
        "note": "参数来自本次请求 body，未写入 config.json。",
    }


def collect_stop(_body: dict[str, Any]) -> dict[str, Any]:
    """停止采集：调用 Collector.stop()（已在途样本写完再退出）。"""
    try:
        return _host.stop_task(TASK_NAME)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


# ---- 其它写操作：本轮不执行（会写工程文件） ----

def _not_implemented(reason: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def fn(_payload: dict[str, Any]) -> dict[str, Any]:
        return {"not_implemented": True, "reason": reason}
    return fn


def _sample_source_dir(out_dir: str) -> str:
    """该板块样本文件所在目录（口径同 api_list：优先 .meta，但 .meta 里没有 jsonl 就用顶层）。

    文章线的完整记录在 <out>/.meta/*.jsonl；响应线的记录直接在 <out>/*.jsonl（.meta 只放 sft 的 txt）。
    """
    meta_dir = os.path.join(out_dir, ".meta")
    if os.path.isdir(meta_dir):
        try:
            if any(f.endswith(".jsonl") and "rejected" not in f for f in os.listdir(meta_dir)):
                return meta_dir
        except OSError:
            pass
    return out_dir


def _all_sample_files(out_dir: str) -> list[str]:
    """该板块**全部**样本文件（*.jsonl、排除 *rejected*），按文件名升序（= 采集先后顺序）。"""
    if not out_dir or not os.path.isdir(out_dir):
        return []
    src_dir = _sample_source_dir(out_dir)
    try:
        names = [f for f in os.listdir(src_dir)
                 if f.endswith(".jsonl") and "rejected" not in f]
    except OSError:
        return []
    names.sort()
    return [os.path.join(src_dir, f) for f in names]


def export_dataset(body: dict[str, Any]) -> dict[str, Any]:
    """导出数据集：只导出**选中**的样本，写到用户选定的路径。

    用户口径（2026-10-14）：没选中不能触发导出；导出只导出选中的数据。选中集 = body['targets']
    （[{src, line, key}]，与删除同一套寻址）：按 (来源文件, 物理行号) 取原始行并校验行指纹，
    逐行原样拼（跳过空行、不去重、不改字段），行尾 CRLF。目标路径由前端「另存为」给出（body['path']）。
    """
    b = body or {}
    target = str(b.get("path") or "").strip()
    if not target:
        return {"ok": False, "error": "缺少导出路径（应由「另存为」对话框给出）"}
    if os.path.isdir(target):
        return {"ok": False, "error": "导出路径指向一个目录：%s" % target}
    if not (b.get("targets") or []):
        return {"ok": False, "error": "没有选中要导出的样本（导出只导出选中的数据）"}

    cfg = _load_config()
    project, source = _resolve_project(cfg, b)
    out_dir = _article_dir(project)
    src_dir = _sample_source_dir(out_dir)
    want = _target_map(b, src_dir)
    if not want:
        return {"ok": False, "error": "选中的样本已经不存在（列表可能刚变过，请刷新列表）",
                "dir": out_dir, "files": 0, "records": 0}

    lines: list[str] = []
    used: list[str] = []
    stale = 0
    for name in sorted(want):
        sel = want[name]
        got = 0
        try:
            with open(os.path.join(src_dir, name), "r", encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if i not in sel:
                        continue
                    raw = line.strip()
                    if not raw:
                        continue
                    key = sel[i]
                    if key and _line_key(raw) != key:
                        stale += 1      # 指纹对不上（列表刚变过）→ 不导出这条
                        continue
                    lines.append(raw)
                    got += 1
        except OSError:
            stale += len(sel)
            continue
        if got:
            used.append(name)
    if not lines:
        return {"ok": False,
                "error": ("选中的样本已经不存在（列表可能刚变过，请刷新列表）" if stale
                          else "选中的样本里没有可导出的记录"),
                "dir": out_dir, "files": 0, "records": 0}

    target = os.path.abspath(target)
    try:
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        tmp = target + ".eai-tmp"
        with open(tmp, "w", encoding="utf-8", newline="\r\n") as f:
            for raw in lines:
                f.write(raw + "\n")
        os.replace(tmp, target)
    except OSError as exc:
        return {"ok": False, "error": "写文件失败：%s" % exc}

    return {
        "ok": True,
        "path": target,
        "dir": out_dir,
        "project": project,
        "project_source": source,
        "files": len(used),
        "sources": used,
        "records": len(lines),
        "bytes": os.path.getsize(target),
        "note": "只导出选中的样本；逐行原样、未去重、字段未改。",
    }


def register(router) -> None:
    router.get("/api/article/config", api_config)
    router.get("/api/article/collection", api_collection)
    router.get("/api/article/list", api_list)
    router.get("/api/article/file_text", api_file_text)
    router.get("/api/article/collect/status", collect_status)

    router.post("/api/article/file_state", file_state)
    router.post("/api/article/models/save", models_save)
    router.post("/api/article/models/test", models_test)
    router.post("/api/article/collection", api_save_collection)
    router.post("/api/article/collect/start", collect_start)
    router.post("/api/article/collect/stop", collect_stop)

    # 兼容旧路径（原页面用名），行为与新路径一致
    router.post("/api/article/config/save", models_save)
    router.post("/api/article/test-connection", models_test)

    router.post("/api/article/export", export_dataset)
    router.post("/api/article/merge", merge_datasets)
    router.post("/api/article/delete", delete_records)

    # 回收站：删除所选 → 进回收站（可恢复）
    router.get("/api/article/trash", trash_list)
    router.post("/api/article/trash/restore", trash_restore)
    router.post("/api/article/trash/purge", trash_purge)
    router.post("/api/article/trash/clear", trash_clear)
