"""bridge_api/train_lora_merge.py —— 「LoRA 合并」页后端（用户 2026-10-17：训练中心里、量化下面）。

把 LoRA 适配器（*.lora.pt）合并进基座权重，落成完整 ckpt（chat / 导出页可直接用）。
实现复用 engine/core/lora.py 的 merge_adapter_file（与「导出模型」勾「合并」同一条链路），
本模块只做：路径解析、入参校验、产物状态回读。

   GET  /api/train_lora_merge/config   基座 / 适配器 / 输出（含存在性与大小）
   POST /api/train_lora_merge/save     {base|adapter|out} 写 config.train.lora_merge_*
   POST /api/train_lora_merge/run      {base, adapter, out} 真合并（同步，几秒）
"""
from __future__ import annotations

import json
import os

import _host

K_BASE = "lora_merge_base"
K_ADAPTER = "lora_merge_adapter"
K_OUT = "lora_merge_out"


def _fact(path: str) -> dict:
    path = path or ""
    try:
        ok = bool(path) and os.path.isfile(path)
        return {"path": path, "exists": ok,
                "size_bytes": os.path.getsize(path) if ok else 0,
                "size_human": _human(os.path.getsize(path)) if ok else "—",
                "mtime": os.path.getmtime(path) if ok else None}
    except OSError:
        return {"path": path, "exists": False, "size_bytes": 0, "size_human": "—", "mtime": None}


def _human(n: int) -> str:
    """体积文案（用户 2026-10-18）：到三位数就不带小数，269.0 MB → 269 MB。"""
    n = int(n or 0)
    if n >= 1024 * 1024 * 1024:
        f = n / (1024.0 ** 3)
        return ("%.1f GB" if f < 100 else "%.0f GB") % f
    if n >= 1024 * 1024:
        f = n / (1024.0 ** 2)
        return ("%.1f MB" if f < 100 else "%.0f MB") % f
    if n >= 1024:
        return "%.0f KB" % (n / 1024.0)
    return "%d B" % n


def _out_path(raw: str, proj: str) -> str:
    """输出：给的是裸名字就放 <工程>/checkpoints（同其它训练页的权重目录）。"""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    if not os.path.isabs(raw) and "/" not in raw and "\\" not in raw:
        name = raw if raw.lower().endswith(".pt") else raw + ".pt"
        return os.path.join(proj, "checkpoints", name)
    return _host.in_project(raw, proj)


def _defaults() -> dict:
    """未配置时的合理起点：基座 = 指令微调/预训练产物；适配器 = lora_out 同名的 *.lora.pt。"""
    cfg = _host.load_cfg()
    train = cfg.get("train") or {}
    proj = _host.project_root(cfg)
    base = train.get(K_BASE) or train.get("sft_init_from") or train.get("pretrain_out") or ""
    adapter = train.get(K_ADAPTER) or ""
    if not adapter:
        lora_out = str(train.get("lora_out") or "")
        if lora_out:
            adapter = os.path.splitext(lora_out)[0] + ".lora.pt"
    out = train.get(K_OUT) or ""
    return {
        "base": _host.in_project(base, proj) if base else "",
        "adapter": _host.in_project(adapter, proj) if adapter else "",
        "out": _out_path(out, proj),
    }


def config(_query: dict) -> dict:
    d = _defaults()
    return {
        "ok": True,
        "base": d["base"], "adapter": d["adapter"], "out": d["out"],
        "facts": {"base": _fact(d["base"]), "adapter": _fact(d["adapter"]), "out": _fact(d["out"])},
        "checkpoints": os.path.join(_host.project_root(), "checkpoints"),
    }


def save(body: dict) -> dict:
    body = body or {}
    cfg = _host.load_cfg()
    train = cfg.setdefault("train", {})
    proj = _host.project_root(cfg)
    saved = {}
    for key, field in ((K_BASE, "base"), (K_ADAPTER, "adapter"), (K_OUT, "out")):
        if field in body:
            val = str(body.get(field) or "").strip()
            train[key] = _out_path(val, proj) if (val and field == "out") else (_host.in_project(val, proj) if val else "")
            saved[field] = train[key]
    _host.save_cfg(cfg)
    return {"ok": True, "saved": saved}


def run(body: dict) -> dict:
    body = body or {}
    cfg = _host.load_cfg()
    proj = _host.project_root(cfg)
    d = _defaults()
    base = _host.in_project(str(body.get("base") or d["base"] or ""), proj)
    adapter = _host.in_project(str(body.get("adapter") or d["adapter"] or ""), proj)
    out_raw = str(body.get("out") or "").strip()
    if not base:
        return {"ok": False, "error": "请先选择基座权重。"}
    if not adapter:
        return {"ok": False, "error": "请先选择 LoRA 适配器。"}
    if not out_raw:
        return {"ok": False, "error": "请先填写输出权重名。"}
    if not os.path.isfile(base):
        return {"ok": False, "error": "基座权重不存在：%s" % base}
    if not os.path.isfile(adapter):
        return {"ok": False, "error": "LoRA 适配器不存在：%s" % adapter}
    out = _out_path(out_raw, proj)
    if not out.lower().endswith(".pt"):
        out += ".pt"
    if os.path.abspath(out) == os.path.abspath(base) or os.path.abspath(out) == os.path.abspath(adapter):
        return {"ok": False, "error": "输出名不能与基座或适配器同名（会覆盖源文件）。"}
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": "创建输出目录失败：%s" % exc}
    try:
        _host.ensure_ide_path()
        from core import lora as lora_lib           # type: ignore
        res = lora_lib.merge_adapter_file(base, adapter, out)
    except Exception as exc:                        # noqa: BLE001
        # 2026-10-23：结构性不兼容（合并必坏）用 RuntimeError 抛，正文本来就是给人看的，
        # 不再加「RuntimeError:」前缀；其它异常保留类型名便于排查
        msg = str(exc) if isinstance(exc, RuntimeError) else "%s: %s" % (type(exc).__name__, exc)
        return {"ok": False, "error": msg}
    fact = _fact(out)
    # 合并产物也要在总览日志里叫「LoRA 合并」（用户 2026-10-22）：与 core/train.py 的
    # note_method 同一份 logs/_train_methods.json，免得日志/卡片退回按文件名猜成「LoRA 微调」
    try:
        logs = os.path.join(proj, "logs")
        os.makedirs(logs, exist_ok=True)
        fp = os.path.join(logs, "_train_methods.json")
        data: dict = {}
        if os.path.isfile(fp):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                data = {}
        data[os.path.basename(out)] = "LoRA 合并"
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    # 训练溯源（用户 2026-10-23）：合并产物也记一条，「初始权重」= 基座权重
    try:
        _host.record_weight_hist(proj, out, "LoRA 合并", base)
    except Exception:                               # noqa: BLE001
        pass
    return {"ok": True, "out": out, "merged": fact,
            "size_human": fact.get("size_human"), "detail": res,
            "warning": str(res.get("base_mismatch") or "")}


def register(router) -> None:
    router.get("/api/train_lora_merge/config", config)
    router.post("/api/train_lora_merge/save", save)
    router.post("/api/train_lora_merge/run", run)
