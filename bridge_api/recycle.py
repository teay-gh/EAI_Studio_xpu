"""bridge_api/recycle.py ——「回收站」页桥接口（全局回收站，2026-10-14）。

用户口径：「回收站放在设置上方，回收站允许接收数据集、模型权重。」
→ 回收站从文章/响应各自的工具栏挪到系统区（设置上方）统一入口，站里收三类东西：

  样本   article / response 两个板块的行级回收站（沿用 bridge_api/_trash.py 的行档，恢复=按原字节写回）
  数据集   整文件（dataset）——数据集文件被删时整文件进 <工程>/.trash/files/
  模型权重  整文件（weights）——checkpoints 下的 *.pt 被删时同样进站

样本部分直接委托 article.py / response.py 各自的 trash_*（同一套行档、同一套恢复口径）；
文件部分用 _trash.file_*（工程根 .trash/，以点开头 → 扫描/合并绝不会把它当训练数据）。

  GET  /api/recycle/list                     全部回收站条目（samples + files）
  POST /api/recycle/restore  {targets}       恢复所选：[{kind:'sample',board,key} | {kind:'file',key}]
  POST /api/recycle/purge    {targets}       彻底删除所选（永久删）
  POST /api/recycle/clear    {}              清空回收站（全部永久删，不进站）
  POST /api/recycle/trash_file {path, kind}  把整文件移入回收站（kind=dataset|weights）
"""
from __future__ import annotations

import binascii
import hashlib
import json
import os
import sys
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))            # ...\EAI_Studio\bridge_api
if HERE not in sys.path:                                     # 让 `import _host` 等稳定可用
    sys.path.insert(0, HERE)

import _host                                     # noqa: E402  引擎宿主
import _trash                                    # noqa: E402  回收站实现
import article as _article                       # noqa: E402  样本回收站（委托同一套行档）
import response as _response                     # noqa: E402

_BOARDS = (("article", _article), ("response", _response))


def _root() -> str:
    """当前工程根（文件级回收站挂在 <工程>/.trash/）。"""
    cfg = _host.load_cfg()
    return _host.project_root(cfg)


def _sample_rows() -> list[dict[str, Any]]:
    """两个板块的样本回收站条目，加 board/kind 标注，按删除时间倒序。"""
    rows: list[dict[str, Any]] = []
    for board, mod in _BOARDS:
        try:
            res = mod.trash_list({})
        except Exception as exc:                 # noqa: BLE001
            rows.append({"kind": "sample", "board": board, "key": "",
                         "title": "读取失败：%s: %s" % (type(exc).__name__, exc),
                         "deleted_at": ""})
            continue
        for e in (res.get("items") or []):
            row = dict(e)
            row["kind"] = "sample"
            row["board"] = board
            rows.append(row)
    rows.sort(key=lambda r: str(r.get("deleted_at") or ""), reverse=True)
    return rows


