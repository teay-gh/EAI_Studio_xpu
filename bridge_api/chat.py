"""对话验证页（导航键 chat）的后端接口 —— 已接真引擎。

契约见 bridge_api/_template.py：模块暴露 register(router)，bridge.py 自动发现。

真实能力（全部复用 ENGINE_DIR 自己的引擎，见 bridge_api/_host.py，不另写一套）：
  * POST /api/chat/load      —— 真加载工程权重（torch.load + core.transformer.GPT），
                                用 _host.start_task 起后台线程（torch 加载慢），边加载边发进度事件；
                                不传 path 时按 Tk 版语义：优先 sft_out（微调权重），
                                缺失回退 pretrain_out（预训练权重），都没有就如实报错。
  * POST /api/chat/generate  —— 用已加载模型**真生成**：逐 token 采样，边生成边发 token 增量事件
                                （可在 /api/chat/status 里看到流式输出与最终文本）。
  * POST /api/chat/stop      —— 真停止生成（_host.stop_task 触发线程内的停止事件）。
  * POST /api/chat/clear     —— 清空当前会话（Tk 版语义：只清当前对话，不删磁盘历史）。
  * GET  /api/chat/status    —— 模型状态 + 加载/生成两个线程任务的增量事件（since 游标）。
  * GET  /api/chat/history   —— 读各工程 chats/*.json（与 core/chat_history.py 落盘格式一致；
                                当前工程直接走 core.chat_history.ChatHistory.list_all）。
  * GET  /api/chat/config    —— 读 config.json 里与对话相关的真实参数与权重路径。
  * POST /api/chat/send      —— /api/chat/generate 的兼容别名。

铁律：不改 config.json（生成参数只读）；不删历史文件（Tk 版对话页没有删除语义）。
      torch 一律懒 import（模块顶层不 import，避免桥启动变重）。
      出错照实返回 {"error": ...}，绝不返回编造的假数据。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional

import _host                                     # 引擎宿主（同目录，bridge_api 已在 sys.path）

# ----------------------------------------------------------------- 路径 / 常量

HERE = os.path.dirname(os.path.abspath(__file__))            # ...\EAI_Studio\bridge_api
WEB_DIR = os.path.dirname(HERE)                              # ...\EAI_Studio
EAI_ROOT = os.path.dirname(WEB_DIR)                          # ...\EAI
ENGINE_DIR = os.path.join(WEB_DIR, "engine")                # 2026-10-23：不再假设目录名叫 EAI_Studio，改名/换盘都成立
CONFIG_PATH = os.path.join(ENGINE_DIR, "config.json")
# 线程任务名（_host.start_task / task_status 用同一份名字）
LOAD_TASK = "chat:load"
GEN_TASK = "chat:generate"
LOAD_GROUP = "chat_load"
GEN_GROUP = "chat_generate"

# 采样参数（与 Tk 版 pages/chat.py::_generate 完全一致）
GEN_TEMPERATURE = 0.8
GEN_TOP_K = 40
GEN_REPETITION_PENALTY = 1.15   # 2026-10-19：原来 1.0=关闭，小模型会原地复读

# ----------------------------------------------------------------- 运行时状态

_LOCK = threading.Lock()
_MODEL: Any = None                     # 已加载的 GPT 实例
_STOI: dict = {}                       # 字符 → id
_ITOS: dict = {}                       # id → 字符
_DEVICE = "cpu"
_INFO: dict = {"loaded": False}        # 模型元信息（供 /status 透出）
_RUNNING = False                      # 「开始」之后才允许对话（用户 2026-10-18）
_HIST: Any = None                      # core.chat_history.ChatHistory 单例
_SESSION_ID: Optional[str] = None      # 当前会话 id（持久化用）
_MESSAGES: list = []                   # 当前会话消息（持久化用）


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _import_torch(emit=None) -> Any:
    """懒 import torch（首次可能十来秒）。"""
    if emit is not None:
        emit("progress", {"stage": "导入 torch…"})
    import torch  # noqa: PLC0415  故意延迟到任务线程里
    return torch


def _pick_device(torch: Any, requested: Any) -> str:
    """推理设备：显式要求且可用就用；auto（或回退）= CUDA → XPU → MPS → CPU，与 core/train.py 一致。"""
    req = str(requested or "auto").strip().lower()
    if req not in ("auto", "cpu", "cuda", "xpu", "mps"):
        req = "auto"
    try:
        avail = {"cuda": bool(torch.cuda.is_available()),
                 "xpu": bool(getattr(torch, "xpu", None) and torch.xpu.is_available()),
                 "mps": bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()),
                 "cpu": True}
    except Exception:                              # noqa: BLE001
        avail = {"cpu": True}
    if req != "auto" and avail.get(req):
        return req
    for name in ("cuda", "xpu", "mps", "cpu"):
        if avail.get(name):
            return name
    return "cpu"


# ----------------------------------------------------------------- 配置 / 工程

def _load_config() -> dict:
    """读 config.json；缺失/损坏返回 {}（不抛）。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:                              # noqa: BLE001
        return {}


