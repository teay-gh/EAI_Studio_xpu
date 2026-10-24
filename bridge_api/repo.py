"""bridge_api/repo.py —— 「仓库」页桥接口（工作台）。

数据集与模型权重总览（卡片排布；一页两视图，数据集/模型权重由前端按钮切换）。
取数完全复用现有桥模块，不重复实现扫描规则：
  · 数据集  = dataset.scan 的文件行（工程 data/ 目录扫描结果，类型/体量/格式判定）
             + 每个文件的主题标签（jsonl 抽 theme/topic；文章线 txt 读配对 .meta）与训练方式
               （采集启动时记的 data/_collect_use.json）
  · 模型权重 = 真实存在的权重文件（config.train 四个产物 + 工程 checkpoints/）——
             同类型可以有多个、逐个出卡；没权重的训练类型不出卡（用户 2026-10-15）

路由（bridge.py 自动发现）：
  GET  /api/repo/summary
       → { ok, project, datasets: [...], weights: [...],
           dataset_count, weight_count, scan_error, weights_error }
  POST /api/repo/add_dataset {paths:[...]}   外部数据集文件拷进工程
  POST /api/repo/add_weight  {paths:[...]}   外部权重文件拷进工程 checkpoints/
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from typing import Any

WEIGHT_EXTS = (".pt", ".bin", ".safetensors", ".ckpt")

# 训练方式推断（与 bridge.py 总览日志同口径）+ 卡片标题用的权重名
METHOD_LABELS = {"pretrain": "预训练", "continue": "继续预训练",
                 "sft": "指令微调", "lora": "LoRA 微调", "merge": "LoRA 合并",
                 "other": "其他"}
WEIGHT_TITLE = {"pretrain": "预训练权重", "continue": "继续预训练权重",
                "sft": "指令微调权重", "lora": "LoRA 权重",
                "merge": "LoRA 合并权重", "other": "其他权重"}


def _guess_method(name: str) -> str:
    # 检查点（用户 2026-10-20 起叫 <权重名>_Check<k>.pt）按基座名字猜，别一律算「其他」
    low = re.sub(r"_check\d+(?=\.[^.]+$)", "", (name or "").lower())
    # LoRA 合并产物（<基座>_Merge.pt 这类）先判「合并」：名字里多半也带 lora，
    # 先判 lora 会把合并出来的权重标成「LoRA 微调」（用户 2026-10-22）
    if "merge" in low:
        return "merge"
    if "lora" in low:
        return "lora"
    if "continue" in low:
        return "continue"
    if "pretrain" in low:
        return "pretrain"
    if "sft" in low or "model" in low:
        return "sft"
    return "other"


# ---------- 文件夹（用户 2026-10-20：权重/数据集增加文件夹系统） ----------
# 文件夹 = 仓库里的分组容器（不动物理路径，卡片视图 + 选择器 + 垃圾桶都按它分组）。
# 成员按「显示名」记在 data/_repo_meta.json 的 folders 段；检查点权重
# （<基名>_Check<k>.pt / .lora.pt）自动归 <基名> 文件夹，不用手摆（用户 2026-10-20）。
CHECK_FOLDER_RE = re.compile(
    r"^(?P<base>.+)_check\d+(?P<lora>\.lora)?\.(pt|bin|safetensors|ckpt)$", re.I)


def _auto_folder(name: str) -> str:
    """检查点权重自动入夹：<基名>_Check<k>.pt → <基名>_Check 文件夹。

    文件夹名带 _Check 后缀（用户 2026-10-20：check 保存出来的文件夹加 check 后缀）；
    其余不自动分组。"""
    m = CHECK_FOLDER_RE.match(str(name or ""))
    return (str(m.group("base") or "") + "_Check") if m else ""


def _folders_norm(meta: dict) -> dict:
    """folders 段规整成 {名字: {items:[显示名], created:bool}}；root_items = 显式放回根目录的项。"""
    folders = meta.get("folders")
    if not isinstance(folders, dict):
        folders = {}
    out: dict = {}
    for k, v in folders.items():
        fd = v if isinstance(v, dict) else {}
        out[str(k)] = {
            "items": [str(x) for x in (fd.get("items") or []) if str(x)],
            "created": bool(fd.get("created")),
            # kind（用户 2026-10-22）：空文件夹归哪个页签 —— datasets / weights；空值 = 老记录，两边都显示
            "kind": str(fd.get("kind") or ""),
        }
    meta["folders"] = out
    meta["root_items"] = [str(x) for x in (meta.get("root_items") or []) if str(x)]
    return out


def _folder_of(meta: dict, name: str) -> str:
    """显示名 → 所属文件夹名（显式归属优先，其次自动检查点规则；空 = 根目录）。"""
    name = str(name or "")
    for fn, fd in _folders_norm(meta).items():
        if name in (fd.get("items") or []):
            return fn
    if name in (meta.get("root_items") or []):
        return ""
    return _auto_folder(name)


def _folder_locked(meta: dict, name: str) -> str:
    """所在文件夹是否被锁（用户 2026-10-20）：返回锁着的文件夹名，没有则 ""。

    文件夹锁会向下继承：锁上文件夹 = 里面所有权重/数据集/子文件夹都锁上；
    所以这里沿分组名的 "/" 逐级往上找，命中任一级就当作已锁。
    """
    locked = set(str(x) for x in (meta.get("locked") or []))
    if not locked:
        return ""
    try:
        fx = _folder_of(meta, name)
    except Exception:                            # noqa: BLE001
        return ""
    while fx:
        if fx in locked:
            return fx
        fx = fx.rsplit("/", 1)[0] if "/" in fx else ""
    return ""


def _folder_assign(meta: dict, name: str, folder: str) -> None:
    """把显示名显式放进某个文件夹（folder 为空 = 显式放回根目录，压住自动规则）。"""
    name = str(name or "")
    for fn in list(_folders_norm(meta).keys()):
        items = meta["folders"][fn]["items"]
        if name in items:
            meta["folders"][fn]["items"] = [x for x in items if x != name]
    meta["root_items"] = [x for x in (meta.get("root_items") or []) if x != name]
    folder = str(folder or "").strip()
    if folder:
        fd = meta["folders"].setdefault(folder, {"items": [], "created": True})
        if name not in fd["items"]:
            fd["items"].append(name)
    else:
        meta["root_items"].append(name)


def _themes_of(path: str) -> list[str]:
    """文件的主题标签：jsonl 抽 theme/topic 去重（只读前 80 行）；文章线 txt 读配对 .meta/<stem>.jsonl。

    2026-10-22（用户）：.meta 就在板块目录里（data/article/.meta），原来往上多找了一级
    （data/.meta）——永远找不到，仓库里所有文章线 txt 的主题都显示「—」。
    """
    p = path
    if path.endswith(".txt"):
        try:
            import article as art_api
            cand = art_api.meta_path_for(os.path.dirname(path), os.path.basename(path))
        except Exception:                            # noqa: BLE001
            cand = ""
        if not cand or not os.path.isfile(cand):
            return []
        p = cand
    out: list[str] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 80:
                    break
                try:
                    obj = json.loads(line)
                except Exception:                   # noqa: BLE001, S112
                    continue
                if isinstance(obj, dict):
                    t = str(obj.get("theme") or obj.get("topic") or "").strip()
                    if t and t not in out:
                        out.append(t)
    except Exception:                               # noqa: BLE001
        return []
    return out[:12]


def _use_map(project: str) -> dict:
    """采集启动时记的「训练用途」：文件名 → {kind, use}（pretrain/continue/sft/lora）。"""
    import _host                                    # 局部导入：插件由 bridge.py 注入，避免加载期循环依赖
    p = os.path.join(project, "data", "_collect_use.json")
    data = _host._read_json_atomic(p, {})            # 读也拿锁（2026-10-24）
    return data if isinstance(data, dict) else {}


def _weight_items(project: str) -> list[dict]:
    """权重 = 真实存在的权重文件（同类型可多个；没权重的训练类型不出卡）。

    来源：config.train 四个产物路径 + 工程 checkpoints/ 下的 *.pt/*.bin/*.safetensors/*.ckpt，
    按绝对路径去重；每个文件按文件名推断训练方式。
    """
    items: dict = {}
    try:
        import _host
        hist_all = _host.read_weight_hist(project) or {}
    except Exception:                               # noqa: BLE001
        hist_all = {}

    def add(p: str, source: str) -> None:
        ap = os.path.abspath(p or "")
        if not ap or ap in items or not os.path.isfile(ap):
            return
        try:
            st = os.stat(ap)
        except OSError:
            return
        name = os.path.basename(ap)
        method = _guess_method(name)
        # 训练溯源（用户 2026-10-20）：检查点 <基名>_Check<k> 沿用基座的训练历史——
        # 它就是那次训练的产物，别退化成按文件名猜的「其他」
        hist = hist_all.get(name)
        if not hist:
            m = CHECK_FOLDER_RE.match(name)
            if m:
                cand = str(m.group("base") or "") + (m.group("lora") or "") + os.path.splitext(name)[1]
                hist = hist_all.get(cand)
        items[ap] = {
            "name": name,
            "path": ap,
            "size": int(st.st_size),
            "mtime": int(st.st_mtime),
            "method": method,
            "method_label": METHOD_LABELS.get(method, "其他"),
            "title": WEIGHT_TITLE.get(method, "其他权重"),
            "source": source,
            "hist": [r for r in (hist or []) if isinstance(r, dict)][-6:],
        }

    try:
        import export as ex_api                      # 同目录桥模块（bridge 已把目录挂上）
        w = ex_api._collect()
        for it in ((w or {}).get("items") or []):
            if isinstance(it, dict):
                add(str(it.get("path") or ""), str(it.get("source") or "config.train"))
    except Exception:                                # noqa: BLE001
        pass
    ck = os.path.join(project, "checkpoints")
    if os.path.isdir(ck):
        try:
            for fn in sorted(os.listdir(ck)):
                if os.path.splitext(fn)[1].lower() in WEIGHT_EXTS:
                    add(os.path.join(ck, fn), "工程 checkpoints/")
        except OSError:
            pass
    return sorted(items.values(), key=lambda r: r.get("mtime") or 0, reverse=True)


def _origin_of(project: str, path: str) -> str:
    """数据集来源（用户 2026-10-22）：按所在板块目录判定。

    语料蒸馏 = data/article、响应蒸馏 = data/response、网页整理 = data/webcurate；
    工程外的（导入时只引用原位置的）以及 data/ 下其它位置一律「外部导入」。
    """
    try:
        ap = os.path.abspath(str(path or ""))
        root = os.path.abspath(os.path.join(project, "data")) if project else ""
        if not ap or not root:
            return "外部导入"
        if not ap.lower().startswith(root.lower() + os.sep):
            return "外部导入"
        top = os.path.relpath(ap, root).split(os.sep)[0]
        return {"article": "语料蒸馏", "response": "响应蒸馏",
                "webcurate": "网页整理"}.get(top, "外部导入")
    except Exception:                                # noqa: BLE001
        return "外部导入"


def _web_curate_items(project: str) -> list[dict]:
    """网页整理产物当「文本」数据集进仓库（用户 2026-10-22：data/webcurate/*.txt）。

    网页整理不写 .meta 侧档，主题在产物索引 data/_web_curate.json 里（按文件名 → topic）。
    形状与 dataset.scan 的条目对齐，后面的字段拼装/筛选/右键一律照旧。
    """
    out: list = []
    if not project:
        return out
    d = os.path.join(project, "data", "webcurate")
    if not os.path.isdir(d):
        return out
    idx: dict = {}
    try:
        with open(os.path.join(project, "data", "_web_curate.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            idx = data
    except Exception:                                # noqa: BLE001
        idx = {}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for fn in names:
        if not fn.lower().endswith(".txt") or "rejected" in fn.lower():
            continue
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read()
        except OSError:
            continue
        m = idx.get(fn)
        m = m if isinstance(m, dict) else {}
        topic = str(m.get("topic") or "").strip()
        out.append({
            "name": fn,
            "folder": "webcurate",
            "path": p,
            "kind": "文本",
            "kind_key": "plain",
            "tab": ["pretrain"],
            "chars": int(m.get("chars") or len(txt)),
            "size_bytes": int(st.st_size),
            "lines": (txt.count("\n") + (0 if txt.endswith("\n") or not txt else 1)),
            "ok": True,
            "fmt_ok": True,
            "fmt_msg": "",
            "themes": [topic] if topic else [],
        })
    return out


def summary(_query: dict | None = None) -> dict:
    import dataset as ds_api                         # 同目录桥模块（bridge 已把目录挂上）

    project = ""
    scan_error = ""
    datasets: list = []
    try:
        # 两次扫描（pretrain / sft），每个文件取「归属页签」那次的格式判定：
        # 纯文本按预训练口径、对话语料按问答口径，别拿 sft 尺子量纯文本。
        by_path: dict = {}
        for k in ("pretrain", "sft"):
            res = ds_api.scan({"kind": k})
            if isinstance(res, dict) and res.get("error"):
                scan_error = str(res.get("error"))
                continue
            project = project or str((res or {}).get("project") or "")
            for f in ((res or {}).get("files") or []):
                if not isinstance(f, dict):
                    continue
                p = str(f.get("path") or "")
                tabs = list(f.get("tab") or [])
                if (k in tabs) or (p not in by_path):
                    by_path[p] = f
        # 网页整理产物也进仓库，按「文本」归类（用户 2026-10-22）
        for _wf in _web_curate_items(project):
            if _wf.get("path") and _wf["path"] not in by_path:
                by_path[_wf["path"]] = _wf
        uses = _use_map(project) if project else {}
        meta_all = _repo_meta(project) if project else {"pinned": [], "remarks": {}, "names": {}, "themes": {}, "locked": []}
        for f in by_path.values():
            name = _shown_name(project, str(f.get("name", "")))   # 外部引用重命名后显示别名
            use = str(((uses.get(name) or {}) if isinstance(uses.get(name), dict) else {}).get("use") or "")
            use2 = str(((uses.get(name) or {}) if isinstance(uses.get(name), dict) else {}).get("use2") or "")   # 第二标注（用户 2026-10-16：一数据集可标两次）
            meta = meta_all
            _pl = meta.get("pinned") or []
            _th_ov = meta.get("themes") or {}
            _themes = (list(_th_ov[name]) if name in _th_ov
                       else (list(f.get("themes") or []) or _themes_of(str(f.get("path") or ""))))
            _pl_lock = meta.get("locked") or []
            datasets.append({
                "pinned": name in _pl,
                "pin_rank": _pl.index(name) if name in _pl else 9999,
                "remark": str((meta.get("remarks") or {}).get(name) or ""),
                "name": name,
                "folder": f.get("folder", ""),
                "vfolder": _folder_of(meta_all, name),   # 文件夹分组（用户 2026-10-20）
                "path": f.get("path", ""),
                "kind": f.get("kind", ""),          # 对话 / JSONL / 纯文本
                "kind_key": f.get("kind_key", ""),  # chat / plain / other
                "tab": list(f.get("tab") or []),    # pretrain / sft
                "chars": int(f.get("chars") or 0),
                "size_bytes": int(f.get("size_bytes") or 0),
                "lines": int(f.get("lines") or 0),
                "ok": bool(f.get("ok", True)),
                "fmt_ok": bool(f.get("fmt_ok", False)),
                "fmt_msg": f.get("fmt_msg", ""),
                "themes": _themes,
                "origin": _origin_of(project, str(f.get("path") or "")),   # 来源（用户 2026-10-22）
                "use": use,                          # 训练方式：pretrain/continue/sft/lora（空=未标注）
                "use2": use2,                        # 第二标注（用户 2026-10-16：一数据集可标两次）
                # 锁（用户 2026-10-20）：自己锁上，或所在文件夹锁上（文件夹锁向下继承）
                "locked": (name in (_pl_lock or [])) or bool(_folder_locked(meta_all, name)),
                "lock_folder": _folder_locked(meta_all, name),
            })
    except Exception as exc:                         # noqa: BLE001
        scan_error = "%s: %s" % (type(exc).__name__, exc)

    weights_error = ""
    weights: list = []
    try:
        if project:
            weights = _weight_items(project)
            meta = _repo_meta(project)
            for w in weights:
                wname = str(w.get("name") or "")
                _pl2 = meta.get("pinned") or []
                w["pinned"] = wname in _pl2
                w["pin_rank"] = _pl2.index(wname) if wname in _pl2 else 9999
                w["remark"] = str((meta.get("remarks") or {}).get(wname) or "")
                # 锁（用户 2026-10-20）：自己锁上，或所在文件夹锁上（文件夹锁向下继承）
                w["lock_folder"] = _folder_locked(meta, wname)
                w["locked"] = (wname in (meta.get("locked") or [])) or bool(w["lock_folder"])
                w["vfolder"] = _folder_of(meta, wname)               # 文件夹分组（用户 2026-10-20）
    except Exception as exc:                         # noqa: BLE001
        weights_error = "%s: %s" % (type(exc).__name__, exc)

    # 文件夹（用户 2026-10-20）：成员合计出一张卡；手工建的空文件夹也出卡
    folders: list = []
    try:
        meta_f = _repo_meta(project) if project else {}
        _pl3 = meta_f.get("pinned") or []
        _lock3 = meta_f.get("locked") or []
        fsum: dict = {}
        _kinds = [(it, "ds") for it in datasets] + [(it, "wt") for it in weights]
        for it, _kd in _kinds:
            fn = str(it.get("vfolder") or "")
            if not fn:
                continue
            e = fsum.setdefault(fn, {"name": fn, "count": 0, "size": 0, "mtime": 0})
            e["count"] += 1
            # 类型计数（用户 2026-10-22）：移动时数据集只能进「没有权重」的文件夹，反之亦然
            e["n_ds" if _kd == "ds" else "n_wt"] = e.get("n_ds" if _kd == "ds" else "n_wt", 0) + 1
            e["size"] += int(it.get("size") or it.get("size_bytes") or 0)
            e["mtime"] = max(e["mtime"], int(it.get("mtime") or 0))
            # 来源（用户 2026-10-20）：文件夹卡第四行「来源」= 自建 / 里面的训练方式
            _h = [h for h in (it.get("hist") or []) if isinstance(h, dict)]
            e.setdefault("_labels", []).append(
                str((_h[-1].get("m") if _h else "") or it.get("method_label") or ""))
        for fn in list(fsum.keys()) + [str(k) for k in (meta_f.get("folders") or {}).keys()]:
            e = fsum.setdefault(fn, {"name": fn, "count": 0, "size": 0, "mtime": 0})
            created = bool((meta_f.get("folders") or {}).get(fn, {}).get("created"))
            if not e["count"] and not created:
                fsum.pop(fn, None)          # 空的自动文件夹不出卡
                continue
            e["created"] = created
            e["auto"] = not created
            e["remark"] = str((meta_f.get("remarks") or {}).get(fn) or "")
            e["pinned"] = fn in _pl3
            e["pin_rank"] = _pl3.index(fn) if fn in _pl3 else 9999
            e["locked"] = fn in _lock3
            e.setdefault("n_ds", 0)
            e.setdefault("n_wt", 0)
            e["kind"] = str(((meta_f.get("folders") or {}).get(fn) or {}).get("kind") or "")
            labels = [x for x in (e.pop("_labels", []) or []) if x]
            uniq = sorted(set(labels))
            if created:
                e["origin"] = "自建"                    # 手工建的文件夹
            elif len(uniq) == 1:
                e["origin"] = uniq[0]                   # 训练自动建的检查点文件夹：沿用训练方式
            elif uniq:
                e["origin"] = "多种"
            else:
                e["origin"] = "自建"
        folders = sorted(fsum.values(), key=lambda r: (0 if r.get("pinned") else 1,
                                                      r.get("pin_rank") or 9999,
                                                      -(r.get("mtime") or 0)))
    except Exception:                              # noqa: BLE001
        pass

    return {
        "ok": True,
        "project": project,
        "datasets": datasets,
        "weights": weights,
        "folders": folders,
        "dataset_count": len(datasets),
        "weight_count": len(weights),
        "scan_error": scan_error,
        "weights_error": weights_error,
    }


def _unique_dest(dest_dir: str, name: str) -> str:
    """同名不覆盖：name.txt → name (2).txt → name (3).txt …"""
    stem, ext = os.path.splitext(name)
    p = os.path.join(dest_dir, name)
    i = 2
    while os.path.exists(p):
        p = os.path.join(dest_dir, "%s (%d)%s" % (stem, i, ext))
        i += 1
    return p


def _review_dataset(path: str) -> tuple:
    """外部数据集入库前的格式审查（用户 2026-10-15）：官方判定（core.data）合格才收。

    口径同数据集页：能判类型（纯文本→预训练口径 / 对话→问答口径）且本口径下合格；
    空白/损坏/判定不出一律拒收。返回 (ok, reason)。
    """
    # 文章条目 jsonl（theme/title/article）也是合法数据集（用户 2026-10-16：外部添加放行）
    if path.lower().endswith((".jsonl", ".json")):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                first = f.readline().strip()
            obj = json.loads(first) if first else None
            if isinstance(obj, dict) and (obj.get("article") or obj.get("text")) \
                    and not (obj.get("question") or obj.get("answer")):
                return True, "文章条目 OK"
        except Exception:  # noqa: BLE001, S112
            pass
    import dataset as ds_api                     # 同目录桥模块（bridge 已把目录挂上）
    D = ds_api._data()
    a = D.audit_file(path)
    if not bool(getattr(a, "ok", True)):
        issues = [str(x) for x in (getattr(a, "issues", []) or [])]
        return False, "；".join(issues) or "文件不可用"
    k = ds_api._kind_key(a)
    if k == "other":
        return False, "格式判定不出（空白/损坏）"
    fmt_ok, fmt_msg = ds_api._fmt_verdict(a, "sft" if k == "chat" else "pretrain")
    return bool(fmt_ok), str(fmt_msg or "格式不合格")


def _journal_add(project: str, kind: str, name: str, path: str,
                 ok: bool = True, reason: str = "") -> None:
    """仓库添加留痕 → data/_repo_adds.json（总览「日志」行用，用户 2026-10-15）；
    append-only，失败静默不影响添加本身。ok=False=拒收/失败（2026-10-16 补：失败也留日志）。"""
    try:
        d = os.path.join(project, "data")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "_repo_adds.json")
        items: list = []
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    items = json.load(f)
                if not isinstance(items, list):
                    items = []
            except Exception:  # noqa: BLE001  坏档当空表重来
                items = []
        items.append({"kind": kind, "name": name, "path": path, "mtime": time.time(),
                      "ok": bool(ok), "reason": str(reason or "")})
        with open(p, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass


def add_files(body: dict | None, kind: str) -> dict:
    """仓库页「+」添加（用户 2026-10-16）：外部文件**直接引用路径、不复制进工程**；
    合并产物才落工程文件夹（<工程>/corpora/，扫描目录看不到）。

    数据集：审查合格后记进 data/.scan_files.json（扫描/合并读原位置）。
    权重：*.pt/*.bin/*.safetensors/*.ckpt → 拷进 checkpoints/（训练/导出链路吃工程内文件）。
    """
    body = body or {}
    paths = [str(p) for p in (body.get("paths") or []) if str(p).strip()]
    if not paths:
        return {"ok": False, "error": "没有选择文件"}
    try:
        import dataset as ds_api                     # 同目录桥模块（bridge 已把目录挂上）
        # body 可带 project 覆盖（测试打 scratch 工程用）；默认 = 当前工程
        project = ds_api._resolve_project(str((body or {}).get("project") or ""))
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "error": "读取工程失败：%s: %s" % (type(exc).__name__, exc)}
    if not project:
        return {"ok": False, "error": "工程未配置"}
    # 进哪个文件夹（用户 2026-10-22）：
    #   folder 非空 = 直接放进这个文件夹（在文件夹内导入，不再套一层子文件夹）
    #   folder 为空 + auto_folder = 在根目录导入文件夹，按源目录名新建同名文件夹
    #   同名文件夹已存在 → 整批不导入，前端弹窗提示
    target_folder = str((body or {}).get("folder") or "").strip()
    auto_folder = str((body or {}).get("auto_folder") or "").strip()
    if auto_folder and not target_folder:
        _m0 = _repo_meta(project)
        if auto_folder in _folders_norm(_m0):
            return {"ok": False, "error": "已存在同名文件夹「%s」，不能导入" % auto_folder}
        target_folder = auto_folder
    added: list = []
    errors: list = []
    rejected: list = []
    # 选进来的可能整个是文件夹（用户 2026-10-16）：递归展开里面的可添加文件
    exts = WEIGHT_EXTS if kind == "weight" else (".txt", ".jsonl", ".json", ".md", ".csv")
    files: list = []
    for src in paths:
        if os.path.isdir(src):
            folder = os.path.basename(os.path.normpath(src)) or src
            got: list = []
            for root, _dirs, fs2 in os.walk(src):
                for fn in sorted(fs2):
                    if os.path.splitext(fn)[1].lower() in exts:
                        got.append(os.path.join(root, fn))
            if got:
                files.extend(got)
            else:
                errors.append("%s：文件夹里没有可添加的文件" % folder)
                _journal_add(project, kind, folder, src, ok=False, reason="文件夹里没有可添加的文件")
        elif os.path.isfile(src):
            files.append(src)
        else:
            errors.append("%s：文件不存在" % os.path.basename(src))
            _journal_add(project, kind, os.path.basename(src), src, ok=False, reason="文件不存在")
    for src in files:
        try:
            name = os.path.basename(src)
            ext = os.path.splitext(name)[1].lower()
            if kind == "weight":
                if ext not in WEIGHT_EXTS:
                    errors.append("%s：不是权重文件" % name)
                    _journal_add(project, kind, name, src, ok=False, reason="不是权重文件")
                    continue
                dest_dir = os.path.join(project, "checkpoints")
                # 口径 2026-10-15：仓库不允许添加同名权重 —— 同名直接拒绝，不自动改名
                if os.path.exists(os.path.join(dest_dir, name)):
                    errors.append("%s：已存在同名权重，不能添加" % name)
                    _journal_add(project, kind, name, src, ok=False, reason="已存在同名权重，不能添加")
                    continue
            else:
                if ext not in (".txt", ".jsonl", ".json", ".md", ".csv"):
                    errors.append("%s：不是数据集文件" % name)
                    _journal_add(project, kind, name, src, ok=False, reason="不是数据集文件")
                    continue
                # 入库前格式审查（用户 2026-10-15：外部数据集只能从仓库添加、添加时就做好审查）
                rv_ok, rv_msg = _review_dataset(src)
                if not rv_ok:
                    rejected.append({"name": name, "reason": rv_msg})
                    _journal_add(project, kind, name, src, ok=False, reason=str(rv_msg))
                    continue
                # 引用不复制（用户 2026-10-16）：记进 .scan_files.json，扫描/合并读原位置
                import _corpus
                src_abs = os.path.abspath(src)
                refs = _corpus.scan_refs(project)
                if src_abs in refs:
                    errors.append("%s：已在仓库中" % name)
                    _journal_add(project, kind, name, src, ok=False, reason="已在仓库中")
                    continue
                if any(os.path.basename(rp) == name for rp in refs):
                    errors.append("%s：已存在同名数据集，不能添加" % name)
                    _journal_add(project, kind, name, src, ok=False, reason="已存在同名数据集，不能添加")
                    continue
                _corpus.add_ref(project, src_abs)
                added.append(name)
                _journal_add(project, kind, name, src_abs)   # 总览日志行；path=外部原位置
                continue
            os.makedirs(dest_dir, exist_ok=True)
            dest = _unique_dest(dest_dir, name)
            shutil.copy2(src, dest)
            added.append(os.path.basename(dest))
            _journal_add(project, kind, os.path.basename(dest), dest)   # 总览日志行（2026-10-15）
        except Exception as exc:                    # noqa: BLE001
            errors.append("%s：%s: %s" % (os.path.basename(src), type(exc).__name__, exc))
            _journal_add(project, kind, os.path.basename(src), src, ok=False,
                         reason="%s: %s" % (type(exc).__name__, exc))
    if target_folder and added:
        try:
            meta_x = _repo_meta(project)
            for nm in added:
                _folder_assign(meta_x, nm, target_folder)
            meta_x["folders"].setdefault(target_folder, {"items": [], "created": True})["created"] = True
            _repo_meta_save(project, meta_x)
        except Exception as exc:                    # noqa: BLE001
            errors.append("归类到文件夹失败：%s" % exc)
    return {"ok": not errors, "added": added, "errors": errors, "rejected": rejected,
            "folder": target_folder}


def _repo_meta(project: str) -> dict:
    """仓库卡片元数据（用户 2026-10-16）：data/_repo_meta.json = {pinned:[名], remarks:{名:备注}}。"""
    out = {"pinned": [], "remarks": {}, "names": {}, "themes": {}, "locked": [],
           "folders": {}, "root_items": []}
    try:
        p = os.path.join(project, "data", "_repo_meta.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                if isinstance(d.get("pinned"), list):
                    out["pinned"] = [str(x) for x in d["pinned"]]
                if isinstance(d.get("remarks"), dict):
                    out["remarks"] = {str(k): str(v) for k, v in d["remarks"].items()}
                if isinstance(d.get("names"), dict):
                    out["names"] = {str(k): str(v) for k, v in d["names"].items()}
                if isinstance(d.get("themes"), dict):
                    out["themes"] = {str(k): list(v or []) for k, v in d["themes"].items()}
                if isinstance(d.get("locked"), list):   # 锁（用户 2026-10-20）：锁上的权重/数据集在 EAI 内不许删
                    out["locked"] = [str(x) for x in d["locked"]]
                if isinstance(d.get("folders"), dict):  # 文件夹（用户 2026-10-20）
                    out["folders"] = d["folders"]
                if isinstance(d.get("root_items"), list):
                    out["root_items"] = d["root_items"]
    except Exception:  # noqa: BLE001
        pass
    _folders_norm(out)
    return out


def _repo_meta_save(project: str, meta: dict) -> None:
    try:
        d = os.path.join(project, "data")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "_repo_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("元数据保存失败：%s" % exc)


def _real_name(project: str, name: str) -> str:
    """显示名 → 磁盘文件名（外部引用的重命名是显示别名，不动物理文件）。"""
    meta = _repo_meta(project)
    return str((meta.get("names") or {}).get(name) or name)


def _shown_name(project: str, real: str) -> str:
    """磁盘文件名 → 显示名（names 反查）。"""
    meta = _repo_meta(project)
    for k, v in (meta.get("names") or {}).items():
        if v == real:
            return str(k)
    return str(real)


def _find_file(project: str, name: str) -> str | None:
    """显示名 → 文件绝对路径（外部引用优先，其次工程 data/ 树）。"""
    import _corpus
    real = _real_name(project, name)
    for rp in _corpus.scan_refs(project):
        if os.path.basename(rp) == real and os.path.isfile(rp):
            return rp
    base = os.path.abspath(os.path.join(project, "data"))
    for root, _dirs, files in os.walk(base):
        if real in files:
            return os.path.join(root, real)
    ck = os.path.abspath(os.path.join(project, "checkpoints"))
    for root, _dirs, files in ([] if not os.path.isdir(ck) else os.walk(ck)):
        if real in files:
            return os.path.join(root, real)
    return None


def _remark_len(text: str) -> float:
    """备注长度（用户 2026-10-16：≤15 个中文字或 ≤20 个英文字）：
    中文按 1 计、其余按 0.75 计，总和 ≤ 15 —— 纯英文正好 20 个。"""
    u = 0.0
    for ch in text:
        u += 1.0 if ord(ch) > 0x2E7F else 0.75
    return u


def mark(body: dict | None) -> dict:
    """右键菜单：置顶 / 备注（用户 2026-10-16）。{name, pinned?, remark?}。"""
    b = body or {}
    name = str(b.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "缺少文件名"}
    import dataset as ds_api                     # 同目录桥模块（bridge 已把目录挂上）
    project = ds_api._resolve_project(str(b.get("project") or ""))
    meta = _repo_meta(project)
    try:
        if "pinned" in b:
            pin = bool(b.get("pinned"))
            lst = [x for x in meta["pinned"] if x != name]
            if pin:
                lst.insert(0, name)
            meta["pinned"] = lst
        if "remark" in b:
            rm = str(b.get("remark") or "").strip()
            if rm:
                if _remark_len(rm) > 15:
                    return {"ok": False, "error": "备注过长：不超过 15 个中文字或 20 个英文字"}
                meta["remarks"][name] = rm
            else:
                meta["remarks"].pop(name, None)
        if "theme" in b:
            # 主题覆盖（用户 2026-10-16：详细信息里可编辑）：空 = 清空；缺省不动 = 照旧读 .meta 侧车
            th = b.get("theme")
            if isinstance(th, str):
                th = [x.strip() for x in th.split("、") if x.strip()]
            if not isinstance(th, list):
                th = []
            th = [str(x).strip() for x in th if str(x).strip()]
            if th:
                meta["themes"][name] = th
            else:
                meta["themes"][name] = []
        _repo_meta_save(project, meta)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def set_lock(body: dict | None) -> dict:
    """锁定 / 解锁（用户 2026-10-20）：{name, locked}。

    锁只对本应用生效（用户直接删文件夹管不着）：删文件前先查这张表，
    锁上的权重/数据集一律拒绝删除；被锁权重训练时也不许勾「覆盖初始权重」。
    """
    b = body or {}
    name = str(b.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "缺少文件名"}
    import dataset as ds_api
    project = ds_api._resolve_project(str(b.get("project") or ""))
    meta = _repo_meta(project)
    want = bool(b.get("locked"))
    # 文件夹判定：手工建的记在 meta.folders；自动文件夹（检查点按前缀成组）要用成员反查
    is_folder = name in (meta.get("folders") or {})
    if not is_folder:
        try:
            is_folder = bool(_folder_members(project, meta, name)) or any(
                _folder_of(meta, str(x)) == name for x in (meta.get("locked") or []))
        except Exception:                        # noqa: BLE001
            is_folder = False

    # 文件夹锁向下继承（用户 2026-10-20）：
    #   · 文件夹不解锁时，里面的文件不许单独解锁；
    #   · 文件夹解锁时，文件跟随解锁（连带清掉成员自己的锁）。
    if not is_folder:
        owner = _folder_locked(meta, name)
        if owner and not want:
            return {"ok": False, "error": "「%s」已锁定，先解锁文件夹再操作" % owner}

    lst = [x for x in (meta.get("locked") or []) if x != name]
    if want:
        lst.append(name)
    elif is_folder:
        # 文件夹解锁 → 成员跟随解锁：清掉所有落在这个文件夹里的成员锁
        drop = [x for x in lst if x != name and _folder_of(meta, str(x)) == name]
        lst = [x for x in lst if x not in drop]
    meta["locked"] = lst
    try:
        _repo_meta_save(project, meta)
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "error": "锁定状态保存失败：%s" % exc}
    return {"ok": True, "name": name, "locked": want, "folder": bool(is_folder), "locked_names": lst}


def lock_state(body: dict | None) -> dict:
    """批量查锁状态（训练页用）：{paths:[绝对路径]} → {locked:{路径:bool}}。

    显示名可能是外部引用的别名，所以除了按 basename 比，还要按 names 别名反查。
    """
    b = body or {}
    paths = b.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        paths = []
    import dataset as ds_api
    project = ds_api._resolve_project(str(b.get("project") or ""))
    meta = _repo_meta(project)
    locked = set(str(x) for x in (meta.get("locked") or []))
    alias = meta.get("names") or {}
    out: dict = {}
    for raw in paths:
        ap = os.path.abspath(str(raw or ""))
        base = os.path.basename(ap)
        hit = base in locked
        if not hit:
            for shown, real in alias.items():
                if os.path.basename(str(real or "")) == base and str(shown) in locked:
                    hit = True
                    break
        if not hit:
            # 文件夹锁向下继承（用户 2026-10-20）：成员按所在文件夹反查
            hit = bool(_folder_locked(meta, base))
        out[str(raw or "")] = hit
    return {"ok": True, "locked": out}


def is_locked_path(project: str, path: str) -> bool:
    """同步查一个路径是否被锁（训练/压缩启动前的兜底，用户 2026-10-20）。"""
    if not project or not path:
        return False
    try:
        meta = _repo_meta(project)
        locked = set(str(x) for x in (meta.get("locked") or []))
        base = os.path.basename(os.path.abspath(path))
        if base in locked:
            return True
        for shown, real in (meta.get("names") or {}).items():
            if os.path.basename(str(real or "")) == base and str(shown) in locked:
                return True
        if _folder_locked(meta, base):           # 文件夹锁向下继承（用户 2026-10-20）
            return True
    except Exception:                            # noqa: BLE001
        return False
    return False


def copy_item(body: dict | None) -> dict:
    """右键「复制」（用户 2026-10-20）：权重/数据集原目录就地复制一份。

    {path, new_name?}；不填新名字时用「原名_副本」。同名、越界、外部引用一律拒绝。
    """
    b = body or {}
    src = str(b.get("path") or "").strip()
    if not src:
        return {"ok": False, "error": "缺少文件路径"}
    import dataset as ds_api
    import shutil
    project = ds_api._resolve_project(str(b.get("project") or ""))
    project_abs = os.path.abspath(project)
    ap = os.path.abspath(src)
    if not ap.startswith(project_abs + os.sep):
        return {"ok": False, "error": "这个文件不在当前工程目录内（外部引用），不能复制"}
    if not os.path.isfile(ap):
        return {"ok": False, "error": "找不到文件"}
    d = os.path.dirname(ap)
    base = os.path.basename(ap)
    stem, ext = os.path.splitext(base)
    raw_new = str(b.get("new_name") or "").strip()
    if raw_new:
        bad = set('\\/:*?"<>|')
        if any(c in bad for c in raw_new):
            return {"ok": False, "error": "名字里不能有 \\ / : * ? \" < > |"}
        if not os.path.splitext(raw_new)[1]:
            raw_new += ext
    else:
        raw_new = "%s_副本%s" % (stem, ext)
    dest = os.path.join(d, raw_new)
    if os.path.abspath(dest) == ap:
        return {"ok": False, "error": "新名字和原文件相同"}
    if os.path.exists(dest):
        return {"ok": False, "error": "「%s」已存在" % raw_new}
    try:
        shutil.copy2(ap, dest)
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "error": "复制失败：%s" % exc}
    # 副本归进原文件同一个仓库文件夹（用户 2026-10-20：文件夹系统）
    try:
        meta_c = _repo_meta(project)
        src_folder = _folder_of(meta_c, os.path.basename(ap))
        if src_folder:
            _folder_assign(meta_c, raw_new, src_folder)
            _repo_meta_save(project, meta_c)
    except Exception:                            # noqa: BLE001
        pass
    # 网页整理的正文 txt 在 .meta/ 下有同名 jsonl 侧档，一起复制，否则新文件失去来源信息
    try:
        if base.endswith("_articles.txt"):
            new_stem = raw_new[:-len("_articles.txt")] if raw_new.endswith("_articles.txt") else os.path.splitext(raw_new)[0]
            old_meta = os.path.join(d, ".meta", stem + ".jsonl")
            if os.path.isfile(old_meta):
                shutil.copy2(old_meta, os.path.join(d, ".meta", new_stem + ".jsonl"))
    except Exception:                            # noqa: BLE001
        pass
    return {"ok": True, "name": raw_new, "path": dest}


def _migrate_label_keys(project: str, name: str, new_name: str) -> None:
    """改名迁移训练方式真值键（用户 2026-10-21：改名后训练标签不能掉成「其他」）。

    两个账都按文件名键控：data/_weight_hist.json（权重训练溯源：详情弹窗「训练方式」/仓库卡标签）、
    logs/_train_methods.json（总览日志标签）。键迁移之外，老名靠检查点沿基座
    （<基名>_Check<k> → <基名>）才查到的，也把值带到新名下（改名后文件名不再匹配检查点规则）。
    """
    def _carry(path: str, val_get) -> None:
        if not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:                            # noqa: BLE001  坏档不动
            return
        if not isinstance(d, dict):
            return
        moved = False
        if name in d and new_name not in d:
            # 复制不删旧键（2026-10-21）：同次训练的检查点（<旧基名>_Check<k>）还要沿旧键继承标签
            d[new_name] = d[name]
            moved = True
        if new_name not in d:
            v = val_get(d)
            if v is not None:
                d[new_name] = v
                moved = True
        if moved:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(d, fh, ensure_ascii=False, indent=1)
            except Exception:                        # noqa: BLE001
                pass

    def _hist_val(d):
        v = d.get(name)
        if v:
            return v
        m = CHECK_FOLDER_RE.match(name)
        if m:
            cand = str(m.group("base") or "") + (m.group("lora") or "") + os.path.splitext(name)[1]
            return d.get(cand)
        return None

    def _method_val(d):
        v = d.get(name)
        if isinstance(v, str) and v:
            return v
        stem = re.sub(r"(?i)_check\d+$", "", os.path.splitext(name)[0])
        for k, mv in list(d.items()):
            ks = re.sub(r"(?i)_check\d+$", "", os.path.splitext(str(k))[0])
            if ks == stem and isinstance(mv, str) and mv:
                return mv
        return None

    _carry(os.path.join(project, "data", "_weight_hist.json"), _hist_val)
    _carry(os.path.join(project, "logs", "_train_methods.json"), _method_val)


def rename(body: dict | None) -> dict:
    """详细信息：名称可编辑（用户 2026-10-16）。
    工程内文件 = 真改文件名；外部引用 = 改显示别名（不动物理文件，与删除同一口径）。"""
    b = body or {}
    name = str(b.get("name") or "").strip()
    new_name = str(b.get("new_name") or "").strip()
    if not name or not new_name:
        return {"ok": False, "error": "缺少名称"}
    if any(ch in new_name for ch in '\\/:*?"<>|'):
        return {"ok": False, "error": "名称含非法字符"}
    import dataset as ds_api
    project = ds_api._resolve_project(str(b.get("project") or ""))
    if new_name == name:
        return {"ok": True, "name": new_name}
    meta = _repo_meta(project)
    real = _real_name(project, name)
    path = str(b.get("path") or "").strip()
    path = path if path and os.path.isfile(path) else _find_file(project, name)
    if not path:
        return {"ok": False, "error": "找不到文件"}
    # 改名不能改格式（用户 2026-10-16）：扩展名一律锁原格式，输入里带了别的扩展也只当名字的一部分
    orig_ext = os.path.splitext(real)[1]
    if orig_ext:
        base = new_name
        if base.lower().endswith(orig_ext.lower()):
            base = base[: -len(orig_ext)]
        if not base.strip():
            return {"ok": False, "error": "缺少名称"}
        new_name = base + orig_ext
    try:
        import _corpus
        ref_hit = any(os.path.basename(rp) == real and os.path.abspath(rp) == os.path.abspath(path)
                      for rp in _corpus.scan_refs(project))
        if ref_hit:
            # 外部引用：只改显示别名
            if new_name != real and any(v == real and k != name for k, v in (meta.get("names") or {}).items()):
                return {"ok": False, "error": "已存在同名数据集，不能重命名"}
            meta["names"].pop(name, None)
            if new_name != real:
                meta["names"][new_name] = real
        else:
            target = os.path.join(os.path.dirname(path), new_name)
            if os.path.exists(os.path.abspath(target)) and \
                    os.path.normcase(os.path.abspath(target)) != os.path.normcase(os.path.abspath(path)):
                return {"ok": False, "error": "已存在同名文件，不能重命名"}
            os.rename(path, target)
            # 文章线改名要把 .meta 侧档一起改名（用户 2026-10-22）：不搬的话主题/来源就断在旧名下
            if real.endswith("_articles.txt") and new_name.endswith("_articles.txt"):
                try:
                    import article as art_api
                    old_meta = art_api.meta_path_for(os.path.dirname(path), real)
                    new_meta = os.path.join(os.path.dirname(path), ".meta",
                                            new_name[:-len("_articles.txt")] + ".jsonl")
                    if (old_meta and os.path.isfile(old_meta)
                            and os.path.normcase(os.path.abspath(old_meta)) !=
                            os.path.normcase(os.path.abspath(new_meta))
                            and not os.path.exists(new_meta)):
                        os.rename(old_meta, new_meta)
                except Exception:                    # noqa: BLE001
                    pass
            meta["names"].pop(name, None)
        # 元数据键迁移（pinned/remarks/themes/names 均按显示名键控）
        meta["pinned"] = [new_name if x == name else x for x in meta["pinned"]]
        for fld in ("remarks", "themes", "names"):
            d = meta.get(fld) or {}
            if name in d:
                d[new_name] = d.pop(name)
            meta[fld] = d
        # 文件夹归属键一起迁移（用户 2026-10-20）
        for _fn, fd in _folders_norm(meta).items():
            fd["items"] = [new_name if x == name else x for x in fd["items"]]
        meta["root_items"] = [new_name if x == name else x
                              for x in (meta.get("root_items") or [])]
        _repo_meta_save(project, meta)
        # 训练方式标注键迁移
        up = os.path.join(project, "data", "_collect_use.json")
        if os.path.isfile(up):
            with open(up, encoding="utf-8") as fh:
                ud = json.load(fh)
            if isinstance(ud, dict) and name in ud:
                ud[new_name] = ud.pop(name)
                with open(up, "w", encoding="utf-8") as fh:
                    json.dump(ud, fh, ensure_ascii=False, indent=1)
        _migrate_label_keys(project, name, new_name)   # 2026-10-21：改名后训练标签不能掉成「其他」
        new_path = path if ref_hit else os.path.join(os.path.dirname(path), new_name)
        return {"ok": True, "name": new_name, "path": new_path}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def reveal(body: dict | None) -> dict:
    """详细信息「浏览」（用户 2026-10-16）：资源管理器定位到文件存储位置。"""
    b = body or {}
    path = str(b.get("path") or "").strip()
    if not path:
        import dataset as ds_api
        project = ds_api._resolve_project(str(b.get("project") or ""))
        path = _find_file(project, str(b.get("name") or "")) or ""
    if not path or not os.path.exists(path):
        return {"ok": False, "error": "路径不存在"}
    import subprocess
    norm = os.path.normpath(path)
    if os.path.isfile(norm):
        subprocess.Popen(["explorer", "/select,", norm])
    else:
        subprocess.Popen(["explorer", norm])
    return {"ok": True, "path": norm}


def delete_files(body: dict | None) -> dict:
    """右键菜单：删除 / 批量删除（用户 2026-10-16）。真删工程 data 下的文件。

    文件夹（用户 2026-10-20）：body 可带 folders:[文件夹名]（删文件夹 = 删里面的文件）；
    folder_tag = 这批文件进回收站时记的所属文件夹名（垃圾桶按它分组显示）。
    """
    b = body or {}
    names = b.get("names")
    names = [str(x) for x in names] if isinstance(names, list) else []
    folder_tag = str(b.get("folder_tag") or "")
    import dataset as ds_api                     # 同目录桥模块（bridge 已把目录挂上）
    import _trash                                # 回收站（用户 2026-10-18：仓库删除也要先进回收站）
    project = ds_api._resolve_project(str(b.get("project") or ""))
    raw_paths = b.get("paths")
    path_map: dict = {}
    if isinstance(raw_paths, dict):
        path_map = {str(k): str(v) for k, v in raw_paths.items()}
    # 文件夹整体删除：先展开成成员文件（用户 2026-10-20）
    fns = b.get("folders")
    if isinstance(fns, list) and fns:
        meta0 = _repo_meta(project)
        for fn in [str(x) for x in fns]:
            for nm, p in _folder_members(project, meta0, fn):
                if nm not in names:
                    names.append(nm)
                path_map.setdefault(nm, p)
    if not names:
        return {"ok": False, "error": "缺少文件名"}
    base = os.path.abspath(os.path.join(project, "data"))
    # 模型权重删除（用户 2026-10-18）：权重住在 <工程>/checkpoints/，不在 data/ 下，
    # 原来只扫 data/ 所以一律「找不到文件」。前端按名字带上真实路径，这里校验必须在工程内。
    project_abs = os.path.abspath(project)
    deleted: list = []
    errors: list = []
    meta = _repo_meta(project)
    import _corpus
    refs = _corpus.scan_refs(project)
    locked_names = set(str(x) for x in (meta.get("locked") or []))
    for nm in names:
        nm = str(nm)
        real = _real_name(project, nm)          # 显示名 → 磁盘文件名（外部引用重命名=别名）
        # 锁（用户 2026-10-20）：锁上的权重/数据集在 EAI 里不许删，先在仓库右键解锁
        if nm in locked_names or real in locked_names:
            errors.append("%s：已锁定，先解锁再删除" % nm)
            continue
        owner = _folder_locked(meta, nm)         # 文件夹锁向下继承（用户 2026-10-20）
        if owner:
            errors.append("%s：所在文件夹「%s」已锁定，先解锁文件夹再删除" % (nm, owner))
            continue
        try:
            p_hit = path_map.get(nm)
            if p_hit:
                ap = os.path.abspath(p_hit)
                if not ap.startswith(project_abs + os.sep):
                    errors.append("%s：不在当前工程目录内，已拒绝删除" % nm)
                    continue
                if not os.path.isfile(ap):
                    errors.append("%s：找不到文件" % nm)
                    continue
                moved = _trash.file_add(project, ap, "weights",
                                        folder=(folder_tag or _folder_of(meta, nm)))   # 垃圾桶分组（2026-10-20）
                if not moved.get("ok"):
                    errors.append("%s：%s" % (nm, moved.get("error") or "移入回收站失败"))
                    continue
                deleted.append(nm)
                meta["pinned"] = [x for x in meta["pinned"] if x != nm]
                meta["remarks"].pop(nm, None)
                meta["themes"].pop(nm, None)
                meta["names"].pop(nm, None)
                continue
            # 外部引用：只摘引用清单，**不动用户原位置的文件**（用户 2026-10-16）
            ref_hit = next((rp for rp in refs if os.path.basename(rp) == real), None)
            if ref_hit:
                _corpus.del_ref(project, ref_hit)
                deleted.append(nm)
                meta["pinned"] = [x for x in meta["pinned"] if x != nm]
                meta["remarks"].pop(nm, None)
                meta["themes"].pop(nm, None)
                meta["names"].pop(nm, None)
                continue
            path = None
            for root, _dirs, files in os.walk(base):
                if real in files:
                    path = os.path.join(root, real)
                    break
            if not path:
                errors.append("%s：找不到文件" % nm)
                continue
            moved = _trash.file_add(project, path, "dataset",
                                    folder=(folder_tag or _folder_of(meta, nm)))   # 数据集同样先进回收站
            if not moved.get("ok"):
                errors.append("%s：%s" % (nm, moved.get("error") or "移入回收站失败"))
                continue
            deleted.append(nm)
            meta["pinned"] = [x for x in meta["pinned"] if x != nm]
            meta["remarks"].pop(nm, None)
        except Exception as exc:  # noqa: BLE001
            errors.append("%s：%s" % (nm, exc))
    try:
        _repo_meta_save(project, meta)
    except Exception:  # noqa: BLE001
        pass
    # 清训练溯源（data/_weight_hist.json 里按文件名记的条目）
    try:
        hp = os.path.join(project, "data", "_weight_hist.json")
        if os.path.isfile(hp):
            with open(hp, "r", encoding="utf-8") as f:
                hist = json.load(f)
            if isinstance(hist, dict):
                changed = False
                for nm in deleted:
                    if nm in hist:
                        hist.pop(nm, None)
                        changed = True
                if changed:
                    with open(hp, "w", encoding="utf-8") as f:
                        json.dump(hist, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass
    # 清训练方式标注（_collect_use.json），失败不影响删除本身
    try:
        up = os.path.join(project, "data", "_collect_use.json")
        if os.path.isfile(up):
            with open(up, "r", encoding="utf-8") as f:
                uses = json.load(f)
            if isinstance(uses, dict):
                for nm in deleted:
                    uses.pop(nm, None)
                with open(up, "w", encoding="utf-8") as f:
                    json.dump(uses, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": not errors, "deleted": deleted, "errors": errors}


def dataset_use(body: dict | None) -> dict:
    """改数据集「训练方式」标注（用户 2026-10-15：仓库卡上的下拉直接改）。
    落库 data/_collect_use.json：{文件名: {kind, use, use2}}；use='' = 未标注。
    slot=1/2（用户 2026-10-16：一个数据集可标两次，两个下拉各写各的槽）。"""
    b = body or {}
    name = str(b.get("name") or "").strip()
    use = str(b.get("use") or "").strip()
    slot = str(b.get("slot") or "1").strip() or "1"
    if not name:
        return {"ok": False, "error": "缺少文件名"}
    if use not in ("pretrain", "continue", "sft", "lora", ""):
        return {"ok": False, "error": "未知训练方式：%s" % use}
    import dataset as ds_api                     # 同目录桥模块（bridge 已把目录挂上）
    project = ds_api._resolve_project(str(b.get("project") or ""))
    p = os.path.join(project, "data", "_collect_use.json")
    try:
        data = {}
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        rec = data.get(name) if isinstance(data.get(name), dict) else {}
        if slot == "2":
            rec["use2"] = use
        else:
            rec["use"] = use
        data[name] = rec
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        return {"ok": True, "name": name,
                "use": str(rec.get("use") or ""), "use2": str(rec.get("use2") or "")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def dataset_text(query: dict | None) -> dict:
    """右键卡片 → 大弹窗看完整数据集（用户 2026-10-15）：读文件全文（显示上限 200KB）。"""
    q = query or {}
    name = str(q.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "缺少文件名"}
    import dataset as ds_api                     # 同目录桥模块（bridge 已把目录挂上）
    project = ds_api._resolve_project(str(q.get("project") or ""))
    base = os.path.abspath(os.path.join(project, "data"))
    path = None
    # 外部引用文件优先命中（用户 2026-10-16：引用原位置、不复制进工程）；别名→磁盘名解析
    import _corpus
    real = _real_name(project, name)
    for rp in _corpus.scan_refs(project):
        if os.path.basename(rp) == real and os.path.isfile(rp):
            path = rp
            break
    for root, _dirs, files in ([] if path else os.walk(base)):
        if real in files:
            path = os.path.join(root, real)
            break
    if not path:
        return {"ok": False, "error": "找不到文件：%s" % name}
    try:
        cap = 200 * 1024
        with open(path, "rb") as fh:
            raw = fh.read(cap + 1)
        truncated = len(raw) > cap
        data = raw[:cap]
        # 截断处退回最后一个换行（用户 2026-10-20：半截 JSON 会让前端判定「不是 JSONL」而退回原文，
        # 一问一答的排版就丢了）
        if truncated:
            cut = data.rfind(b"\n")
            if cut > 0:
                data = data[:cut + 1]
        out = {"ok": True, "name": name,
               "text": data.decode("utf-8", "replace"),
               "truncated": truncated}
        # 纯文本条目元数据（用户 2026-10-15：补上主题和时间）——
        # 采集管线把逐条元数据落在 .meta/<同名>.jsonl（theme/created_at/title/article）。
        stem = os.path.splitext(os.path.basename(path))[0]
        # 元数据文件名=去掉尾缀的运行号（如 X_articles.txt ↔ .meta/X.jsonl），按前缀匹配取最长
        meta_file = None
        # 改名过的老工程先按尾部时间戳找 sidecar（用户 2026-10-22：TC_… ↔ TC2_… 这种前缀匹配不上）
        try:
            import article as art_api
            if os.path.basename(path).endswith("_articles.txt"):
                _hit = art_api.meta_path_for(os.path.dirname(path), os.path.basename(path))
                if _hit and os.path.isfile(_hit):
                    meta_file = _hit
        except Exception:                            # noqa: BLE001
            pass
        for mdir in (() if meta_file else (os.path.join(os.path.dirname(path), ".meta"),
                     os.path.join(os.path.dirname(os.path.dirname(path)), ".meta"))):
            if not os.path.isdir(mdir):
                continue
            best = None
            for fn in os.listdir(mdir):
                if not fn.endswith(".jsonl"):
                    continue
                mstem = fn[:-6]
                if stem == mstem or stem.startswith(mstem + "_") or mstem.startswith(stem):
                    if best is None or len(mstem) > len(os.path.splitext(best)[0]):
                        best = fn
            if best:
                meta_file = os.path.join(mdir, best)
                break
        for cand in ((meta_file,) if meta_file else ()):
            if not os.path.isfile(cand):
                continue
            recs = []
            try:
                with open(cand, "r", encoding="utf-8", errors="replace") as fh:
                    for ln in fh:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            r = json.loads(ln)
                        except Exception:  # noqa: BLE001
                            continue
                        if isinstance(r, dict):
                            recs.append({
                                "theme": str(r.get("theme") or ""),
                                "title": str(r.get("title") or ""),
                                "created_at": str(r.get("created_at") or ""),
                                "text": str(r.get("article") or r.get("text") or ""),
                            })
            except Exception:  # noqa: BLE001
                recs = []
            if recs:
                out["records"] = recs
            break
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def weight_loss(query: dict) -> dict:
    """仓库「详细信息」→ Loss 曲线（用户 2026-10-20）：读这个权重那次训练的完整 CSV。

    取数顺序：<工程>/logs/<权重名>_loss.csv（引擎跑完会留副本）→ <工程>/logs/<训练方式>_loss.csv
    → logs 下修改时间最接近权重文件的那个 *_loss.csv。
    """
    try:
        import dataset as ds_api
        project = ds_api._resolve_project(str(query.get("project") or ""))
        logs = os.path.join(project, "logs")
        if not os.path.isdir(logs):
            return {"ok": False, "error": "工程里没有 logs 目录"}
        name = str(query.get("name") or "").strip()
        path = str(query.get("path") or "").strip()
        method = str(query.get("method") or "").strip()
        stem = os.path.splitext(os.path.basename(path or name))[0]
        if stem.endswith(".lora"):
            stem = stem[:-5]

        def pick(fn: str) -> str:
            p = os.path.join(logs, fn)
            return p if os.path.isfile(p) else ""

        src = ""
        if stem:
            src = pick(stem + "_loss.csv")
        if not src and method:
            src = pick(method + "_loss.csv")
        if not src:                                  # 兜底：挑修改时间最近的那个
            try:
                target = os.stat(path).st_mtime if path and os.path.isfile(path) else 0
            except OSError:
                target = 0
            best, best_gap = "", None
            for fn in os.listdir(logs):
                if not fn.lower().endswith(".csv"):
                    continue
                fp = os.path.join(logs, fn)
                try:
                    gap = abs(os.stat(fp).st_mtime - target) if target else 0
                except OSError:
                    continue
                if best_gap is None or gap < best_gap:
                    best, best_gap = fp, gap
            src = best
        if not src:
            return {"ok": True, "points": [], "rows": 0, "source": "",
                    "note": "没有找到这个权重的训练日志 CSV"}
        points, rows = [], 0
        with open(src, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                parts = line.strip().split(",")
                if i == 0 or len(parts) < 3:
                    continue
                try:
                    points.append({"step": int(float(parts[0])),
                                   "train": float(parts[1]), "val": float(parts[2])})
                except (TypeError, ValueError):
                    continue
                rows += 1
        return {"ok": True, "points": points, "rows": rows, "source": src,
                "note": "" if points else "日志里还没有数据点"}

    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def _folder_members(project: str, meta: dict, fn: str) -> list:
    """文件夹里现有的成员 = 显式归属的项 + 自动入夹的检查点文件。[(显示名, 路径)]"""
    out: list = []
    seen: set = set()
    for nm in ((meta.get("folders") or {}).get(fn) or {}).get("items") or []:
        nm = str(nm)
        if nm in seen:
            continue
        p = _find_file(project, nm)
        if p and os.path.isfile(p):
            seen.add(nm)
            out.append((nm, p))
    ck = os.path.abspath(os.path.join(project, "checkpoints"))
    for root, _dirs, files in ([] if not os.path.isdir(ck) else os.walk(ck)):
        for f in files:
            if _auto_folder(f) == fn and f not in seen:
                seen.add(f)
                out.append((f, os.path.join(root, f)))
    return out


def folder_create(body: dict | None) -> dict:
    """新建文件夹（用户 2026-10-20）：{name}。空文件夹也出卡。"""
    b = body or {}
    name = str(b.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "文件夹名不能为空"}
    if any(ch in name for ch in '\\/:*?"<>|'):
        return {"ok": False, "error": "文件夹名里不能有 \\ / : * ? \" < > |"}
    import dataset as ds_api
    project = ds_api._resolve_project(str(b.get("project") or ""))
    meta = _repo_meta(project)
    # 重名审查（用户 2026-10-22）：权重和数据集虽然分两个页签，但文件夹是同一套名字空间 ——
    # 只要任一类型里已有同名文件夹（含检查点自动归出的 * _Check 文件夹）或同名文件，就不许再建。
    names = set()
    files = set()
    try:
        sm = summary({})
        names = set(str(f.get("name") or "") for f in (sm.get("folders") or []))
        for key in ("datasets", "weights"):
            for it in (sm.get(key) or []):
                nm = str(it.get("name") or "")
                if nm:
                    files.add(nm)
                    af = _auto_folder(nm)
                    if af:
                        names.add(af)
    except Exception:                               # noqa: BLE001  扫描失败就退回只看显式文件夹
        names = set(_folders_norm(meta).keys())
    if name in names:
        return {"ok": False, "error": "「%s」文件夹已存在" % name}
    if name in files:
        return {"ok": False, "error": "已有同名文件「%s」，文件夹不能与文件同名" % name}
    _kind = str((b.get("kind") or "") or "").strip()
    if _kind not in ("datasets", "weights"):
        _kind = "datasets"
    meta["folders"][name] = {"items": [], "created": True, "kind": _kind}
    try:
        _repo_meta_save(project, meta)
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "name": name}


def folder_rename(body: dict | None) -> dict:
    """重命名文件夹（用户 2026-10-22）：{name, new_name}。

    重名审查与新建一致：权重 / 数据集共用一个文件夹名字空间，新名字不能撞任何已有文件夹
    （含检查点自动归出的 *_Check）或已有文件；自己不算重名。自动归出的文件夹也支持改名 ——
    把成员显式挂到新名字下，老名字随之消失。
    """
    b = body or {}
    old = str(b.get("name") or "").strip()
    new = str(b.get("new_name") or "").strip()
    if not old:
        return {"ok": False, "error": "缺少文件夹名"}
    if not new:
        return {"ok": False, "error": "文件夹名不能为空"}
    if any(ch in new for ch in '\\/:*?"<>|'):
        return {"ok": False, "error": "文件夹名里不能有 \\ / : * ? \" < > |"}
    if new == old:
        return {"ok": True, "name": new, "unchanged": True}
    import dataset as ds_api
    project = ds_api._resolve_project(str(b.get("project") or ""))
    meta = _repo_meta(project)
    names = set()
    files = set()
    sm = {}
    try:
        sm = summary({})
        names = set(str(f.get("name") or "") for f in (sm.get("folders") or []))
        for key in ("datasets", "weights"):
            for it in (sm.get(key) or []):
                nm = str(it.get("name") or "")
                if nm:
                    files.add(nm)
                    af = _auto_folder(nm)
                    if af:
                        names.add(af)
    except Exception:                               # noqa: BLE001
        names = set(_folders_norm(meta).keys())
    # 成员 = 显式挂着的 + 按检查点规则自动归到老名字下的
    fd = (meta.get("folders") or {}).get(old)
    fd = fd if isinstance(fd, dict) else {}
    items = [str(x) for x in (fd.get("items") or []) if str(x)]
    auto_members = []
    for key in ("datasets", "weights"):
        for it in (sm.get(key) or []):
            if str(it.get("vfolder") or "") == old:
                nm = str(it.get("name") or "")
                if nm and nm not in items:
                    auto_members.append(nm)
    items += auto_members
    names.discard(old)                              # 自己不算重名
    # 成员自带的名字（TCX_Check1.pt → TCX_Check）不算冲突：那是这些成员自己的归集名
    for _nm in items:
        _af = _auto_folder(_nm)
        if _af:
            names.discard(_af)
    if new in names:
        return {"ok": False, "error": "「%s」文件夹已存在" % new}
    if new in files:
        return {"ok": False, "error": "已有同名文件「%s」，文件夹不能与文件同名" % new}
    kind = str(fd.get("kind") or "")
    if not kind:
        kind = "weights" if any(os.path.splitext(x)[1].lower() in WEIGHT_EXTS for x in items) else "datasets"
    created = bool(fd.get("created", True)) or bool(auto_members)
    meta["folders"].pop(old, None)
    meta["folders"][new] = {"items": items, "created": created, "kind": kind}
    # 元数据键跟着改名：置顶 / 备注 / 锁
    if old in (meta.get("pinned") or []):
        meta["pinned"] = [new if x == old else x for x in meta["pinned"]]
    _r = meta.get("remarks") or {}
    if old in _r:
        _r[new] = _r.pop(old)
        meta["remarks"] = _r
    if old in (meta.get("locked") or []):
        meta["locked"] = [new if x == old else x for x in meta["locked"]]
    try:
        _repo_meta_save(project, meta)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "name": new, "old": old, "items": items}


def item_folder(body: dict | None) -> dict:
    """移动到文件夹（用户 2026-10-20）：{names:[显示名], folder: 文件夹名；空 = 放回根目录}。"""
    b = body or {}
    names = b.get("names")
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list) or not names:
        return {"ok": False, "error": "缺少文件名"}
    folder = str(b.get("folder") or "").strip()
    import dataset as ds_api
    project = ds_api._resolve_project(str(b.get("project") or ""))
    meta = _repo_meta(project)
    for nm in [str(x) for x in names]:
        _folder_assign(meta, nm, folder)
    if folder:
        fd = meta["folders"].setdefault(folder, {"items": [], "created": True})
        fd["created"] = True                          # 挪东西进去 = 这是个真实文件夹
        if not fd.get("kind"):
            # 空文件夹第一次进东西：按成员后缀定归属页签（用户 2026-10-22）
            _is_w = any(os.path.splitext(str(nm))[1].lower() in WEIGHT_EXTS for nm in names)
            fd["kind"] = "weights" if _is_w else "datasets"
    try:
        _repo_meta_save(project, meta)
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "names": [str(x) for x in names], "folder": folder}


def folder_delete(body: dict | None) -> dict:
    """删除文件夹（用户 2026-10-20）：删文件夹 + 里面现有的文件（进回收站、带文件夹标记）。"""
    b = body or {}
    names = b.get("names")
    if not isinstance(names, list) or not names:
        nm0 = str(b.get("name") or "").strip()
        names = [nm0] if nm0 else []
    if not names:
        return {"ok": False, "error": "缺少文件夹名"}
    import dataset as ds_api
    project = ds_api._resolve_project(str(b.get("project") or ""))
    errors: list = []
    deleted: list = []
    for fn in [str(x) for x in names]:
        meta = _repo_meta(project)
        if fn in set(str(x) for x in (meta.get("locked") or [])):
            errors.append("「%s」已锁定，先解锁再删除" % fn)
            continue
        members = _folder_members(project, meta, fn)
        got = [nm for nm, _p in members]
        paths = {nm: p for nm, p in members if p}
        if got:
            res = delete_files({"names": got, "paths": paths,
                                "project": project, "folder_tag": fn})
            errors.extend(res.get("errors") or [])
            deleted.extend(res.get("deleted") or [])
        # 元数据里整个文件夹段去掉（delete_files 会自己存一份 meta，这里重读再改）
        meta = _repo_meta(project)
        meta.get("folders", {}).pop(fn, None)
        meta["pinned"] = [x for x in meta["pinned"] if x != fn]
        meta["remarks"].pop(fn, None)
        meta["themes"].pop(fn, None)
        # 删掉文件夹时把成员的锁一起清掉，避免留下指向已不存在文件的锁（用户 2026-10-20）
        meta["locked"] = [x for x in (meta.get("locked") or [])
                          if x != fn and _folder_of(meta, str(x)) != fn]
        try:
            _repo_meta_save(project, meta)
        except Exception:                            # noqa: BLE001
            pass
    return {"ok": not errors, "deleted": deleted, "errors": errors}


def folder_copy(body: dict | None) -> dict:
    """复制文件夹（用户 2026-10-20）：成员文件各复制一份「原名_副本」，归进新文件夹。"""
    b = body or {}
    fn = str(b.get("name") or "").strip()
    new_name = str(b.get("new_name") or "").strip() or (fn + "_副本" if fn else "")
    if not fn:
        return {"ok": False, "error": "缺少文件夹名"}
    if any(ch in new_name for ch in '\\/:*?"<>|'):
        return {"ok": False, "error": "文件夹名里不能有 \\ / : * ? \" < > |"}
    import dataset as ds_api
    project = ds_api._resolve_project(str(b.get("project") or ""))
    meta = _repo_meta(project)
    if new_name in _folders_norm(meta):
        return {"ok": False, "error": "「%s」文件夹已存在" % new_name}
    members = _folder_members(project, meta, fn)
    if not members:
        return {"ok": False, "error": "「%s」里没有可复制的文件" % fn}
    made: list = []
    errors: list = []
    for nm, p in members:
        res = copy_item({"path": p, "project": project})
        if not res.get("ok"):
            errors.append("%s：%s" % (nm, res.get("error") or "复制失败"))
            continue
        made.append(str(res.get("name") or ""))
    meta = _repo_meta(project)
    for nm2 in made:
        if nm2:
            _folder_assign(meta, nm2, new_name)
    meta["folders"].setdefault(new_name, {"items": [], "created": True})["created"] = True
    try:
        _repo_meta_save(project, meta)
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": not errors, "name": new_name, "copied": made, "errors": errors}


def register(router) -> None:
    router.get("/api/repo/summary", summary)
    router.get("/api/repo/dataset_text", dataset_text)
    router.get("/api/repo/weight_loss", weight_loss)
    router.post("/api/repo/dataset_use", dataset_use)
    router.post("/api/repo/mark", mark)
    router.post("/api/repo/lock", set_lock)
    router.post("/api/repo/lock_state", lock_state)
    router.post("/api/repo/copy", copy_item)
    router.post("/api/repo/rename", rename)
    router.post("/api/repo/reveal", reveal)
    router.post("/api/repo/delete_files", delete_files)
    router.post("/api/repo/folder_create", folder_create)
    router.post("/api/repo/item_folder", item_folder)
    router.post("/api/repo/folder_delete", folder_delete)
    router.post("/api/repo/folder_copy", folder_copy)
    router.post("/api/repo/folder_rename", folder_rename)
    router.post("/api/repo/add_dataset", lambda b: add_files(b, "dataset"))
    router.post("/api/repo/add_weight", lambda b: add_files(b, "weight"))