def _folder_rows(files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把「删除时属于某个文件夹」的文件聚成一条文件夹条目（用户 2026-10-20）。

    返回 (folder_rows, loose_files)：folder_rows 里 key="folder:<名>"，带成员 tid；
    其余不属于任何文件夹的条目原样留在 loose_files 里。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    loose: list[dict[str, Any]] = []
    for e in files:
        fn = str(e.get("folder") or "")
        if fn:
            groups.setdefault(fn, []).append(e)
        else:
            loose.append(e)
    rows: list[dict[str, Any]] = []
    for fn, items in groups.items():
        kinds = set(str(i.get("kind") or "file") for i in items)
        rows.append({
            "kind": "folder",
            "key": "folder:" + fn,
            "name": fn,
            "count": len(items),
            "size_bytes": sum(int(i.get("size_bytes") or 0) for i in items),
            "deleted_at": max([str(i.get("deleted_at") or "") for i in items] or [""]),
            "orig_path": os.path.dirname(str(items[0].get("orig_path") or "")),
            # 文件夹里装的是权重还是数据集：前端按页签分流（mixed = 两种都有）
            "file_kind": ("weights" if kinds == {"weights"} else
                          "dataset" if kinds == {"dataset"} else "mixed"),
            "members": [str(i.get("tid")) for i in items],
            "names": [str(i.get("name") or "") for i in items],
            # 成员明细：回收站里双击文件夹进去看（只读浏览，用户 2026-10-20）
            "items": [{
                "key": str(i.get("tid") or ""),
                "name": str(i.get("name") or ""),
                "kind": str(i.get("kind") or "file"),
                "size_bytes": int(i.get("size_bytes") or 0),
                "deleted_at": str(i.get("deleted_at") or ""),
                "orig_path": str(i.get("orig_path") or ""),
                "method_label": str(i.get("method_label") or ""),
            } for i in items],
            # 文件夹卡也要显示训练方式（成员取自第一个带训练方式的文件）
            "method_label": next((str(i.get("method_label") or "") for i in items
                                  if str(i.get("method_label") or "")), ""),
        })
    rows.sort(key=lambda r: str(r.get("deleted_at") or ""), reverse=True)
    return rows, loose


def _weight_meta(rows: list[dict[str, Any]]) -> None:
    """给回收站里的权重补「训练方式」（用户 2026-10-20：权重卡要看训练方式和 Loss）。

    训练方式取 data/_weight_hist.json 里这条权重的最后一次记录（训练时记的中文标签），
    没有记录就按文件名猜（与仓库同一套 _guess_method）；Loss 曲线前端按名字去
    /api/repo/weight_loss 取，权重文件已经不在了也能取（logs/*_loss.csv 还在）。
    """
    try:
        import _host
        import repo as repo_api
    except Exception:                            # noqa: BLE001
        return
    try:
        hist_all = _host.read_weight_hist(_root()) or {}
    except Exception:                            # noqa: BLE001
        hist_all = {}
    for e in rows:
        if str(e.get("kind") or "") != "weights":
            continue
        name = str(e.get("name") or "")
        method = repo_api._guess_method(name)
        label = repo_api.METHOD_LABELS.get(method, "其他")
        hist = hist_all.get(name) or []
        if not hist:
            m = repo_api.CHECK_FOLDER_RE.match(name)
            if m:
                base = str(m.group("base") or "") + (m.group("lora") or "") + os.path.splitext(name)[1]
                hist = hist_all.get(base) or []
        if hist:
            last = hist[-1] if isinstance(hist[-1], dict) else {}
            label = str(last.get("m") or label)
        e["method"] = method
        e["method_label"] = label


def _file_rows() -> list[dict[str, Any]]:
    rows = []
    for e in _trash.file_items(_root()):
        row = dict(e)
        row["key"] = str(e.get("tid") or "")
        rows.append(row)
    rows.sort(key=lambda r: str(r.get("deleted_at") or ""), reverse=True)
    return rows


# ---- 保存并隔离的样本（*_rejected.jsonl，用户 2026-10-16）----
# 「保存并隔离被拒文本」写出的 <名>_<戳>_rejected.jsonl 逐行进回收站（kind=rejected）：
# 可查看、可恢复（恢复 = 行放回数据集：文章线进 .meta/<名>_<戳>.jsonl + 重建 txt 正文，
# 问答线进 <名>_<戳>.jsonl + 重建 sft；随后从隔离文件抹掉该行）。彻底删除 = 只从隔离文件抹掉。
def _rej_key(raw: str) -> str:
    """行指纹（同 _trash.add 的行 key 口径：去掉首尾空白后 sha1 前 12 位）。"""
    return hashlib.sha1((raw or "").strip().encode("utf-8")).hexdigest()[:12]


def _rej_board_dirs() -> list[tuple[str, str]]:
    cfg = _host.load_cfg()
    project = _host.project_root(cfg)
    return [("article", _article._article_dir(project)), ("response", _response._response_dir(project))]


def _rejected_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for board, d in _rej_board_dirs():
        if not d or not os.path.isdir(d):
            continue
        try:
            names = sorted(f for f in os.listdir(d) if f.endswith(".jsonl") and "rejected" in f)
        except OSError:
            continue
        for fn in names:
            path = os.path.join(d, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
            except OSError:
                continue
            for raw in lines:
                s = (raw or "").strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                except Exception:                    # noqa: BLE001
                    rec = None
                rec = rec if isinstance(rec, dict) else {}
                title = str(rec.get("title") or "")
                question = str(rec.get("question") or "")
                rows.append({
                    "kind": "rejected", "board": board,
                    "key": _rej_key(raw),
                    "src": fn,
                    "title": title, "title_short": title[:200],
                    "question": question, "question_short": question[:200],
                    "article": str(rec.get("article") or ""),
                    "answer": str(rec.get("answer") or ""),
                    "theme": str(rec.get("theme") or ""), "topic": str(rec.get("topic") or ""),
                    "deleted_at": str(rec.get("created_at") or when),
                    "size_bytes": len(raw.encode("utf-8", "replace")),
                })
    rows.sort(key=lambda r: str(r.get("deleted_at") or ""), reverse=True)
    return rows


def _find_rejected(board: str, key: str) -> tuple[str, str] | None:
    """按行指纹在该板块的 *_rejected.jsonl 里找一行；返回 (文件路径, 原始行)。"""
    for b, d in _rej_board_dirs():
        if b != board or not d or not os.path.isdir(d):
            continue
        try:
            names = sorted(f for f in os.listdir(d) if f.endswith(".jsonl") and "rejected" in f)
        except OSError:
            continue
        for fn in names:
            path = os.path.join(d, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for raw in f:
                        s = (raw or "").strip()
                        if s and _rej_key(raw) == key:
                            return path, raw
            except OSError:
                continue
    return None


def _remove_rej_line(path: str, key: str) -> bool:
    """从隔离文件抹掉首个匹配指纹的行（彻底删除 / 恢复后收尾共用）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return False
    out: list[str] = []
    dropped = False
    for raw in lines:
        s = (raw or "").strip()
        if not dropped and s and _rej_key(raw) == key:
            dropped = True
            continue
        out.append(raw)
    if not dropped:
        return False
    tmp = path + ".eai-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.writelines(out)
    os.replace(tmp, path)
    return True


def _restore_rejected(board: str, key: str) -> tuple[bool, str]:
    """恢复一条隔离样本：按 DatasetStore.append 同口径写回数据集 + 重建派生文本。"""
    hit = _find_rejected(board, key)
    if not hit:
        return False, "隔离样本不在了（可能已被恢复或删除）"
    path, raw = hit
    out_dir = os.path.dirname(path)
    fn = os.path.basename(path)
    stem = fn[: -len("_rejected.jsonl")] if fn.endswith("_rejected.jsonl") else fn[:-6]
    try:
        rec = json.loads((raw or "").strip())
    except Exception:                                # noqa: BLE001
        rec = None
    rec = rec if isinstance(rec, dict) else {}
    name = stem + ".jsonl"
    if rec.get("article") is not None:
        # 文章线：完整记录进 .meta，txt/<名>_articles.txt 由 _sync_article_txt 重建
        meta_dir = os.path.join(out_dir, ".meta")
        os.makedirs(meta_dir, exist_ok=True)
        main = os.path.join(meta_dir, name)
        sync = _article._trash_sync
    else:
        # 问答线：记录直接进顶层 jsonl，.meta/<名>_sft.txt 由 _sync_response_sft 重建
        main = os.path.join(out_dir, name)
        sync = _response._trash_sync
    with open(main, "a", encoding="utf-8", newline="\n") as f:
        f.write(raw.rstrip("\r\n") + "\n")
    _remove_rej_line(path, key)
    try:
        sync(out_dir, name)
    except Exception:                                # noqa: BLE001
        pass
    return True, ""


def _purge_rejected(board: str, key: str) -> bool:
    hit = _find_rejected(board, key)
    return bool(hit and _remove_rej_line(hit[0], key))


def list_items(query: dict[str, Any]) -> dict[str, Any]:
    """回收站全部条目：samples（文章/响应样本 + 隔离样本）+ files（数据集 / 模型权重整文件）。"""
    samples = _sample_rows() + _rejected_rows()
    _all = _file_rows()
    _weight_meta(_all)                        # 权重条目补训练方式（用户 2026-10-20）
    folders, files = _folder_rows(_all)
    return {"ok": True, "samples": samples, "files": files, "folders": folders,
            "total": len(samples) + len(files) + len(folders), "project": _root()}


def _targets(body: dict[str, Any]) -> list[dict[str, str]]:
    """targets 归一化成 [{kind, board, key}]；kind=file 时 board 为空。"""
    out: list[dict[str, str]] = []
    for t in (body or {}).get("targets") or []:
        if not isinstance(t, dict):
            continue
        key = str(t.get("key") or "").strip()
        if not key:
            continue
        kind = str(t.get("kind") or "sample").strip() or "sample"
        out.append({"kind": kind, "board": str(t.get("board") or ""), "key": key})
    return out


def _expand_folders(targets: list[dict[str, str]]) -> list[dict[str, str]]:
    """把 {kind:'folder', key:'folder:<名>'} 展开成它的成员文件（tid）目标。"""
    need = [t for t in targets if t["kind"] == "folder"]
    if not need:
        return targets
    try:
        items = _trash.file_items(_root())
    except Exception:                            # noqa: BLE001
        items = []
    out: list[dict[str, str]] = [t for t in targets if t["kind"] != "folder"]
    for t in need:
        fn = t["key"][7:] if t["key"].startswith("folder:") else t["key"]
        for e in items:
            if str(e.get("folder") or "") == fn:
                out.append({"kind": "file", "board": "", "key": str(e.get("tid") or "")})
    return out


def restore(body: dict[str, Any]) -> dict[str, Any]:
    """恢复所选：样本按原字节写回来源文件 + 重建派生文本；文件移回原路径。"""
    b = body or {}
    raw = _targets(b)
    # 文件夹条目：先记下「文件夹名 → 成员文件名」，还原文件后把文件夹本身也重建回来
    folder_plan: dict[str, list[str]] = {}
    try:
        _items = _trash.file_items(_root())
    except Exception:                            # noqa: BLE001
        _items = []
    for t in raw:
        if t["kind"] != "folder":
            continue
        fn = t["key"][7:] if t["key"].startswith("folder:") else t["key"]
        folder_plan[fn] = [str(e.get("name") or "") for e in _items
                           if str(e.get("folder") or "") == fn]
    targets = _expand_folders(raw)             # 文件夹条目 → 成员文件（用户 2026-10-20）
    if not targets:
        return {"ok": False, "error": "没有指定要恢复的条目"}
    restored = 0
    errors: list[str] = []
    for board, mod in _BOARDS:
        keys = [t["key"] for t in targets if t["kind"] == "sample" and t["board"] == board]
        if not keys:
            continue
        try:
            r = mod.trash_restore({"keys": keys})
        except Exception as exc:                 # noqa: BLE001
            errors.append("%s：%s: %s" % (board, type(exc).__name__, exc))
            continue
        if r.get("ok"):
            restored += int(r.get("restored") or 0)
            if r.get("skipped"):
                errors.append("%s：%s" % (board, "；".join(map(str, r["skipped"]))))
        else:
            errors.append("%s：%s" % (board, str(r.get("error") or "未知错误")))
    fkeys = [t["key"] for t in targets if t["kind"] == "file"]
    if fkeys:
        try:
            r = _trash.file_restore(_root(), fkeys)
        except Exception as exc:                 # noqa: BLE001
            errors.append("文件：%s: %s" % (type(exc).__name__, exc))
            r = {}
        if r.get("ok"):
            restored += int(r.get("restored") or 0)
            if r.get("skipped"):
                errors.append("文件：%s" % "；".join(map(str, r["skipped"])))
            # 回链仓库文件夹归属（用户 2026-10-20：删的是文件夹里的东西，恢复回原文件夹）
            try:
                import repo as repo_api
                by_folder: dict[str, list] = {}
                for it in (r.get("items") or []):
                    fn = str(it.get("folder") or "")
                    if fn:
                        by_folder.setdefault(fn, []).append(str(it.get("name") or ""))
                for fn, nms in by_folder.items():
                    repo_api.item_folder({"names": nms, "folder": fn, "project": _root()})
            except Exception:                    # noqa: BLE001
                pass
            # 恢复的是文件夹条目：文件夹本身也重建，成员放回去（用户 2026-10-20）
            for fn, nms in folder_plan.items():
                try:
                    import repo as _repo
                    _repo.folder_create({"name": fn, "project": _root()})
                    if nms:
                        _repo.item_folder({"names": nms, "folder": fn, "project": _root()})
                except Exception:                # noqa: BLE001
                    pass
        elif r:
            errors.append("文件：%s" % str(r.get("error") or "未知错误"))
    for t in [t for t in targets if t["kind"] == "rejected"]:
        try:
            ok, err = _restore_rejected(t["board"], t["key"])
        except Exception as exc:                 # noqa: BLE001
            ok, err = False, "%s: %s" % (type(exc).__name__, exc)
        if ok:
            restored += 1
        else:
            errors.append(err)
    return {"ok": restored > 0, "restored": restored, "errors": errors,
            "error": "；".join(errors) if (errors and not restored) else ""}


def purge(body: dict[str, Any]) -> dict[str, Any]:
    """彻底删除所选（永久删，不再留档）。"""
    b = body or {}
    targets = _expand_folders(_targets(b))     # 文件夹条目 → 成员文件（用户 2026-10-20）
    if not targets:
        return {"ok": False, "error": "没有指定要删除的条目"}
    purged = 0
    errors: list[str] = []
    for board, mod in _BOARDS:
        keys = [t["key"] for t in targets if t["kind"] == "sample" and t["board"] == board]
        if not keys:
            continue
        try:
            r = mod.trash_purge({"keys": keys})
        except Exception as exc:                 # noqa: BLE001
            errors.append("%s：%s: %s" % (board, type(exc).__name__, exc))
            continue
        if r.get("ok"):
            purged += int(r.get("purged") or 0)
        else:
            errors.append("%s：%s" % (board, str(r.get("error") or "未知错误")))
    fkeys = [t["key"] for t in targets if t["kind"] == "file"]
    if fkeys:
        try:
            r = _trash.file_purge(_root(), fkeys)
        except Exception as exc:                 # noqa: BLE001
            errors.append("文件：%s: %s" % (type(exc).__name__, exc))
            r = {}
        if r.get("ok"):
            purged += int(r.get("purged") or 0)
        elif r:
            errors.append("文件：%s" % str(r.get("error") or "未知错误"))
    for t in [t for t in targets if t["kind"] == "rejected"]:
        try:
            if _purge_rejected(t["board"], t["key"]):
                purged += 1
            else:
                errors.append("隔离样本不在了（可能已被恢复或删除）")
        except Exception as exc:                 # noqa: BLE001
            errors.append("%s: %s" % (type(exc).__name__, exc))
    return {"ok": purged > 0, "purged": purged, "errors": errors,
            "error": "；".join(errors) if (errors and not purged) else ""}


def clear(_body: dict[str, Any]) -> dict[str, Any]:
    """清空回收站（样本 + 文件全部永久删除，不进站）。"""
    purged = 0
    for _board, mod in _BOARDS:
        try:
            r = mod.trash_clear({})
            purged += int(r.get("purged") or 0) if r.get("ok") else 0
        except Exception:                        # noqa: BLE001
            pass
    try:
        r = _trash.file_purge(_root(), None)
        purged += int(r.get("purged") or 0) if r.get("ok") else 0
    except Exception:                            # noqa: BLE001
        pass
    return {"ok": True, "purged": purged}


def trash_file(body: dict[str, Any]) -> dict[str, Any]:
    """把一个整文件移入回收站（数据集文件 kind=dataset / 模型权重 kind=weights）。"""
    b = body or {}
    path = str(b.get("path") or "").strip()
    kind = str(b.get("kind") or "").strip()
    if not path:
        return {"ok": False, "error": "缺少文件路径"}
    try:
        return _trash.file_add(_root(), path, kind)
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def content(body: dict[str, Any]) -> dict[str, Any]:
    """查看内容（用户 2026-10-16）：整文件给 .trash/files 本体头部（文本原文 / 二进制给信息+HEX 头）；
    隔离样本给原始行。"""
    b = body or {}
    kind = str(b.get("kind") or "")
    key = str(b.get("key") or "")
    if kind == "rejected":
        hit = _find_rejected(str(b.get("board") or ""), key)
        if not hit:
            return {"ok": False, "error": "找不到这条样本"}
        return {"ok": True, "text": hit[1].rstrip("\r\n"), "binary": False}
    if not key:
        return {"ok": False, "error": "缺少条目 id"}
    name = ""
    for e in _trash.file_items(_root()):
        if str(e.get("tid") or "") == key:
            name = str(e.get("name") or "")
            break
    if not name:
        return {"ok": False, "error": "回收站里没有这个文件"}
    path = os.path.join(_trash.ftrash_files_dir(_root()), key + "__" + name)
    if not os.path.isfile(path):
        return {"ok": False, "error": "文件本体不在了"}
    try:
        with open(path, "rb") as f:
            head = f.read(262144)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    if b"\x00" in head[:8192]:                       # 二进制（.pt 等）：给信息 + HEX 头
        return {"ok": True, "binary": True, "name": name, "size_bytes": os.path.getsize(path),
                "hex": binascii.hexlify(head[:256]).decode("ascii")}
    return {"ok": True, "binary": False, "name": name,
            "text": head.decode("utf-8", "replace").rstrip("\r\n")}


def register(router) -> None:
    router.get("/api/recycle/list", list_items)
    router.post("/api/recycle/restore", restore)
    router.post("/api/recycle/purge", purge)
    router.post("/api/recycle/clear", clear)
    router.post("/api/recycle/trash_file", trash_file)
    router.post("/api/recycle/content", content)
