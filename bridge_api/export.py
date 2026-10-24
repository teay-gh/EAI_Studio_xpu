"""bridge_api/export.py —— 「导出模型」独立页的后端。

  GET  /api/export/state   列出可导出的模型文件（config.train 的真实产物路径 + 工程 checkpoints/ 下其它权重）
  POST /api/export/run     把选中的文件复制到用户挑的文件夹，逐项校验大小与 sha256

铁律：
  * 只读工程/引擎侧，只往 target_dir 写；绝不改工程或引擎里的任何文件。
  * 绝不返回编造的数据；源文件不存在、目标不可写等一律照实返回错误。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time

import _host
import repo as _repo            # 训练方式真值链与仓库同口径（bridge 已把目录挂上）

# config.train 里的训练产物：key -> (中文名, 配置字段)
OUTPUT_KEYS = [
    ("pretrain", "预训练权重", "pretrain_out"),
    ("continue", "继续预训练权重", "continue_out"),
    ("sft", "指令微调权重", "sft_out"),
    ("lora", "LoRA 权重", "lora_out"),
]

WEIGHT_EXT = (".pt", ".pth", ".safetensors", ".bin")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _train_labels(name: str, hist_all: dict) -> tuple:
    """训练方式标注（与仓库 repo._weight_items 同一条真值链，用户 2026-10-21：导出页缺标注）：

    训练溯源 data/_weight_hist.json 里的中文训练方式 → 检查点 <基名>_Check<k> 沿用基座历史
    → 文件名推断兜底。返回 (中文训练方式或空, 推断 method, 展示用短标签)。"""
    hist = hist_all.get(name)
    if not hist:
        m = _repo.CHECK_FOLDER_RE.match(name)
        if m:
            cand = str(m.group("base") or "") + (m.group("lora") or "") + os.path.splitext(name)[1]
            hist = hist_all.get(cand)
    hist = [r for r in (hist or []) if isinstance(r, dict)]
    m_cn = str((hist[-1].get("m") if hist else "") or "").strip()
    method = _repo._guess_method(name)
    return m_cn, method, (m_cn or _repo.METHOD_LABELS.get(method, "其他"))


def _item(key: str, label: str, path: str, source: str, source_kind: str = "config") -> dict:
    """source_kind（用户 2026-10-23）：来源的结构化标记，中文 source 只给中文界面用；
    英文界面按 source_kind 自己拼文案（工程 checkpoints/ → project checkpoints/）。"""
    abspath = os.path.abspath(path) if path else ""
    exists = bool(abspath) and os.path.isfile(abspath)
    return {
        "key": key,
        "label": label,
        "path": abspath,
        "name": os.path.basename(abspath) if abspath else "",
        "exists": exists,
        "size": os.path.getsize(abspath) if exists else 0,
        "mtime": int(os.path.getmtime(abspath)) if exists else 0,
        "source": source,
        "source_kind": source_kind,
    }


def _lora_paths(train: dict, root: str) -> tuple:
    """LoRA 产物口径（同 train_lora._lora_paths）：适配器 = lora_out 去后缀 + .lora.pt。

    2026-10-17（用户）：导出只做「复制文件」，不再提供 LoRA 合并——合并统一在「LoRA 合并」页做。"""
    def _abs(p: str) -> str:
        p = str(p or "").strip()
        return p if os.path.isabs(p) else os.path.join(root, p)
    out = _abs(str(train.get("lora_out") or "").strip())
    adapter = (os.path.splitext(out)[0] + ".lora.pt") if out else ""
    return out, adapter


def _collect() -> dict:
    """真实清单：先按 config.train 的产物路径，再补工程 checkpoints/ 里其它权重。

    LoRA 项指适配器文件（*.lora.pt）——训练不合并、导出也只复制（用户 2026-10-17：
    合并功能已从导出页删除，合并统一走「LoRA 合并」页）。
    """
    cfg = _host.load_cfg()
    train = cfg.get("train") or {}
    root = _host.project_root(cfg)
    try:
        hist_all = _host.read_weight_hist(root) or {}
    except Exception:                              # noqa: BLE001
        hist_all = {}
    items: list = []
    seen: set = set()

    def annotate(it: dict, kind: str) -> dict:
        """标注对齐仓库真值链（用户 2026-10-21：导出页缺很多没标注）：
        有训练溯源 → 「中文训练方式+权重」；否则 config 项保留原名、checkpoints 项按推断给「XX权重」。"""
        m_cn, method, m_label = _train_labels(it["name"], hist_all)
        it["method"] = method
        it["method_label"] = m_label
        if kind != "adapter":
            if m_cn:
                it["label"] = m_cn + "权重"
            elif kind == "file":
                it["label"] = _repo.WEIGHT_TITLE.get(method, "其他权重")
        return it
    _lora_out, lora_adapter = _lora_paths(train, root)
    for key, label, field in OUTPUT_KEYS:
        if key == "lora":
            # 训练产物 = 适配器；合并是导出时的可选项
            if not lora_adapter:
                continue
            it = annotate(_item(key, "LoRA 适配器", lora_adapter, "config.train.lora_out（.lora.pt）"), "adapter")
            seen.add(os.path.normcase(os.path.abspath(lora_adapter)))
            if it["exists"]:            # 没有的权重不显示（用户 2026-10-16）
                items.append(it)
            continue
        path = str(train.get(field) or "").strip()
        if not path:
            continue
        it = annotate(_item(key, label, path, "config.train." + field), "config")
        seen.add(os.path.normcase(os.path.abspath(path)))
        if it["exists"]:                # 没有的权重不显示（用户 2026-10-16）
            items.append(it)
    ckpt = os.path.join(root, "checkpoints")
    if os.path.isdir(ckpt):
        for fn in sorted(os.listdir(ckpt)):
            if not fn.lower().endswith(WEIGHT_EXT):
                continue
            path = os.path.join(ckpt, fn)
            if os.path.normcase(os.path.abspath(path)) in seen:
                continue
            seen.add(os.path.normcase(os.path.abspath(path)))
            items.append(annotate(_item("file:" + fn, fn, path, "工程 checkpoints/",
                                        "project-checkpoints"), "file"))
    return {"ok": True, "project": root, "items": items,
            "default_target": os.path.join(root, "exports"),
            "engine_dir": _host.IDE_DIR}


def register(router) -> None:
    def state(query: dict) -> dict:
        return _collect()

    def run(body: dict) -> dict:
        target = str(body.get("target_dir") or "").strip()
        keys = body.get("keys") or []
        if not target:
            return {"ok": False, "error": "没有选择导出文件夹"}
        target = os.path.abspath(target)
        if not os.path.isdir(target):
            return {"ok": False, "error": "导出文件夹不存在：%s" % target}
        if not isinstance(keys, list) or not keys:
            return {"ok": False, "error": "没有选择要导出的文件"}

        data = _collect()
        want = [it for it in data["items"] if it["key"] in keys]
        if not want:
            return {"ok": False, "error": "选中的条目在清单里已不存在，请重新扫描"}

        copied, errors, skipped = [], [], []
        for it in want:
            src = it["path"]
            dst = os.path.join(target, it["name"])
            if not it["exists"]:
                errors.append({"key": it["key"], "name": it["name"], "error": "源文件不存在"})
                continue
            if os.path.normcase(src) == os.path.normcase(os.path.abspath(dst)):
                skipped.append({"key": it["key"], "name": it["name"], "note": "源与目标同一路径，已跳过"})
                continue
            t0 = time.time()
            try:
                shutil.copy2(src, dst)
            except Exception as exc:                       # 权限 / 占用 / 磁盘满
                errors.append({"key": it["key"], "name": it["name"],
                               "error": "%s: %s" % (type(exc).__name__, exc)})
                continue
            src_size = os.path.getsize(src)
            dst_size = os.path.getsize(dst)
            src_hash = _sha256(src)
            dst_hash = _sha256(dst)
            copied.append({
                "key": it["key"],
                "name": it["name"],
                "path": dst,
                "size": dst_size,
                "size_ok": src_size == dst_size,
                "sha256_ok": src_hash == dst_hash,
                "sha256": dst_hash[:12],
                "ms": int((time.time() - t0) * 1000),
            })
            _host.model_log(None, "导出", it["name"], dst)   # 导出写日志：op=「导出」（2026-10-21）
        return {"ok": not errors and bool(copied), "target_dir": target,
                "copied": copied, "errors": errors, "skipped": skipped,
                "count": len(copied)}

    router.get("/api/export/state", state)
    router.post("/api/export/run", run)
