"""bridge_api/response.py —— 「响应蒸馏」页（导航键 response）的真实数据 / 动作接口。

对应 Tk 页 <项目目录>\\（Tk 版已删除）distill.py 的 DistillPage(kind="response")：
三个标签页 = 模型配置 / 采集中心 / 数据管理。

真实数据来源：
  * 只读：EAI_Studio/engine/config.json 的 `response` 段 + 当前工程（config.app.recent_projects[0]）
    的 data/response 目录下的 *.jsonl。**绝不编造**任何样本 / 计数 / 配置值。
  * 真动作（复用 EAI_Studio/engine 自己的引擎，见 bridge_api/_host.py）：
      - 采集：进程内线程托管 core.response_collector.Collector（出题→答题→审阅→落盘），
              落盘用 core.storage.DatasetStore 写到工程 data/response；支持 stop()。
      - 模型配置保存：只改 config.json 的 response.<role>，api_key 不传明文则保留原值。
      - 测试连通：用 core.client.ChatClient 真发一次最小请求（max_tokens<=16）。

样本计数与解析规则与 core/storage.py 的 DatasetStore.import_jsonl 完全一致：
逐行 json.loads，只收录「非空且是 dict，且含 article / question / title 之一」的行；
排除文件名含 "rejected" 的 *_rejected.jsonl。

接口：
  GET  /api/response/config                     模型卡（真实 config.json，api_key 不下发明文）
  GET  /api/response/collection                 采集参数 + 输出目录
  GET  /api/response/list                        数据管理：真实样本列表
  GET  /api/response/collect/status?since=N      采集运行态 + 增量事件 + 工程真实统计
  POST /api/response/collect/start               起真采集线程（body 覆盖表单值，不写 config.json）
  POST /api/response/collect/stop                停止采集
  POST /api/response/models/save                 保存模型配置（只改 response.<role>）
  POST /api/response/models/test                 真发一次最小请求测连通
  POST /api/response/export  导出数据集：本工程该板块多个样本文件合并成一个文件（路径由用户「另存为」给出）
  POST /api/response/delete  删除所选样本（按来源文件+行号精确删行，未选中的行原样保留）
  （导入已取消：数据管理只能导出、不能导入 —— 用户 2026-10-13）
"""
from __future__ import annotations

import hashlib
import json
import os
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
KIND = "response"

# 采集线程作业的宿主名 / 互斥组（同名只留最近一个；组内互斥由本模块自查）
TASK_NAME = "collect:response"
GROUP = "response_collect"

# 与 distill.py 的 RESPONSE_MODELS 逐字对齐：(key, 卡片标题, 顶色条 tone)
RESPONSE_MODELS = (
    ("generator", "出题模型", "accent"),
    ("responder", "答题模型", "ink3"),
    ("reviewer", "审阅模型", "ink3"),
)

MODEL_KEYS = tuple(m[0] for m in RESPONSE_MODELS)
# 允许保存 / 覆盖的配置字段（api_key 单独处理：不传明文则保留原值）
MODEL_FIELDS = ("base_url", "model", "temperature", "max_tokens", "system_prompt")

# 与 distill.py _fill_topics(kind="response") 同一组示例主题（本地 UI 便捷填充，非样本数据）
# 示例主题（用户 2026-10-18）：同 article；问答页也保证只有唐朝一条文化类
EXAMPLE_TOPICS = "唐朝历史\n机器学习基础\n数据库原理\n线性代数\n计算机网络基础"

# 数据管理表格：Tk 版问题截断到 50 字
QUESTION_LIMIT = 50

# 模型请求超时：推理模型（如 mimo-v2.6-pro）偶发长时间思考，默认 90s 会被硬掐断，
# 给采集客户端更宽裕的上限（只影响本页采集，不改 core.client 默认值）。
CLIENT_TIMEOUT = 180.0


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


def _response_dir(project: str) -> str:
    """工程内的固定响应样本目录（与 Tk 版 proj.response_dir 一致：<工程>/data/response）。"""
    return os.path.join(project, "data", KIND)