def _resolve_project(cfg: dict) -> str:
    """当前工程：config.app.recent_projects[0]（存在才用）。

    2026-10-21 撤兜底：没有工程直接报「请先创建工程」，不再回落默认工程。
    """
    recent = (cfg.get("app") or {}).get("recent_projects") or []
    if isinstance(recent, list):
        for item in recent[:1]:   # 只认当前工程（2026-10-23）：不回落到第 2 个，防工程串
            if isinstance(item, str) and item and _host.is_project_dir(item):
                return os.path.abspath(item)
    raise RuntimeError("请先创建工程")   # 撤兜底（2026-10-21）


def _candidate_roots(cfg: dict) -> list:
    """候选工程根：当前工程优先，其次其余最近工程，最后默认工程（去重）。"""
    roots: list = []

    def add(p) -> None:
        if not p:
            return
        p = os.path.abspath(p)
        if p not in roots:
            roots.append(p)

    add(_resolve_project(cfg))
    recent = (cfg.get("app") or {}).get("recent_projects") or []
    if isinstance(recent, list):
        for item in recent:
            if isinstance(item, str) and os.path.isdir(item):
                add(item)
    return roots


def _weight_paths(cfg: Optional[dict] = None) -> dict:
    """解析权重路径：sft_out（微调） / pretrain_out（预训练） / 工程根。"""
    cfg = cfg if cfg is not None else _host.load_cfg()
    proj = _host.project_root(cfg)
    train = cfg.get("train") or {}
    sft = _host.in_project(str(train.get("sft_out") or "checkpoints/eproai_model.pt"), proj)
    pre = _host.in_project(str(train.get("pretrain_out") or "checkpoints/eproai_pretrain.pt"), proj)
    return {"project": proj, "sft": sft, "pretrain": pre}


# ----------------------------------------------------------------- 历史记录

def _history_store():
    """core.chat_history.ChatHistory 单例，绑定到当前工程（与 App / 助手同一套）。

    2026-10-18：原来只在首次调用时 bind 一次，切工程后仍读旧工程的 chats/ —— 现在每次都核对工程根。
    """
    global _HIST
    try:
        _host.ensure_ide_path()
        root = os.path.abspath(_host.project_root())
        if _HIST is None:
            from core.chat_history import ChatHistory  # type: ignore
            _HIST = ChatHistory()
        if os.path.abspath(getattr(_HIST, "_project_root", "") or "") != root:
            _HIST.bind_project(root)
        return _HIST
    except Exception:                              # noqa: BLE001
        return None


def _is_panel(conv: dict) -> bool:
    """AI 面板（内置助手）的会话：source=assistant。

    用户 2026-10-17：「AI 窗口的对话为什么在这」—— 面板与对话验证页共用 core.chat_history 一个库，
    但两边各看各的：本页（对话验证）只列自己产生的会话，面板历史由 /api/assistant/history 自己过滤。
    """
    return str((conv or {}).get("source") or "").strip().lower() == "assistant"


def _conv_item(conv: dict, project_name: str, path: str) -> dict:
    msgs = [
        {"role": str(m.get("role") or ""), "content": str(m.get("content") or "")}
        for m in (conv.get("messages") or [])
        if isinstance(m, dict)
    ]
    return {
        "id": str(conv.get("id") or ""),
        "title": conv.get("title") or "新对话",
        "created": conv.get("created") or "",
        "updated": conv.get("updated") or conv.get("created") or "",
        "count": len(msgs),
        "project": project_name,
        "path": path,
        "messages": msgs,
    }


