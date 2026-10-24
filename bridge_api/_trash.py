"""数据管理 · 回收站（article / response 两个板块共用实现）。

口径（2026-10-14，用户：「删除选择的数据先放回收站，如果全部删除则不放回收站」）：
- 「删除所选」= 移入回收站：逐条把**原始行**（含原换行符）记进 <板块>/.trash/trash.jsonl，可恢复；
- 「清空全部」= 永久删除，不进回收站（由各板块的 clear_all 直接删文件）；
- 回收站目录以 . 开头 → core.data 扫描器 / 语料合并都跳过它，绝不会混进训练数据。

每条回收站记录一行 JSON：{tid, deleted_at, src, key, raw, rec}
- tid = 回收站内的唯一 id（前端选择项用它）；key = 原始行指纹（与删除口径一致）
- raw = 该行原始字节文本（带原换行符）——恢复时逐字节写回；rec = 解析出的记录（只供展示）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from typing import Any, Callable

_TID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}$")


def _log_exc(tag: str, exc: object, **fields: object) -> None:
    """兜底异常落盘（2026-10-24）：转发到 _host._log_exc → core/errlog.py。永不抛。"""
    try:
        import _host                                  # noqa: PLC0415  同目录模块，避免顶层循环导入
        _host._log_exc(tag, exc, **fields)
    except Exception:                                 # noqa: BLE001
        pass


def trash_dir(out_dir: str) -> str:
    return os.path.join(out_dir, ".trash")


def trash_path(out_dir: str) -> str:
    return os.path.join(trash_dir(out_dir), "trash.jsonl")


def _load(out_dir: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with open(trash_path(out_dir), "r", encoding="utf-8") as f:
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


def _save(out_dir: str, entries: list[dict[str, Any]]) -> None:
    os.makedirs(trash_dir(out_dir), exist_ok=True)
    path = trash_path(out_dir)
    tmp = path + ".eai-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def add(out_dir: str, src: str, raw_lines: list[str]) -> int:
    """删下来的原始行进回收站（raw 带原换行符，恢复时逐字节写回）。返回入站条数。"""
    entries = _load(out_dir)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    for raw in raw_lines:
        s = (raw or "").strip()
        if not s:
            continue
        try:
            rec = json.loads(s)
        except Exception:
            rec = None
        entries.append({
            "tid": "t%d-%03d" % (int(time.time() * 1000), n),
            "deleted_at": now,
            "src": os.path.basename(str(src or "")),
            "key": hashlib.sha1(s.encode("utf-8")).hexdigest()[:12],
            "raw": raw,
            "rec": rec if isinstance(rec, dict) else None,
        })
        n += 1
    if n:
        _save(out_dir, entries)
    return n


def items(out_dir: str) -> list[dict[str, Any]]:
    """回收站条目（最新删除的在前）。"""
    return list(reversed(_load(out_dir)))


def _keys_ok(keys: Any) -> list[str]:
    out = []
    for k in (keys or []):
        s = str(k or "").strip()
        if _TID_RE.match(s):
            out.append(s)
    return out


def _split(out_dir: str, keys: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    want = set(keys)
    entries = _load(out_dir)
    hit = [e for e in entries if str(e.get("tid")) in want]
    return hit, entries, want


def purge(out_dir: str, keys: Any) -> dict[str, Any]:
    """彻底删除回收站条目；keys 为空 = 清空整个回收站。永久删，不再留档。"""
    entries = _load(out_dir)
    if keys is None:
        n = len(entries)
        kept: list[dict[str, Any]] = []
    else:
        want = set(_keys_ok(keys))
        if not want:
            return {"ok": False, "error": "没有指定要删除的回收站条目"}
        kept = [e for e in entries if str(e.get("tid")) not in want]
        n = len(entries) - len(kept)
    if not n:
        return {"ok": False, "error": "回收站里没有这些条目（可能已被删除或恢复）"}
    _save(out_dir, kept)
    return {"ok": True, "purged": n}


def restore(out_dir: str, keys: Any, src_dir: str,
            sync: Callable[[str, str], None]) -> dict[str, Any]:
    """恢复：原始行按原字节追加回来源文件（文件没了就重建），重建派生文本后出站。

    追加到文件末尾（回收站不记原行位置；恢复后该行可能不在原来的位置，内容逐字节一致）。
    """
    want = set(_keys_ok(keys))
    if not want:
        return {"ok": False, "error": "没有指定要恢复的样本"}
    hit, entries, _ = _split(out_dir, want)
    if not hit:
        return {"ok": False, "error": "回收站里没有这些样本（可能已被恢复或删除）"}

    os.makedirs(src_dir, exist_ok=True)
    files: list[str] = []
    done: set[str] = set()
    restored = 0
    for e in hit:
        tid = str(e.get("tid"))
        name = os.path.basename(str(e.get("src") or ""))
        raw = str(e.get("raw") or "")
        if (not name.endswith(".jsonl")) or ("rejected" in name) or (".." in name) or (not raw.strip()):
            continue
        path = os.path.join(src_dir, name)
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(src_dir):
            continue
        # 追加前保证前一行有换行收尾（老文件可能没有尾换行），否则会和末行粘在一起
        need_nl = False
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                need_nl = f.read(1) not in (b"\n", b"\r")
        if not raw.endswith("\n"):
            raw += "\n"
        with open(path, "a", encoding="utf-8", newline="") as f:
            if need_nl:
                f.write("\n")
            f.write(raw)
        restored += 1
        done.add(tid)
        if name not in files:
            files.append(name)
    if not restored:
        return {"ok": False, "error": "没有可恢复的样本"}
    for name in files:
        try:
            sync(out_dir, name)
        except Exception as exc:                   # noqa: BLE001
            _log_exc("trash.restore_sync", exc, file=name)   # 恢复后同步失败（2026-10-24）
            pass
    _save(out_dir, [e for e in entries if str(e.get("tid")) not in done])
    return {"ok": True, "restored": restored, "files": files}


# ---------------------------------------------------------------- 文件级回收站
# （2026-10-14 用户口径：「回收站允许接收数据集、模型权重」）
# 整文件进工程根 <proj>/.trash/：files.jsonl 记账 + files/ 存本体（改名 <tid>__<原名>）。
# kind = dataset（数据集文件）/ weights（模型权重文件）。
# .trash 以点开头 → 数据扫描 / 语料合并一律跳过，绝不会混进训练数据。

def ftrash_dir(proj: str) -> str:
    return os.path.join(proj, ".trash")


def ftrash_files_dir(proj: str) -> str:
    return os.path.join(ftrash_dir(proj), "files")


def ftrash_path(proj: str) -> str:
    return os.path.join(ftrash_dir(proj), "files.jsonl")


def _fload(proj: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with open(ftrash_path(proj), "r", encoding="utf-8") as f:
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


def _fsave(proj: str, entries: list[dict[str, Any]]) -> None:
    os.makedirs(ftrash_dir(proj), exist_ok=True)
    path = ftrash_path(proj)
    tmp = path + ".eai-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _under_root(proj: str, path: str) -> bool:
    """path 必须在工程根之内（防 ../、别的盘），否则拒绝入站。"""
    try:
        ap = os.path.abspath(path)
        root = os.path.abspath(proj)
        return os.path.commonpath([ap, root]) == root and ap != root
    except ValueError:
        return False


def file_add(proj: str, path: str, kind: str, folder: str = "") -> dict[str, Any]:
    """把一个整文件移进回收站（数据集 / 模型权重）。返回 {ok, item}。

    folder = 删除时它在仓库里所属的文件夹名（用户 2026-10-20：垃圾桶按文件夹分组显示）。"""
    ap = os.path.abspath(str(path or ""))
    if not _under_root(proj, ap):
        return {"ok": False, "error": "只能回收工程目录内的文件"}
    if not os.path.isfile(ap):
        return {"ok": False, "error": "文件不存在：%s" % ap}
    kind = str(kind or "").strip()
    if kind not in ("dataset", "weights"):
        return {"ok": False, "error": "未知的回收类型：%s" % (kind or "（空）")}
    name = os.path.basename(ap)
    tid = "f%d-%03d" % (int(time.time() * 1000), len(_fload(proj)) % 1000)
    dst_dir = ftrash_files_dir(proj)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, tid + "__" + name)
    size = os.path.getsize(ap)
    shutil.move(ap, dst)
    entry = {
        "tid": tid,
        "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "name": name,
        "orig_path": ap,
        "size_bytes": size,
        "folder": str(folder or ""),
    }
    entries = _fload(proj)
    entries.append(entry)
    _fsave(proj, entries)
    return {"ok": True, "item": entry}


def file_items(proj: str) -> list[dict[str, Any]]:
    """文件回收站条目（最新删除的在前）。"""
    return list(reversed(_fload(proj)))


def file_restore(proj: str, keys: Any) -> dict[str, Any]:
    """恢复：把回收站里的整文件移回原路径。原路径已有同名文件则跳过并照实报错。"""
    want = set(_keys_ok(keys))
    if not want:
        return {"ok": False, "error": "没有指定要恢复的文件"}
    entries = _fload(proj)
    hit = [e for e in entries if str(e.get("tid")) in want]
    if not hit:
        return {"ok": False, "error": "回收站里没有这些文件（可能已被恢复或删除）"}
    src_dir = ftrash_files_dir(proj)
    restored = 0
    skipped: list[str] = []
    done: set[str] = set()
    restored_items: list[dict[str, Any]] = []   # 恢复成功的 {name, folder}（回链文件夹归属，2026-10-20）
    for e in hit:
        tid = str(e.get("tid"))
        name = str(e.get("name") or "")
        dst = str(e.get("orig_path") or "")
        src = os.path.join(src_dir, tid + "__" + name)
        if (not name) or (not dst) or (not _under_root(proj, dst)) or (not os.path.isfile(src)):
            skipped.append(name or tid)
            continue
        if os.path.exists(dst):
            skipped.append(name + "（原路径已有同名文件）")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        restored += 1
        done.add(tid)
        restored_items.append({"name": name, "folder": str(e.get("folder") or "")})
    _fsave(proj, [e for e in entries if str(e.get("tid")) not in done])
    return {"ok": True, "restored": restored, "skipped": skipped, "items": restored_items}


def file_purge(proj: str, keys: Any) -> dict[str, Any]:
    """彻底删除文件回收站条目（连本体一起删）。keys 为 None = 清空。"""
    entries = _fload(proj)
    if keys is None:
        hit = entries
    else:
        want = set(_keys_ok(keys))
        if not want:
            return {"ok": False, "error": "没有指定要删除的回收站条目"}
        hit = [e for e in entries if str(e.get("tid")) in want]
        if not hit:
            return {"ok": False, "error": "回收站里没有这些文件（可能已被恢复或删除）"}
    src_dir = ftrash_files_dir(proj)
    done = {str(e.get("tid")) for e in hit}
    for e in hit:
        name = str(e.get("name") or "")
        src = os.path.join(src_dir, str(e.get("tid")) + "__" + name)
        if name and os.path.isfile(src):
            try:
                os.remove(src)
            except OSError as exc:
                _log_exc("trash.purge_file", exc, path=src)  # 彻底删除失败（2026-10-24）
                pass
    _fsave(proj, [e for e in entries if str(e.get("tid")) not in done])
    return {"ok": True, "purged": len(done)}