def _project_paths() -> tuple[str, str]:
    """用引擎宿主解析当前工程：返回 (工程根, 响应样本目录)。目录树会被确保存在。"""
    proj = _host.project()
    root = proj.root
    out_dir = getattr(proj, "response_dir", "") or os.path.join(root, "data", KIND)
    return root, out_dir


# ---------------------------------------------------------------- 轻量类型转换

def _to_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "是", "开"):
        return True
    if s in ("0", "false", "no", "off", "否", "关", ""):
        return False
    return default


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
    """扫描响应目录：按 mtime 倒序取最近 3 个主 JSONL（排除 *rejected*），合并记录。

    与 Tk 版 DistillPage._refresh_data 同规则，返回 (记录列表, 来源文件名列表)。
    """
    if not out_dir or not os.path.isdir(out_dir):
        return [], []
    files = [
        os.path.join(out_dir, f)
        for f in os.listdir(out_dir)
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


def _count_lines(path: str) -> int:
    """只数换行，不解析 JSON（用户 2026-10-19：蒸馏跑久后每秒全量解析输出目录会卡）。"""
    try:
        n = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                n += chunk.count(b"\n")
        return n
    except OSError:
        return 0


_LINE_CACHE: dict[tuple, int] = {}          # (路径, 大小, mtime) → 行数，文件不变就不重数
_LIST_CACHE: dict[str, Any] = {"key": None, "at": 0.0, "items": []}


def _list_jsonl(out_dir: str, ttl: float = 5.0) -> list[dict[str, Any]]:
    """列出输出目录下所有主 JSONL 的真实行数（排除 *rejected*），按 mtime 倒序。

    采集中心每秒轮询一次，这里做两级缓存：
      · 单文件行数按 (路径, 大小, mtime) 缓存，未变就不重数；
      · 整个目录列表 5 秒内直接返回上次结果（用户 2026-10-19：数据越多越卡）。
    """
    if not out_dir or not os.path.isdir(out_dir):
        return []
    now = time.time()
    if _LIST_CACHE["key"] == out_dir and (now - float(_LIST_CACHE["at"] or 0)) < ttl:
        return _LIST_CACHE["items"]
    items: list[dict[str, Any]] = []
    try:
        names = [f for f in os.listdir(out_dir) if f.endswith(".jsonl") and "rejected" not in f]
    except OSError:
        return []
    for name in names:
        path = os.path.join(out_dir, name)
        try:
            st = os.stat(path)
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            mtime, size = 0, 0
        key = (path, size, round(mtime, 3))
        lines = _LINE_CACHE.get(key)
        if lines is None:
            lines = _count_lines(path)
            if len(_LINE_CACHE) > 200:
                _LINE_CACHE.clear()
            _LINE_CACHE[key] = lines
        items.append({
            "name": name,
            "path": path,
            "lines": lines,
            "size_bytes": size,
            "mtime": mtime,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    _LIST_CACHE.update(key=out_dir, at=now, items=items)
    return items


# ---------------------------------------------------------------- GET 处理器

def api_config(query: dict[str, Any]) -> dict[str, Any]:
    """模型配置：返回真实 config.json 中 response 段的三张模型卡。

    api_key 出于安全不以明文下发：给 api_key_set 布尔 + 按真实长度生成的掩码。
    """
    cfg = _load_config()
    project, source = _resolve_project(cfg, query)
    section = cfg.get(KIND) or {}
    models = []
    for key, label, tone in RESPONSE_MODELS:
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
        "defaults": _host.model_defaults(KIND, [m[0] for m in RESPONSE_MODELS]),
    }


def api_collection(query: dict[str, Any]) -> dict[str, Any]:
    """采集参数：返回真实 config.json 中 response.collection 段 + 解析出的输出目录。

    response 与 article 的采集字段不同：没有 min_words / max_words，
    但多出 finetune（微调方式 sft|lora）与 auto_topic（自动出题）。
    「模式」已删（用户 2026-10-14）：固定流式，mode 只作内部字段恒为 stream。
    """
    cfg = _load_config()
    project, source = _resolve_project(cfg, query)
    coll = ((cfg.get(KIND) or {}).get("collection") or {})
    if not isinstance(coll, dict):
        coll = {}
    out_dir = _response_dir(project)
    mode = "stream"                       # 删掉模式选择后固定流式
    finetune = str(coll.get("finetune") or "sft")
    if finetune not in ("sft", "lora"):
        finetune = "sft"
    fields = {
        "topics": coll.get("topics", ""),
        "count": coll.get("count", 50),
        "concurrency": coll.get("concurrency", 2),
        "min_words": coll.get("min_words", 20),
        "max_words": coll.get("max_words", 200),
        "dataset_name": coll.get("dataset_name", "response_set"),
        "discard_on_reject": bool(coll.get("discard_on_reject", True)),
        "save_rejected": bool(coll.get("save_rejected", False)),
        "mode": mode,
        "finetune": finetune,
        "auto_topic": bool(coll.get("auto_topic", True)),
    }
    # 用户 2026-10-19：本工程上次填过的表单优先（采集页不回写 config，删页重挂就丢）
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
        "example_topics": EXAMPLE_TOPICS,
        "fields": fields,
    }


def api_save_collection(body: dict[str, Any]) -> dict[str, Any]:
    """保存采集表单（用户 2026-10-19）：切页 / 切工程 / 重开应用回来，已填内容要还在。"""
    cfg = _load_config()
    project, _src = _resolve_project(cfg, body)
    keys = ("topics", "count", "concurrency", "min_words", "max_words", "dataset_name",
            "discard_on_reject", "save_rejected", "finetune", "auto_topic", "mode")
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
    out_dir = _response_dir(project)
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
    """数据管理「文件视图」的行：输出目录下的产物 *.jsonl（问答线主产物就是记录本体）。

    meta = 记录里 src 的文件名（响应线即本文件名），前端按它把逐条对位到文件。
    """
    rows: list[dict[str, Any]] = []
    if not out_dir or not os.path.isdir(out_dir):
        return rows
    state = _file_state_map(out_dir)
    for fn in sorted(os.listdir(out_dir)):
        if not fn.endswith(".jsonl") or "rejected" in fn.lower():
            continue
        p = os.path.join(out_dir, fn)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        rows.append({
            "name": fn,
            "stem": fn[:-6] if fn.endswith(".jsonl") else fn,
            "meta": fn,
            "meta_path": "",
            "path": p,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "enabled": bool((state.get(fn) or {}).get("enabled", True)),
        })
    rows.sort(key=lambda r: r.get("mtime") or 0, reverse=True)
    return rows


def api_file_text(query: dict[str, Any]) -> dict[str, Any]:
    """单个产物文件的全文（文件视图单击行 → 下方栏显示整个文件）。只认输出目录下的裸文件名，防目录穿越。"""
    cfg = _load_config()
    project, _source = _resolve_project(cfg, query)
    out_dir = _response_dir(project)
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


def _sync_response_sft(out_dir: str, name: str, force: bool = False) -> None:
    # force=True（回收站恢复时用）：派生文本不存在也要重建；默认只在已存在时更新。
    """问答线：按剩下的记录重建 .meta/<stem>_sft.txt（\\x01问\\x02答\\x03 每行一条）；没记录了就删掉。"""
    src_path = os.path.join(_sample_source_dir(out_dir), name)
    sft_path = os.path.join(out_dir, ".meta", name[:-len(".jsonl")] + "_sft.txt")
    if not force and not os.path.exists(sft_path):
        return
    try:
        from core.storage import pair_to_txt             # 惰性导入，别让桥启动就带上引擎
    except Exception:
        return
    recs = [r for r in _read_records(src_path) if r.get("question") and r.get("answer")]
    body = "".join(pair_to_txt(r) + "\n" for r in recs)
    if not body.strip():
        try:
            os.remove(sft_path)
        except OSError:
            pass
        return
    os.makedirs(os.path.dirname(sft_path), exist_ok=True)
    tmp = sft_path + ".eai-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    os.replace(tmp, sft_path)

def merge_datasets(body: dict[str, Any]) -> dict[str, Any]:
    """数据集合并（用户 2026-10-15）：把选中的多个数据集 jsonl 合成一个新的数据集（原文件保留）。

    逐行原样拼接（行键不变），派生 .meta/<stem>_sft.txt 走 _sync_response_sft；
    同名直接拒绝（与仓库权重同口径）。"""
    b = body or {}
    cfg = _load_config()
    project, _source = _resolve_project(cfg, b)
    out_dir = _response_dir(project)
    names: list[str] = []
    for n in (b.get("names") or []):
        nm = os.path.basename(str(n).strip())
        if nm and nm not in names:
            names.append(nm)
    if len(names) < 2:
        return {"ok": False, "error": "至少选两个数据集才能合并"}
    raw_name = os.path.basename(str(b.get("name") or "").strip())
    stem = raw_name[:-6] if raw_name.lower().endswith(".jsonl") else raw_name
    if not stem:
        return {"ok": False, "error": "缺少合并后的数据集名称"}
    if any(ch in stem for ch in '\\/:*?"<>|'):
        return {"ok": False, "error": "名称含非法字符"}
    out_path = os.path.join(out_dir, stem + ".jsonl")
    if os.path.exists(out_path):
        return {"ok": False, "error": "%s：已存在同名数据集，不能合并" % stem}
    merged: list[str] = []
    used = 0
    for fn in names:
        p = os.path.join(out_dir, fn)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        merged.append(line.rstrip("\r\n"))
        except OSError:
            continue
        used += 1
    if used < 2:
        return {"ok": False, "error": "选中的数据集文件已不存在，请刷新列表"}
    if not merged:
        return {"ok": False, "error": "选中的数据集里没有可合并的内容"}
    os.makedirs(out_dir, exist_ok=True)
    tmp = out_path + ".eai-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(merged) + "\n")
    os.replace(tmp, out_path)
    _sync_response_sft(out_dir, stem + ".jsonl", force=True)
    return {
        "ok": True,
        "name": stem + ".jsonl",
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
    out_dir = _response_dir(project)
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
            _sync_response_sft(out_dir, name)
    if not removed:
        return {"ok": False,
                "error": "这些样本已经不存在（列表可能刚变过，请刷新列表）" if stale else "没有可删除的样本"}
    return {"ok": True, "removed": removed, "stale": stale, "trashed": trashed, "files": touched,
            "dir": out_dir, "project": project}


def trash_list(query: dict[str, Any]) -> dict[str, Any]:
    """回收站列表（最新删除的在前）。key = 回收站条目 id（恢复 / 彻底删除按它选）。"""
    cfg = _load_config()
    project, _source = _resolve_project(cfg, query)
    out_dir = _response_dir(project)
    items = []
    for e in _trash.items(out_dir):
        rec_any = e.get("rec")
        rec: dict[str, Any] = rec_any if isinstance(rec_any, dict) else {}
        question = str(rec.get("question") or "")
        answer = str(rec.get("answer") or "")
        items.append({
            "key": str(e.get("tid") or ""),
            "id": _collect_date(rec, rec.get("id", "")),
            "question": question,
            "question_short": question[:QUESTION_LIMIT],
            "topic": str(rec.get("topic") or ""),
            "theme": str(rec.get("theme") or ""),   # 单条弹窗的主题行要用（2026-10-21）
            "deleted_at": str(e.get("deleted_at") or ""),
            "src": str(e.get("src") or ""),
            "answer": answer,
        })
    return {"ok": True, "total": len(items), "items": items,
            "dir": _trash.trash_dir(out_dir), "project": project}


def _trash_sync(out_dir: str, name: str) -> None:
    _sync_response_sft(out_dir, name, force=True)


def trash_restore(body: dict[str, Any]) -> dict[str, Any]:
    """恢复回收站里选中的样本：原始行按原字节写回来源文件（文件没了就重建）+ 重建派生文本。"""
    b = body or {}
    cfg = _load_config()
    project, _source = _resolve_project(cfg, b)
    out_dir = _response_dir(project)
    src_dir = _sample_source_dir(out_dir)
    return _trash.restore(out_dir, b.get("keys"), src_dir, _trash_sync)


def trash_purge(body: dict[str, Any]) -> dict[str, Any]:
    """彻底删除回收站里选中的条目（永久删，不再留档）。"""
    b = body or {}
    cfg = _load_config()
    project, _source = _resolve_project(cfg, b)
    out_dir = _response_dir(project)
    return _trash.purge(out_dir, b.get("keys"))


def trash_clear(body: dict[str, Any]) -> dict[str, Any]:
    """清空回收站（全部彻底删除，不回收）。"""
    b = body or {}
    cfg = _load_config()
    project, _source = _resolve_project(cfg, b)
    out_dir = _response_dir(project)
    return _trash.purge(out_dir, None)


def api_list(query: dict[str, Any]) -> dict[str, Any]:
    """数据管理：返回真实样本列表（含完整问答，供本地预览），无数据即 total=0。"""
    cfg = _load_config()
    project, _source = _resolve_project(cfg, query)
    out_dir = _response_dir(project)
    records, pos = _records_with_pos(out_dir)
    sources = sorted({p[0] for p in pos})
    items = []
    for i, r in enumerate(records):
        review = r.get("review")
        score = review.get("score", "") if isinstance(review, dict) else ""
        question = str(r.get("question") or "")
        answer = str(r.get("answer") or "")
        items.append({
            "id": _collect_date(r, r.get("id", i)),
            "src": pos[i][0],          # 来源文件名（删除时按它对位）
            "line": pos[i][1],         # 该文件里的物理行号（1 起）
            "key": pos[i][2],          # 该行内容指纹（删除时校验，防止行号过期误删）
            "question": question,
            "question_short": question[:QUESTION_LIMIT],
            "topic": str(r.get("topic") or ""),
            "score": score,
            "chars": len(answer),
            "answer": answer,
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
    """采集运行态：宿主线程作业的增量事件 + 当前工程 / 输出目录真实统计。

    ?since=<游标> 取自该游标起的新增事件（每个事件 {kind, payload, t}），返回的 next 作新游标。
    """
    try:
        since = int(float(str((query or {}).get("since") or "0") or "0"))
    except (TypeError, ValueError):
        since = 0

    try:
        st = _host.task_status(TASK_NAME, since)
    except Exception as exc:                       # noqa: BLE001
        st = {"ok": False, "exists": False, "running": False, "events": [], "next": 0,
              "error": "%s: %s" % (type(exc).__name__, exc)}

    try:
        root, out_dir = _project_paths()
        files = _list_jsonl(out_dir)
        samples = sum(f["lines"] for f in files)
        dir_error = ""
    except Exception as exc:                        # noqa: BLE001
        root, out_dir, files, samples = "", "", [], 0
        dir_error = "%s: %s" % (type(exc).__name__, exc)

    return {
        "ok": True,
        "task": TASK_NAME,
        "exists": bool(st.get("exists")),
        "running": bool(st.get("running")),
        "events": st.get("events") or [],
        "next": st.get("next") or 0,
        "total_events": st.get("total_events") or 0,
        "stats": st.get("stats") or {},
        "started_at": st.get("started_at"),
        "finished_at": st.get("finished_at"),
        "elapsed": st.get("elapsed"),
        "result": st.get("result"),
        "error": st.get("error") or "",
        "project": root,
        "output_dir": out_dir,
        "output_dir_exists": bool(out_dir) and os.path.isdir(out_dir),
        "samples": samples,
        "files": files,
        "dir_error": dir_error,
    }


# ---------------------------------------------------------------- 真动作：采集（线程作业）

def _build_collector(section: dict[str, Any], body: dict[str, Any]):
    """按 config.response 的模型段 + body 覆盖的采集参数，构造 Collector 与落盘 store。

    与 Tk 版 DistillPage._start(kind="response") 同一套参数；body 里的表单值直接覆盖
    config 里的对应项（不写 config.json）。
    """
    from core.client import ChatClient                              # type: ignore
    from core.response_collector import Collector                   # type: ignore
    from core.storage import DatasetStore                           # type: ignore

    coll_cfg = section.get("collection") or {}
    if not isinstance(coll_cfg, dict):
        coll_cfg = {}

    def ov(name: str, default: Any) -> Any:
        if name in body and body[name] not in (None, ""):
            return body[name]
        if name in coll_cfg and coll_cfg[name] is not None:
            return coll_cfg[name]
        return default

    topics = str(ov("topics", "") or "")
    count = max(1, _to_int(ov("count", 1), 1))
    concurrency = min(10, max(1, _to_int(ov("concurrency", 2), 2)))   # 并发上限 10（用户 2026-10-14）
    mode = "stream"                       # 删掉模式选择后固定流式
    finetune = str(ov("finetune", "sft") or "sft")   # 微调方式：指令微调(sft) / LoRA 微调(lora)
    if finetune not in ("sft", "lora"):
        finetune = "sft"
    auto_topic = _to_bool(ov("auto_topic", True), True)
    discard = _to_bool(ov("discard_on_reject", True), True)
    save_rejected = _to_bool(ov("save_rejected", False), False)
    dataset_name = str(ov("dataset_name", "response_set") or "response_set").strip() or "response_set"
    # 答案字数区间（用户 2026-10-19）：与语料蒸馏同口径 —— 不写进提示词，只在请求时补一句长度要求
    min_words = max(0, _to_int(ov("min_words", 20), 20))
    _mx = max(0, _to_int(ov("max_words", 200), 200))
    max_words = max(min_words, _mx) if _mx else 0     # 0 = 不限（不再被抬成 min）

    gen_prof = section.get("generator") or {}
    resp_prof = section.get("responder") or {}
    rev_prof = section.get("reviewer") or {}
    generator = ChatClient.from_profile(gen_prof if isinstance(gen_prof, dict) else {}, timeout=CLIENT_TIMEOUT)
    responder = ChatClient.from_profile(resp_prof if isinstance(resp_prof, dict) else {}, timeout=CLIENT_TIMEOUT)
    reviewer = ChatClient.from_profile(rev_prof if isinstance(rev_prof, dict) else {}, timeout=CLIENT_TIMEOUT)

    root, out_dir = _project_paths()
    store = DatasetStore(out_dir, dataset_name, save_rejected=save_rejected, kind=KIND)

    collector = Collector(
        generator, responder, store,
        reviewer=reviewer,
        topics=topics,
        count=count,
        concurrency=concurrency,
        mode=mode,
        auto_topic=auto_topic,
        generator_system=str((gen_prof or {}).get("system_prompt") or ""),
        responder_system=str((resp_prof or {}).get("system_prompt") or ""),
        reviewer_system=str((rev_prof or {}).get("system_prompt") or ""),
        discard_on_reject=discard,
        min_words=min_words,
        max_words=max_words,
        lang=str(ov("lang", "zh") or "zh"),      # 界面语言（用户 2026-10-20）：决定出题/答题/审阅的用户消息语言
    )

    info = {
        "project": root,
        "output_dir": out_dir,
        "store_path": store.main_path,
        "min_words": min_words,
        "max_words": max_words,
        "dataset_name": dataset_name,
        "topics": topics,
        "topic_count": len([t for t in topics.splitlines() if t.strip()]),
        "count": count,
        "concurrency": concurrency,
        "mode": mode,
        "finetune": finetune,
        "auto_topic": auto_topic,
        "discard_on_reject": discard,
        "save_rejected": save_rejected,
        "generator_model": generator.model,
        "responder_model": responder.model,
        "reviewer_model": reviewer.model,
    }
    return collector, info


def collect_start(body: dict[str, Any]) -> dict[str, Any]:
    """开始采集：真起一条进程内线程，跑 core.response_collector.Collector.run()。

    body 里带 topics / count / concurrency / min_words / max_words / finetune / auto_topic /
    discard_on_reject / save_rejected / dataset_name 的表单值会覆盖 config 中对应项（不改 config.json）。
    「模式」已删（用户 2026-10-14）：固定流式。
    已有采集在跑时返回 {"ok": false, "error": ...}。
    """
    body = body or {}
    busy = _host.running_task(GROUP)
    if busy:
        return {"ok": False, "error": "已有采集任务在运行（%s），请先停止。" % busy}

    try:
        cfg = _host.load_cfg()
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    section = cfg.get(KIND) or {}
    if not isinstance(section, dict):
        section = {}

    try:
        collector, info = _build_collector(section, body)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    if not collector.generator.model or not collector.responder.model:
        return {"ok": False, "error": "出题 / 答题模型未配置 model，无法采集（请在「模型配置」补全）。"}

    # 总览日志行要显示这条产物的微调方式（响应 · 指令微调/LoRA 微调 · 文件名）
    _host.record_collect_use(str(info.get("project") or ""), "response",
                             str(info.get("store_path") or ""), str(info.get("finetune") or "sft"))

    def runner(emit, task, group):                  # noqa: ANN001, ARG001
        def on_event(kind: str, payload: Any) -> None:
            if kind == "progress" and isinstance(payload, dict):
                try:
                    task.set_stats(**{k: payload.get(k) for k in
                                      ("total", "done", "discarded", "failed", "inflight", "finished")})
                except Exception:                   # noqa: BLE001
                    pass
            emit(kind, payload)

        emit("log", {"text": "[桥] 启动采集：%d 条 · 并发 %d · 答案 %d-%d 字 · 微调方式 %s · 落盘 %s"
                             % (info["count"], info["concurrency"],
                                info["min_words"], info["max_words"],
                                "指令微调" if info["finetune"] == "sft" else "LoRA 微调",
                                info["store_path"])})
        collector.on_event = on_event
        stats = collector.run()
        try:
            task.result = {"total": stats.total, "done": stats.done,
                           "discarded": stats.discarded, "failed": stats.failed}
        except Exception:                           # noqa: BLE001
            pass

    try:
        _host.start_task(TASK_NAME, runner, stop=collector.stop, group=GROUP)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    return {"ok": True, "task": TASK_NAME, "info": info,
            "note": "已启动真实采集流水线（出题 → 答题 → 审阅 → 落盘工程 data/response）。"}


def collect_stop(_body: dict[str, Any]) -> dict[str, Any]:
    """停止采集：调用 Collector.stop()，已在途的样本会写完再退出。"""
    try:
        return _host.stop_task(TASK_NAME)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


# ---------------------------------------------------------------- 真动作：模型配置

def models_save(body: dict[str, Any]) -> dict[str, Any]:
    """保存模型配置：只改 config.json 的 response.<role> 的
    base_url / model / temperature / max_tokens / system_prompt。

    api_key 不传明文则保留原值（前端留空即表示不修改）；只认非空且非掩码的明文。
    """
    body = body or {}
    saved_roles: list[str] = []
    try:
        cfg = _host.load_cfg()
        section = cfg.get(KIND)
        if not isinstance(section, dict):
            section = {}
            cfg[KIND] = section
        for key in MODEL_KEYS:
            incoming = body.get(key)
            if not isinstance(incoming, dict):
                continue
            prof = section.get(key)
            if not isinstance(prof, dict):
                prof = {}
                section[key] = prof
            for field in MODEL_FIELDS:
                if field not in incoming:
                    continue
                val = incoming[field]
                if field == "temperature":
                    try:
                        prof[field] = float(val)
                    except (TypeError, ValueError):
                        prof[field] = val
                elif field == "max_tokens":
                    try:
                        prof[field] = int(float(val))
                    except (TypeError, ValueError):
                        prof[field] = val
                else:
                    prof[field] = str(val if val is not None else "")
            api_key = incoming.get("api_key")
            if isinstance(api_key, str) and api_key.strip() and (set(api_key) - set("\u2022")):
                prof["api_key"] = api_key.strip()   # 只覆盖真实明文
            saved_roles.append(key)
        _host.save_cfg(cfg)
        return {"ok": True, "config_path": _host.CONFIG_PATH, "saved_roles": saved_roles}
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def models_test(body: dict[str, Any]) -> dict[str, Any]:
    """测试连通：用 core.client.ChatClient 真发一次最小请求（max_tokens<=16）。

    body.role ∈ generator/responder/reviewer；body.profile 可带表单里未保存的值覆盖。
    成功 / 失败都把真实结果（含错误原文）返回，由页面照实显示。
    """
    body = body or {}
    role = str(body.get("role") or body.get("model") or "").strip()
    if role not in MODEL_KEYS:
        return {"ok": False, "error": "未知模型角色：%s（应为 %s）"
                                       % (role or "(空)", "/".join(MODEL_KEYS))}

    try:
        cfg = _host.load_cfg()
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "role": role, "error": "%s: %s" % (type(exc).__name__, exc)}

    section = cfg.get(KIND) or {}
    prof = section.get(role) or {}
    prof = dict(prof) if isinstance(prof, dict) else {}

    override = body.get("profile")
    if isinstance(override, dict):
        for k in ("base_url", "model"):
            if str(override.get(k) or "").strip():
                prof[k] = str(override[k]).strip()
        ak = str(override.get("api_key") or "").strip()
        if ak and (set(ak) - set("\u2022")):
            prof["api_key"] = ak

    base_url = str(prof.get("base_url") or "").strip()
    model = str(prof.get("model") or "").strip()
    if not base_url:
        return {"ok": False, "role": role, "model": model,
                "error": "base_url 未配置，无法测试连通。"}
    if not model:
        return {"ok": False, "role": role, "base_url": base_url,
                "error": "model 未配置，无法测试连通。"}

    try:
        from core.client import ChatClient                              # type: ignore
        client = ChatClient.from_profile(prof, timeout=10.0)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "role": role, "base_url": base_url, "model": model,
                "error": "%s: %s" % (type(exc).__name__, exc)}

    try:
        reply = client.chat(
            [{"role": "user", "content": "请只回复两个字：在线"}],
            temperature=0.0, max_tokens=16,
        )
        return {"ok": True, "role": role, "base_url": base_url, "model": model,
                "reply": (reply or "").strip()[:120],
                "note": "真实联网测试成功（max_tokens=16）。"}
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "role": role, "base_url": base_url, "model": model,
                "error": "%s: %s" % (type(exc).__name__, exc)}


# ---------------------------------------------------------------- POST：未接入（不写工程 JSONL）

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
    out_dir = _response_dir(project)
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
    router.get("/api/response/config", api_config)
    router.get("/api/response/collection", api_collection)
    router.get("/api/response/list", api_list)
    router.get("/api/response/file_text", api_file_text)
    router.get("/api/response/collect/status", collect_status)

    router.post("/api/response/file_state", file_state)
    router.post("/api/response/collection", api_save_collection)
    router.post("/api/response/collect/start", collect_start)
    router.post("/api/response/collect/stop", collect_stop)
    router.post("/api/response/models/save", models_save)
    router.post("/api/response/models/test", models_test)

    router.post("/api/response/export", export_dataset)
    router.post("/api/response/merge", merge_datasets)
    router.post("/api/response/delete", delete_records)

    # 回收站：删除所选 → 进回收站（可恢复）
    router.get("/api/response/trash", trash_list)
    router.post("/api/response/trash/restore", trash_restore)
    router.post("/api/response/trash/purge", trash_purge)
    router.post("/api/response/trash/clear", trash_clear)