def _read_convs(chats_dir: str, project_name: str, errors: list) -> list:
    """读一个工程的 chats/*.json（core/chat_history.py 的落盘格式）。"""
    out: list = []
    if not os.path.isdir(chats_dir):
        return out
    for name in sorted(os.listdir(chats_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(chats_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:                    # noqa: BLE001
            errors.append(f"{path}: {exc}")
            continue
        if not isinstance(data, dict) or _is_panel(data):
            continue
        out.append(_conv_item(data, project_name, path))
    return out


def _history(query: dict) -> dict:
    """列历史会话：只列**当前工程** chats/ 里的会话（用户 2026-10-18：对话也要按工程隔离）。

    原来会扫 recent_projects 里所有工程，导致别的工程的会话（EAI_GC / default）混进来。
    """
    try:
        errors: list = []
        seen: dict = {}
        source = "disk"

        hist = _history_store()
        if hist is not None:
            try:
                chats_dir = hist.chats_dir or ""
                proj_name = os.path.basename(os.path.dirname(chats_dir.rstrip("\\/")))
                for conv in hist.list_all():
                    cid = str(conv.get("id") or "")
                    if not cid or cid in seen or _is_panel(conv):
                        continue
                    seen[cid] = _conv_item(conv, proj_name,
                                           os.path.join(chats_dir, cid + ".json"))
                source = "core.chat_history+disk"
            except Exception as exc:                # noqa: BLE001
                errors.append("core.chat_history: %s" % exc)

        cfg = _load_config()
        roots = [_resolve_project(cfg)]          # 只看当前工程
        scanned: list = []
        for root in roots:
            chats_dir = os.path.join(root, "chats")
            scanned.append(chats_dir)
            for conv in _read_convs(chats_dir, os.path.basename(root), errors):
                if conv["id"] in seen:
                    continue
                seen[conv["id"]] = conv

        items = list(seen.values())
        items.sort(key=lambda x: x.get("updated") or x.get("created") or "", reverse=True)
        return {
            "total": len(items),
            "items": items,
            "_meta": {
                "source": source,
                "project": roots[0] if roots else "",
                "scanned": scanned,
                "errors": errors,
            },
        }
    except Exception as exc:                        # noqa: BLE001
        return {"error": str(exc)}


# ----------------------------------------------------------------- 生成参数（只读）

def _config(query: dict) -> dict:
    if not os.path.isfile(CONFIG_PATH):
        return {"error": "找不到 config.json：%s" % CONFIG_PATH}
    try:
        cfg = _load_config()
        train = cfg.get("train") or {}
        project = _resolve_project(cfg)

        def _resolve(p):
            if not p:
                return None
            # 2026-10-18 修：相对路径要相对「当前工程根」解析（原来按 ENGINE_DIR 拼，
            # 「生成参数」里预训练权重会显示成 EAI_Studio/engine/checkpoints/… 并误判缺失）
            return p if os.path.isabs(p) else os.path.join(project, p)

        def _stat(p) -> dict:
            rp = _resolve(p)
            info = {"configured": p or "", "path": rp, "exists": False, "size_mb": None}
            try:
                if rp and os.path.isfile(rp):
                    info["exists"] = True
                    info["size_mb"] = round(os.path.getsize(rp) / 1024.0 / 1024.0, 2)
            except Exception:                        # noqa: BLE001
                pass
            return info

        ckpt_dir = os.path.join(project, "checkpoints")
        files: list = []
        try:
            if os.path.isdir(ckpt_dir):
                for name in sorted(os.listdir(ckpt_dir)):
                    fp = os.path.join(ckpt_dir, name)
                    if os.path.isfile(fp):
                        files.append({"name": name,
                                      "size_mb": round(os.path.getsize(fp) / 1024.0 / 1024.0, 2)})
        except Exception:                            # noqa: BLE001
            pass

        sft = _stat(train.get("sft_out") or "checkpoints/eproai_model.pt")
        pre = _stat(train.get("pretrain_out") or "checkpoints/eproai_pretrain.pt")
        with _LOCK:
            loaded = bool(_INFO.get("loaded"))
        # 「自动」时给出实际解析到的设备（用户 2026-10-17：XPU→内存 / GPU→显存 的标签要按真实设备走）；
        # 只读 backend_marker 的落盘缓存，不为此 import torch。
        req_dev = str(train.get("device") or "")
        dev_resolved = req_dev
        if req_dev in ("", "auto"):
            try:
                from core import backend_marker  # noqa: PLC0415
                dev_resolved = str(backend_marker.read().get("backend") or req_dev)
            except Exception:                        # noqa: BLE001
                dev_resolved = req_dev
        return {
            "device": req_dev,
            "device_resolved": dev_resolved,
            "sample_chars": train.get("sample_chars"),
            "repetition_penalty": float((cfg.get("chat") or {}).get("repetition_penalty")
                                        or GEN_REPETITION_PENALTY),
            "block_size": train.get("block_size"),          # 训练配置（下次训练用）
            "chat_max_len": int(train.get("chat_max_len") or 0),   # 0 = 跟随权重自带值
            # 已加载模型的实际值（用户 2026-10-18）：覆盖后可能与训练配置不同
            **(lambda inf: {"loaded_block_size": inf.get("block_size"),
                            "loaded_trained_max_len": inf.get("trained_max_len"),
                            "max_len_overridden": inf.get("max_len_overridden")})(_INFO if loaded else {}),
            "prompt": str(train.get("prompt") or ""),
            "training_format": "\\x01问\\x02答\\x03",
            "project": project,
            "checkpoints_dir": ckpt_dir,
            "checkpoint_files": files,
            "sft_out": sft,
            "pretrain_out": pre,
            "model_ready": bool(sft["exists"] or pre["exists"]),
            "model_loaded": loaded,
        }
    except Exception as exc:                        # noqa: BLE001
        return {"error": str(exc)}


# ----------------------------------------------------------------- 任务状态

def _task_view(name: str, since: int) -> dict:
    try:
        st = _host.task_status(name, since)
    except Exception as exc:                        # noqa: BLE001
        return {"exists": False, "running": False, "events": [], "next": 0,
                "result": None, "error": str(exc)}
    if not st.get("exists"):
        return {"exists": False, "running": False, "events": [], "next": 0,
                "result": None, "error": ""}
    return {
        "exists": True,
        "running": bool(st.get("running")),
        "events": st.get("events") or [],
        "next": st.get("next"),
        "total_events": st.get("total_events"),
        "result": st.get("result"),
        "error": st.get("error") or "",
        "elapsed": st.get("elapsed"),
        "stats": st.get("stats") or {},
    }


def _status(query: dict) -> dict:
    """模型状态 + 加载/生成两个线程任务的增量事件。query: load_since / gen_since。"""
    def _since(key: str) -> int:
        try:
            return int(float(str((query or {}).get(key) or 0) or 0))
        except (TypeError, ValueError):
            return 0

    with _LOCK:
        model = dict(_INFO) if _INFO.get("loaded") else {"loaded": False}
        session = _SESSION_ID
    return {
        "ok": True,
        "model": model,
        "running": _RUNNING,
        "session": session,
        "load": _task_view(LOAD_TASK, _since("load_since")),
        "generate": _task_view(GEN_TASK, _since("gen_since")),
    }


# ----------------------------------------------------------------- 加载权重（真动作）

def _do_load(emit, task, path: str, source: str) -> dict:
    emit("start", {"path": path, "name": os.path.basename(path), "source": source})
    t0 = time.time()
    torch = _import_torch(emit)
    emit("progress", {"stage": "读取权重文件…"})
    ckpt = torch.load(path, map_location="cpu", weights_only=False)
    if not isinstance(ckpt, dict):
        raise RuntimeError("权重文件格式无法识别（顶层不是 dict）")
    if isinstance(ckpt, dict) and ckpt.get("_lora"):
        emit("progress", {"stage": "检测到 LoRA 权重（仍尝试按完整权重加载）"})

    stoi = ckpt.get("stoi") or {}
    if not stoi:
        raise RuntimeError("权重缺少词表（stoi 为空），无法解码")
    raw_itos = ckpt.get("itos") or {}
    if isinstance(raw_itos, dict):
        itos = {int(k): v for k, v in raw_itos.items()}
    else:
        itos = {i: ch for i, ch in enumerate(raw_itos)}

    args = ckpt.get("args") or ckpt.get("model_args") or {}
    a = args if isinstance(args, dict) else {}
    device = _pick_device(torch, (_host.load_cfg().get("train") or {}).get("device"))
    try:
        _host.note_torch_backend(torch)     # 对话验证也算「真用到 torch」，顺手刷新落盘缓存
    except Exception:                        # noqa: BLE001
        pass

    # 对话上下文覆盖（用户 2026-10-18）：config.train.chat_max_len，0 = 跟随权重自带值。
    # 正弦位置编码和因果掩码都是 persistent=False 的常量 buffer（不在 state_dict 里），
    # 所以缩小是安全的；放大到超过训练值能跑但质量会掉。
    trained_len = int(a.get("block_size", 128))
    try:
        over_len = int((_host.load_cfg().get("train") or {}).get("chat_max_len") or 0)
    except (TypeError, ValueError):
        over_len = 0
    max_len = over_len if over_len > 0 else trained_len

    emit("progress", {"stage": "构建模型结构…", "vocab": len(stoi)})
    from core.transformer import GPT                    # 懒 import（拖 torch，不在顶层）
    model = GPT(
        vocab_size=len(stoi),
        d_model=int(a.get("d_model", 128)),
        n_head=int(a.get("n_head", 4)),
        n_layer=int(a.get("n_layer", 4)),
        max_len=max_len,
        dropout=0.0,
        tie_weights=bool(a.get("tie_weights", True)),
        d_ff=a.get("d_ff") or None,
        use_mtp=bool(a.get("mtp", False)),
        mtp_k=int(a.get("mtp_k", 3)),
    )
    state = ckpt.get("model") or ckpt.get("state_dict") or ckpt
    missing, unexpected = model.load_state_dict(state, strict=False)
    model.eval()
    model.to(device)
    params = int(model.n_params())
    ms = int(round((time.time() - t0) * 1000))
    size_mb = round(os.path.getsize(path) / 1048576.0, 2)

    info = {
        "loaded": True, "path": path, "name": os.path.basename(path), "source": source,
        "device": device, "vocab": len(stoi), "params": params,
        "d_model": int(a.get("d_model", 128)), "n_head": int(a.get("n_head", 4)),
        "n_layer": int(a.get("n_layer", 4)), "block_size": max_len,
        "trained_max_len": trained_len, "max_len_overridden": int(max_len != trained_len),
        "d_ff": int(a.get("d_ff") or 4 * int(a.get("d_model", 128))),
        "tie_weights": bool(a.get("tie_weights", True)),
        "mtp": bool(a.get("mtp", False)), "mtp_k": int(a.get("mtp_k", 3)),
        "size_mb": size_mb, "load_ms": ms, "loaded_at": _now(),
        "missing_keys": len(missing), "unexpected_keys": len(unexpected),
    }
    global _MODEL, _STOI, _ITOS, _DEVICE, _INFO
    with _LOCK:
        _MODEL, _STOI, _ITOS, _DEVICE, _INFO = model, stoi, itos, device, info
    task.result = info
    task.set_stats(params=params, vocab=len(stoi), load_ms=ms, device=device)
    emit("result", info)
    return info


def _lora_base(cfg: dict, proj: str) -> str:
    """LoRA 适配器合并用的基座：sft_init_from > pretrain_out > checkpoints 里第一个非适配器 .pt。"""
    train = cfg.get("train") or {}
    for raw in (train.get("sft_init_from"), train.get("pretrain_out"),
                "checkpoints/eproai_pretrain.pt"):
        p = _host.in_project(str(raw or ""), proj) if raw else ""
        if p and os.path.isfile(p):
            return p
    ck = os.path.join(proj, "checkpoints")
    try:
        names = sorted(n for n in os.listdir(ck)
                       if n.lower().endswith(".pt") and not n.lower().endswith(".lora.pt"))
    except OSError:
        names = []
    return os.path.join(ck, names[0]) if names else ""


def _do_load_lora(emit, task, base: str, adapter: str, merged: str) -> dict:
    """先把 LoRA 适配器合并进基座（core/lora.merge_adapter_file，与导出页同一链路），再按完整权重加载。"""
    emit("start", {"path": merged, "name": os.path.basename(merged),
                   "source": "LoRA 适配器（合并基座后加载）"})
    emit("progress", {"stage": "合并 LoRA 适配器到基座…",
                      "base": os.path.basename(base), "adapter": os.path.basename(adapter)})
    _import_torch(emit)
    _host.ensure_ide_path()
    from core.lora import merge_adapter_file          # 懒 import（拖 torch）
    os.makedirs(os.path.dirname(merged), exist_ok=True)
    info = merge_adapter_file(base, adapter, merged)
    emit("progress", {"stage": "合并完成：%d 层 / %d 张量 · %.2f MB，开始加载…" % (
        int(info.get("wrapped") or 0), int(info.get("copied") or 0),
        float(info.get("size") or 0) / 1048576.0)})
    out = _do_load(emit, task, merged, "LoRA 合并权重")
    out["base"] = base                      # 用户 2026-10-19：权重两行按真实来源显示
    out["adapter"] = adapter
    with _LOCK:
        _INFO["base"] = base
        _INFO["adapter"] = adapter
    return out


def _load(body: dict) -> dict:
    """加载权重：不传 path 时优先 sft_out，缺失回退 pretrain_out，都没有就报错。"""
    global _RUNNING
    _RUNNING = False                     # 换权重回到「未开始」（用户 2026-10-18）
    body = body or {}
    explicit = str(body.get("path") or "").strip()
    adapter = str(body.get("adapter") or "").strip()
    cfg = _host.load_cfg()
    wp = _weight_paths(cfg)
    if adapter:                                  # 「加载 LoRA 权重」：适配器 + 基座 → 合并 → 加载
        ap = adapter if os.path.isabs(adapter) else _host.in_project(adapter, wp["project"])
        if not os.path.isfile(ap):
            return {"ok": False, "error": "LoRA 适配器不存在：%s" % ap}
        base = _lora_base(cfg, wp["project"])
        if not base:
            return {"ok": False, "error": "找不到可合并的基座权重：请先产出预训练权重，或在「LoRA 微调」页指定基座。"}
        merged = os.path.join(wp["project"], "logs", "_chat_lora_merged.pt")
        try:
            _host.start_task(
                LOAD_TASK,
                lambda emit, task, group: _do_load_lora(emit, task, base, ap, merged),
                group=LOAD_GROUP,
            )
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "task": LOAD_TASK, "path": merged, "base": base, "adapter": ap,
                "source": "LoRA 适配器（合并基座后加载）",
                "size_mb": round(os.path.getsize(ap) / 1048576.0, 2)}
    if explicit:
        chosen = explicit if os.path.isabs(explicit) else _host.in_project(explicit, wp["project"])
        source = "指定路径"
    elif os.path.isfile(wp["sft"]):
        chosen, source = wp["sft"], "sft_out（微调权重）"
    elif os.path.isfile(wp["pretrain"]):
        chosen, source = wp["pretrain"], "pretrain_out（预训练权重）"
    else:
        return {"ok": False,
                "error": "未找到任何权重：sft_out=%s、pretrain_out=%s 都不存在，请先在「训练」页产出权重。"
                         % (wp["sft"], wp["pretrain"])}
    if not os.path.isfile(chosen):
        return {"ok": False, "error": "权重文件不存在：%s" % chosen}
    try:
        _host.start_task(
            LOAD_TASK,
            lambda emit, task, group: _do_load(emit, task, chosen, source),
            group=LOAD_GROUP,
        )
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "task": LOAD_TASK, "path": chosen, "source": source,
            "size_mb": round(os.path.getsize(chosen) / 1048576.0, 2)}


# ----------------------------------------------------------------- 生成（真动作）

def _persist_turn(prompt: str, reply: str) -> None:
    """把这一轮问答写入当前会话（core.chat_history，落盘到 <工程>/chats/<id>.json）。"""
    global _SESSION_ID, _MESSAGES
    hist = _history_store()
    if hist is None:
        return
    with _LOCK:
        if _SESSION_ID is None:
            conv = hist.new_session()
            conv["source"] = "chat"                     # 标来源：面板那份是 assistant，两边各看各的
            _SESSION_ID = conv.get("id")
            _MESSAGES = []
        _MESSAGES.append({"role": "user", "content": prompt})
        _MESSAGES.append({"role": "assistant", "content": reply})
        sid = _SESSION_ID
        msgs = list(_MESSAGES)
    if sid:
        hist.save_messages(sid, msgs)


def _do_generate(emit, task, prompt: str, max_new: int, block: int, persist: bool, stop_evt) -> dict:
    import torch
    import torch.nn.functional as F
    from core.train import encode                        # 懒 import

    with _LOCK:
        model, stoi, itos, device = _MODEL, _STOI, _ITOS, _DEVICE
    if model is None:
        raise RuntimeError("模型未加载，无法生成。")

    emit("start", {"prompt": prompt, "max_new": max_new, "block_size": block, "device": device})
    t0 = time.time()

    block = int(getattr(model, "max_len", block) or block)   # 以已加载模型的实际上限为准（2026-10-18）
    prefix = "\x01" + prompt + "\x02"
    ids = encode(prefix[-block:], stoi)[-block:].tolist()
    if not ids:
        raise RuntimeError("提示词无法编码成任何 token（词表不含输入字符）")
    idx = torch.tensor([ids], dtype=torch.long, device=device)

    chars: list = []
    ctrl_ids = [int(_STOI[c]) for c in ("\x01", "\x02") if c in _STOI]
    try:                                        # 重复惩罚跟随「指令微调」页的横条（用户 2026-10-19）
        _cfg = _host.load_cfg()
        rep_penalty = float((_cfg.get("chat") or {}).get("repetition_penalty")
                            or (_cfg.get("train") or {}).get("repetition_penalty")
                            or GEN_REPETITION_PENALTY)
    except Exception:                           # noqa: BLE001
        rep_penalty = GEN_REPETITION_PENALTY
    stopped = False
    with torch.no_grad():
        for step in range(max_new):
            if stop_evt.is_set():
                stopped = True
                break
            idx_cond = idx[:, -model.max_len:]
            logits, _ = model(idx_cond)
            logits = logits[:, -1, :].clone()
            if ctrl_ids:                                  # 2026-10-19：答案里不许出现  / 
                logits[:, ctrl_ids] = float("-inf")
            if rep_penalty and rep_penalty != 1.0:
                seen = torch.zeros_like(logits, dtype=torch.bool)
                seen.scatter_(1, idx_cond, True)
                penalized = torch.where(logits > 0, logits / rep_penalty,
                                        logits * rep_penalty)
                logits = torch.where(seen, penalized, logits)
            if GEN_TEMPERATURE == 0:
                nxt = logits.argmax(dim=-1, keepdim=True)
            else:
                logits = logits / GEN_TEMPERATURE
                if GEN_TOP_K:
                    v, _ = torch.topk(logits, min(GEN_TOP_K, logits.size(-1)))
                    logits[logits < v[:, [-1]]] = float("-inf")
                probs = F.softmax(logits, dim=-1)
                nxt = torch.multinomial(probs, num_samples=1)
            idx = torch.cat([idx, nxt], dim=1)
            ch = itos.get(int(nxt), "")
            if ch == "\x03":                             # 训练格式：答完即停
                break
            chars.append(ch)
            emit("token", {"text": ch, "index": len(chars), "partial": "".join(chars)})

    text = "".join(chars)
    ms = int(round((time.time() - t0) * 1000))
    result = {
        "text": text, "prompt": prompt, "tokens": len(chars), "max_new": max_new,
        "elapsed_ms": ms, "stopped": stopped, "device": device, "mode": "stream",
        "temperature": GEN_TEMPERATURE, "top_k": GEN_TOP_K, "repetition_penalty": rep_penalty,
    }
    task.result = result
    task.set_stats(tokens=len(chars), elapsed_ms=ms, stopped=stopped)
    if persist:
        try:
            _persist_turn(prompt, text)
        except Exception as exc:                         # noqa: BLE001
            emit("warn", {"text": "写入历史失败：" + str(exc)})
    emit("result", result)
    return result


def _generate(body: dict) -> dict:
    """用已加载模型真生成（后台线程，逐 token 发增量事件）。"""
    body = body or {}
    prompt = str(body.get("prompt") or body.get("message") or "").strip()
    if not prompt:
        return {"ok": False, "error": "请输入要发送的内容。"}
    with _LOCK:
        loaded = bool(_INFO.get("loaded"))
    if not loaded:
        return {"ok": False, "error": "请先加载模型权重。"}
    if not _RUNNING:
        return {"ok": False, "error": "请先点「开始」，对话才会运行。"}

    cfg = _host.load_cfg()
    train = cfg.get("train") or {}
    raw_sample = body.get("sample_chars")
    try:
        sample = int(raw_sample) if raw_sample not in (None, "") else int(train.get("sample_chars") or 120)
    except (TypeError, ValueError):
        sample = int(train.get("sample_chars") or 120)
    sample = max(1, min(sample, 4000))
    block = int(_INFO.get("block_size") or train.get("block_size") or 128)   # 已加载模型的实际上限
    persist = bool(body.get("persist", True))

    stop_evt = threading.Event()
    try:
        _host.start_task(
            GEN_TASK,
            lambda emit, task, group: _do_generate(emit, task, prompt, sample, block, persist, stop_evt),
            stop=lambda: stop_evt.set(),
            group=GEN_GROUP,
        )
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "task": GEN_TASK, "prompt": prompt,
            "sample_chars": sample, "block_size": block}


# ----------------------------------------------------------------- 停止 / 清空

def _set_limits(body: dict) -> dict:
    """生成长度 / 上下文长度（用户 2026-10-18 改成滑条后统一走这里）。

    - sample_chars：对话单次最多生成的字符数，也用于训练结束时的样例采样；
    - chat_max_len：0 = 跟随权重自带的 max_len，>0 时在加载权重时覆盖（不写回权重）。
    """
    body = body or {}
    cfg = _host.load_cfg()
    train = cfg.setdefault("train", {})
    out = {}
    if "sample_chars" in body:
        try:
            v = int(body["sample_chars"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "生成长度不合法"}
        if not (1 <= v <= 4096):                  # 上限 4096（用户 2026-10-19）
            return {"ok": False, "error": "生成长度建议 1~4096 字符"}
        train["sample_chars"] = v
        out["sample_chars"] = v
    if "repetition_penalty" in body:            # 只影响生成（用户 2026-10-19）
        try:
            v = round(float(body["repetition_penalty"]), 2)
        except (TypeError, ValueError):
            return {"ok": False, "error": "重复惩罚不合法"}
        if not (1.0 <= v <= 2.0):
            return {"ok": False, "error": "重复惩罚建议 1.00~2.00"}
        cfg.setdefault("chat", {})["repetition_penalty"] = v
        out["repetition_penalty"] = v
    if "chat_max_len" in body:
        try:
            v = int(body["chat_max_len"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "上下文长度不合法"}
        if v and not (16 <= v <= 4096):
            return {"ok": False, "error": "上下文长度建议 16~4096"}
        train["chat_max_len"] = v
        out["chat_max_len"] = v
    _host.save_cfg(cfg)
    with _LOCK:
        cur = int(_INFO.get("block_size") or 0)
        trained = int(_INFO.get("trained_max_len") or 0)
    out.update({"ok": True, "loaded_block_size": cur, "trained_max_len": trained})
    return out


def _set_max_len(body: dict) -> dict:
    """对话上下文长度覆盖（用户 2026-10-18）。

    value=0 表示跟随权重文件里记的训练值；>0 时在加载模型时覆盖 max_len。
    只在内存里生效，**不写回权重**；改完要重新加载权重才生效。
    """
    raw = (body or {}).get("value")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return {"ok": False, "error": "数值不合法"}
    if v and not (16 <= v <= 4096):
        return {"ok": False, "error": "上下文长度建议在 16~4096 之间"}
    cfg = _host.load_cfg()
    cfg.setdefault("train", {})["chat_max_len"] = v
    _host.save_cfg(cfg)
    with _LOCK:
        cur = int(_INFO.get("block_size") or 0)
        trained = int(_INFO.get("trained_max_len") or 0)
    return {"ok": True, "chat_max_len": v, "loaded_block_size": cur,
            "trained_max_len": trained, "note": "重新加载权重后生效。"}


def _start(body: dict) -> dict:
    """开始运行（用户 2026-10-18）：加载权重之后必须点它，才允许发消息生成。"""
    global _RUNNING
    with _LOCK:
        loaded = bool(_INFO.get("loaded"))
    if not loaded:
        return {"ok": False, "error": "请先加载模型权重。"}
    _RUNNING = True
    return {"ok": True, "running": True, "note": "已开始运行。"}


def _stop(body: dict) -> dict:
    """停止（用户 2026-10-18）：正在生成就先停生成，然后关掉运行开关。"""
    global _RUNNING
    was = _RUNNING
    _RUNNING = False
    try:
        res = _host.stop_task(GEN_TASK)
    except Exception as exc:                             # noqa: BLE001
        res = {"ok": False, "error": str(exc)}
    out = dict(res or {})
    out["running"] = False
    if was:
        out.setdefault("note", "已停止运行。")
    return out


def _unload(body: dict) -> dict:
    """退出模型运行（用户 2026-10-23）：离开对话验证页超过 2 分钟的自动退出。

    与「停止」的区别：停止只关运行开关（模型还在内存里占着几个 GB），
    这里连模型一起丢掉，把内存/显存还回去；再要对话就得重新加载权重。
    """
    global _RUNNING, _MODEL, _STOI, _ITOS, _DEVICE, _INFO
    _RUNNING = False
    try:
        _host.stop_task(GEN_TASK)               # 正在生成的话先停线程（它自己持有一份模型引用）
    except Exception:                           # noqa: BLE001
        pass
    time.sleep(0.4)                             # 让生成线程先退出，再丢模型
    with _LOCK:
        had = bool(_INFO.get("loaded"))
        _MODEL = None
        _STOI = {}
        _ITOS = {}
        _DEVICE = "cpu"
        _INFO = {"loaded": False}
    try:                                        # 释放缓存（torch 懒导入）
        import torch                            # noqa: F401
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            torch.xpu.empty_cache()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:                           # noqa: BLE001
        pass
    return {"ok": True, "unloaded": had, "running": False,
            "note": "已退出模型运行。" if had else "模型本来就没有加载。"}


def _clear(body: dict) -> dict:
    """清空当前会话（Tk 版语义：只清当前对话，不删磁盘上的历史记录）。"""
    global _SESSION_ID, _MESSAGES
    with _LOCK:
        _SESSION_ID = None
        _MESSAGES = []
    return {"ok": True, "note": "已清空当前对话（不影响磁盘上的历史记录）。"}


def _clear_all(body: dict) -> dict:
    """清空对话验证的全部对话与历史（用户 2026-10-20：清空对话 = 清空全部对话和历史）。

    前端先弹二次确认，确认后才打到这里。只清本页产生的会话（source=assistant 的 AI 助手
    窗口会话不动 —— 那是另一份历史，用户 2026-10-20 要求两边分开）。
    """
    global _SESSION_ID, _MESSAGES
    with _LOCK:
        _SESSION_ID = None
        _MESSAGES = []
    deleted: list = []
    errors: list = []
    h = _history_store()
    try:
        convs = list(h.list_all()) if h else []
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": "读取历史失败：%s" % exc}
    for conv in convs:
        if _is_panel(conv):
            continue
        cid = str(conv.get("id") or "").strip()
        if not cid:
            continue
        try:
            if h.delete(cid):
                deleted.append(cid)
        except Exception as exc:                              # noqa: BLE001
            errors.append("%s: %s" % (cid, exc))
    return {"ok": True, "deleted": deleted, "count": len(deleted), "errors": errors}


def _delete(body: dict) -> dict:
    """删除一条历史会话（AI 面板历史列表里点 ✕）。只删工程里的 chats/<id>.json。"""
    cid = str(body.get("id") or "").strip()
    path = str(body.get("path") or "").strip()
    if not cid:
        return {"ok": False, "error": "缺少会话 id"}
    h = _history_store()
    try:
        if h.delete(cid):
            return {"ok": True, "deleted": cid}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": "删除失败：%s" % exc}
    # 当前工程里没这条（比如它属于别的工程的 chats/）：按前端给的 path 复核后删文件
    if path:
        p = os.path.abspath(path)
        if os.path.basename(p) == cid + ".json" and os.path.basename(os.path.dirname(p)) == "chats":
            try:
                os.remove(p)
                return {"ok": True, "deleted": cid}
            except OSError as exc:
                return {"ok": False, "error": "删除失败：%s" % exc}
    return {"ok": False, "error": "没找到这条会话（可能已被删除）"}


# ----------------------------------------------------------------- 注册

def register(router) -> None:
    router.get("/api/chat/history", _history)
    router.get("/api/chat/config", _config)
    router.get("/api/chat/status", _status)
    router.post("/api/chat/load", _load)
    router.post("/api/chat/generate", _generate)
    router.post("/api/chat/send", _generate)             # 兼容别名
    router.post("/api/chat/limits", _set_limits)
    router.post("/api/chat/max_len", _set_max_len)
    router.post("/api/chat/start", _start)
    router.post("/api/chat/stop", _stop)
    router.post("/api/chat/unload", _unload)          # 离开本页 2 分钟自动退出（用户 2026-10-23）
    router.post("/api/chat/clear", _clear)
    router.post("/api/chat/delete", _delete)
    router.post("/api/chat/clear_all", _clear_all)
