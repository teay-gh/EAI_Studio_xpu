#!/usr/bin/env python3
"""模型转换（用户 2026-10-18）：「模型」分组里的新页面，排在「导出模型」上面。

  GET  /api/modelconv/config   —— 当前工程、能否存进 EAI 权重、默认目录
  POST /api/modelconv/probe    —— 识别源权重（格式 / 大小 / tensor 数 / 参数量 / 词表 / 能否转）
  POST /api/modelconv/start    —— 真转换（后台线程 + 增量事件）
  GET  /api/modelconv/status   —— 增量事件（since 游标）
  POST /api/modelconv/stop     —— 请求停止

口径（用户 2026-10-18 明确）：
  * safetensors ⇄ pt 双向转换；外部模型允许直接从任意路径导入；
  * 输出由用户选：另存到指定文件夹，或存进 EAI（当前工程 checkpoints/）；
  * **只有 safetensors → .pt 才能存进 EAI**；.pt → safetensors 只能另存；
  * 没创建工程时页面照常可用（不置灰），只是「保存到 EAI 权重」不可选。

实现：.pt 用 torch 读写；safetensors 用纯 Python 读写（本机 .venv 没装 safetensors 包，
按规范直接解析 8 字节长度 + JSON 头 + 原始张量区，避免为一件小事加依赖）。
EAI 的 .pt 里除了 state_dict 还有 stoi / itos / args：
  * .pt → safetensors 时这三样写进 safetensors 的 __metadata__（键 eai），往返不丢；
  * safetensors → .pt 时优先用 metadata 还原；没有词表就按张量形状推断结构，
    并明确提示「不含词表，对话验证无法直接加载」。
"""
from __future__ import annotations

import json
import os
import struct
import threading
import time
from typing import Any

import _host

TASK_NAME = "modelconv"
TASK_GROUP = "modelconv"

# safetensors dtype → numpy dtype（bf16 单独处理）
ST_TO_NP: dict[str, Any] = {
    "F64": "float64", "F32": "float32", "F16": "float16",
    "I64": "int64", "I32": "int32", "I16": "int16", "I8": "int8",
    "U8": "uint8", "BOOL": "bool",
}
NP_TO_ST = {
    "float64": "F64", "float32": "F32", "float16": "F16",
    "int64": "I64", "int32": "I32", "int16": "I16", "int8": "I8",
    "uint8": "U8", "bool": "BOOL",
}


# ---------------------------------------------------------------- 路径 / 工程

def _cfg() -> dict:
    return _host.load_cfg()


def _project(cfg: dict) -> str:
    """当前工程根；没有可用工程返回空串（不拿兜底工程冒充）。"""
    app = cfg.get("app") or {}
    # 2026-10-23：只认当前工程（recent_projects[0]）—— 当前工程被删时不再悄悄换成第 2 个（工程串）
    for item in (app.get("recent_projects") or [])[:1]:
        if isinstance(item, str) and item and _host.is_project_dir(item):
            return os.path.abspath(item)
    return ""


def _checkpoints_dir(cfg: dict) -> str:
    root = _project(cfg)
    return os.path.join(root, "checkpoints") if root else ""


# ---------------------------------------------------------------- safetensors 读写

def _st_header(path: str) -> tuple[dict, dict, int]:
    with open(path, "rb") as f:
        raw = f.read(8)
        if len(raw) < 8:
            raise ValueError("不是 safetensors 文件（文件太短）")
        n = struct.unpack("<Q", raw)[0]
        if n <= 0 or n > 200 * 1024 * 1024:
            raise ValueError("safetensors 头部长度异常（%d）" % n)
        blob = f.read(n)
    try:
        head = json.loads(blob.decode("utf-8"))
    except Exception as exc:                       # noqa: BLE001
        raise ValueError("safetensors 头部 JSON 解析失败：%s" % exc)
    if not isinstance(head, dict):
        raise ValueError("safetensors 头部不是对象")
    meta = head.pop("__metadata__", None) or {}
    return head, {str(k): str(v) for k, v in meta.items()}, 8 + n


def _st_load(path: str) -> tuple[dict, dict]:
    import numpy as np                             # noqa: PLC0415
    import torch                                   # noqa: PLC0415

    head, meta, start = _st_header(path)
    sd: dict[str, Any] = {}
    with open(path, "rb") as f:
        for name, info in head.items():
            if not isinstance(info, dict):
                continue
            dt = str(info.get("dtype") or "")
            shape = [int(x) for x in (info.get("shape") or [])]
            offs = info.get("data_offsets") or [0, 0]
            a, b = int(offs[0]), int(offs[1])
            f.seek(start + a)
            buf = f.read(max(0, b - a))
            if dt == "BF16":
                t = torch.frombuffer(bytearray(buf), dtype=torch.uint16).view(torch.bfloat16)
                sd[name] = t.reshape(shape).clone()
            elif dt in ST_TO_NP:
                arr = np.frombuffer(buf, dtype=np.dtype(ST_TO_NP[dt]))
                sd[name] = torch.from_numpy(arr.reshape(shape)).clone()
            else:
                raise ValueError("不支持的 safetensors dtype：%s（%s）" % (dt, name))
    return sd, meta


def _st_save(path: str, sd: dict, meta: dict | None = None) -> int:
    import numpy as np                             # noqa: PLC0415
    import torch                                   # noqa: PLC0415

    header: dict[str, Any] = {}
    parts: list[bytes] = []
    off = 0
    for name, t in sd.items():
        if not torch.is_tensor(t):
            raise ValueError("张量 %s 不是 torch.Tensor" % name)
        t = t.detach().cpu().contiguous()
        if t.dtype == torch.bfloat16:
            arr, dt = t.view(torch.uint16).numpy(), "BF16"
        else:
            arr, dt = t.numpy(), NP_TO_ST.get(str(t.numpy().dtype))
            if not dt:
                raise ValueError("不支持的 dtype：%s（%s）" % (t.dtype, name))
        blob = arr.tobytes()
        header[name] = {"dtype": dt, "shape": list(t.shape), "data_offsets": [off, off + len(blob)]}
        parts.append(blob)
        off += len(blob)
    if meta:
        header["__metadata__"] = {str(k): str(v) for k, v in meta.items() if v is not None}
    hb = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    hb += b" " * ((-len(hb)) % 8)                  # 头必须 8 字节对齐
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(hb)))
        f.write(hb)
        for b in parts:
            f.write(b)
    return os.path.getsize(path)


# ---------------------------------------------------------------- .pt 读写

def _pt_load(path: str) -> tuple[dict, dict]:
    import torch                                   # noqa: PLC0415

    obj = torch.load(path, map_location="cpu", weights_only=False)
    if isinstance(obj, dict) and isinstance(obj.get("model"), dict):
        return obj["model"], obj
    if isinstance(obj, dict) and obj and all(torch.is_tensor(v) for v in obj.values()):
        return obj, {}
    raise ValueError("不支持的 .pt 内容：既不是 {'model': state_dict}，也不是纯 state_dict")


def _pt_save(path: str, payload: dict) -> int:
    import torch                                   # noqa: PLC0415

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    torch.save(payload, path)
    return os.path.getsize(path)


# ---------------------------------------------------------------- 结构推断

def _n_params(sd: dict) -> int:
    return int(sum(int(getattr(t, "numel", lambda: 0)()) for t in sd.values()))


def _infer_args(sd: dict, base: dict | None = None) -> dict:
    """按张量形状推断 GPT 结构参数（safetensors 里没存 args 时用）。"""
    a = dict(base or {})
    emb = sd.get("tok_emb.weight")
    if emb is not None and getattr(emb, "dim", lambda: 0)() == 2:
        a.setdefault("vocab_size", int(emb.shape[0]))
        a.setdefault("d_model", int(emb.shape[1]))
    layers = set()
    for k in sd:
        if k.startswith("blocks.") and k.count(".") >= 2:
            try:
                layers.add(int(k.split(".")[1]))
            except (IndexError, ValueError):
                continue
    if layers:
        a.setdefault("n_layer", max(layers) + 1)
    fc = sd.get("blocks.0.ffn.fc_in.weight")
    if fc is not None and getattr(fc, "dim", lambda: 0)() == 2:
        a.setdefault("d_ff", int(fc.shape[0]))
    if "d_model" in a:
        d = int(a["d_model"])
        if "n_head" not in a:
            a["n_head"] = max(1, d // 64)          # 推断值，仅用于凑出能加载的结构
    a.setdefault("block_size", 128)
    a.setdefault("tie_weights", "head.weight" not in sd)
    a.setdefault("mtp", any(k.startswith("mtp_heads.") for k in sd))
    a["inferred"] = True
    return a


def _eai_meta(sd: dict, extra: dict) -> dict:
    """把 stoi/itos/args 塞进 safetensors 的 __metadata__（字符串化）。"""
    out: dict[str, str] = {"eai_format": "1"}
    for k in ("stoi", "itos", "args"):
        if extra.get(k):
            out[k] = json.dumps(extra[k], ensure_ascii=False)
    for k in ("source", "created_at"):
        if extra.get(k):
            out[k] = str(extra[k])
    return out


# ---------------------------------------------------------------- 接口：配置 / 识别

def _conv_log(cfg: dict, src: str, out: str) -> None:
    """转换成功写日志（用户 2026-10-21）：
    转换结果进工程 =「转换导入」（把模型加进来）；工程内权重转出去 =「转换导出」；其余转换 =「转换」。

    落库走 _host.model_log（<工程>/data/_model_logs.json）；总览日志按 path 让位去重，
    同一文件的 checkpoints 扫描行（笼统「训练」）让位，不再一条文件两行。
    """
    try:
        proj = _project(cfg)
    except Exception:                                # noqa: BLE001  没工程就写不了日志
        return
    if not proj:
        return

    def _in(p: str) -> bool:
        try:
            ap = os.path.normcase(os.path.abspath(p))
            rp = os.path.normcase(os.path.abspath(proj))
            return ap == rp or ap.startswith(rp + os.sep)
        except Exception:                            # noqa: BLE001
            return False

    op = "转换导入" if _in(out) else ("转换导出" if _in(src) else "转换")
    _host.model_log(cfg, op, os.path.basename(out), out)


def config(_query: dict) -> dict:
    cfg = _cfg()
    root = _project(cfg)
    return {
        "ok": True,
        "project": root,
        "project_name": os.path.basename(root) if root else "",
        "project_ready": bool(root),
        "checkpoints": _checkpoints_dir(cfg),
        "home": os.path.expanduser("~"),
    }


def probe(body: dict) -> dict:
    """识别一个权重文件：格式、张量数、参数量、有没有词表/结构参数。"""
    path = str(body.get("path") or "").strip().strip('"')
    if not path:
        return {"ok": False, "error": "先选择要转换的权重文件"}
    if not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在：%s" % path}
    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path)
    info: dict[str, Any] = {
        "ok": True, "path": path, "name": os.path.basename(path),
        "ext": ext, "bytes": size, "size_mb": round(size / 1048576.0, 2),
    }
    try:
        if ext == ".safetensors":
            head, meta, _start = _st_header(path)
            tensors = [k for k, v in head.items() if isinstance(v, dict) and v.get("shape") is not None]
            params = 0
            dtypes = set()
            for k in tensors:
                shp = head[k].get("shape") or []
                n = 1
                for d in shp:
                    n *= int(d)
                params += n
                dtypes.add(str(head[k].get("dtype") or ""))
            info.update({
                "format": "safetensors", "target": "pt",
                "tensors": len(tensors), "params": params,
                "dtypes": sorted(x for x in dtypes if x),
                "has_vocab": bool(meta.get("stoi")),
                "has_args": bool(meta.get("args")),
                "sample_keys": tensors[:6],
            })
            if meta.get("args"):
                try:
                    info["args"] = json.loads(meta["args"])
                except (ValueError, TypeError):
                    pass
        elif ext in (".pt", ".pth", ".bin"):
            sd, extra = _pt_load(path)
            info.update({
                "format": "pt", "target": "safetensors",
                "tensors": len(sd), "params": _n_params(sd),
                "dtypes": sorted({str(t.dtype).replace("torch.", "") for t in sd.values()}),
                "has_vocab": bool(extra.get("stoi")),
                "has_args": bool(extra.get("args")),
                "sample_keys": list(sd.keys())[:6],
                "is_eai": bool(extra.get("stoi")),
            })
            if isinstance(extra.get("args"), dict):
                info["args"] = {k: extra["args"][k] for k in
                                ("d_model", "n_head", "n_layer", "d_ff", "block_size", "vocab_size")
                                if k in extra["args"]}
        else:
            return {"ok": False, "error": "只支持 .safetensors / .pt（.pth、.bin 按 .pt 处理）"}
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    info["can_project"] = (info["format"] == "safetensors")
    if info["format"] == "pt":
        info["project_note"] = "只有 safetensors → .pt 才能存进 EAI 权重，.pt → safetensors 请另存"
    elif not info.get("has_vocab"):
        info["project_note"] = "该 safetensors 不含词表：转成 .pt 后对话验证无法加载（结构会按形状推断）"
    return info


# ---------------------------------------------------------------- 接口：转换

def start(body: dict) -> dict:
    cfg = _cfg()
    path = str(body.get("path") or "").strip().strip('"')
    mode = str(body.get("mode") or "folder")
    out_dir = str(body.get("out_dir") or "").strip().strip('"')
    out_name = str(body.get("out_name") or "").strip()
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "源文件不存在：%s" % path}
    running = _host.task_status(TASK_NAME)
    if running.get("running"):
        return {"ok": False, "error": "已有转换任务在运行，先停止"}

    ext = os.path.splitext(path)[1].lower()
    if ext == ".safetensors":
        target = "pt"
    elif ext in (".pt", ".pth", ".bin"):
        target = "safetensors"
    else:
        return {"ok": False, "error": "只支持 .safetensors / .pt"}

    if mode == "project":
        if target != "pt":
            return {"ok": False, "error": "只有 safetensors → .pt 才能保存到 EAI 权重"}
        ckpt = _checkpoints_dir(cfg)
        if not ckpt:
            return {"ok": False, "error": "当前没有工程，无法保存到 EAI 权重；请先创建/切换工程，或改成另存到文件夹"}
        out_dir = ckpt
    else:
        if not out_dir:
            return {"ok": False, "error": "先选择输出文件夹"}
        if not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as exc:
                return {"ok": False, "error": "输出目录不可用：%s" % exc}

    base = os.path.splitext(os.path.basename(path))[0]
    name = out_name or base
    name = os.path.splitext(name)[0] + (".pt" if target == "pt" else ".safetensors")
    out = os.path.join(out_dir, name)

    stop_evt = threading.Event()

    def job(emit, task, group):                    # noqa: ANN001
        t0 = time.time()
        try:
            emit("log", {"text": "读取源文件：%s（%.2f MB）" % (os.path.basename(path), os.path.getsize(path) / 1048576.0),
                          "text_en": "Reading source: %s (%.2f MB)" % (os.path.basename(path), os.path.getsize(path) / 1048576.0)})
            if target == "safetensors":
                sd, extra = _pt_load(path)
                emit("log", {"text": "解析到 %d 个张量 · 参数 %s" % (len(sd), _fmt_int(_n_params(sd))),
                              "text_en": "Parsed %d tensors · %s parameters" % (len(sd), _fmt_int(_n_params(sd)))})
                if stop_evt.is_set():
                    emit("log", {"text": "已停止（未写入文件）", "text_en": "Stopped (nothing written)"}); return
                meta = _eai_meta(sd, {"stoi": extra.get("stoi"), "itos": extra.get("itos"),
                                      "args": extra.get("args"),
                                      "source": os.path.basename(path),
                                      "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
                n = _st_save(out, sd, meta)
                note = "已写入 safetensors（含词表/结构元数据）" if extra.get("stoi") else "已写入 safetensors"
                note_en = "Wrote safetensors (vocab / structure metadata included)" if extra.get("stoi") else "Wrote safetensors"
                emit("log", {"text": note, "text_en": note_en, "level": "ok"})
            else:
                sd, meta = _st_load(path)
                emit("log", {"text": "解析到 %d 个张量 · 参数 %s" % (len(sd), _fmt_int(_n_params(sd))),
                              "text_en": "Parsed %d tensors · %s parameters" % (len(sd), _fmt_int(_n_params(sd)))})
                if stop_evt.is_set():
                    emit("log", {"text": "已停止（未写入文件）", "text_en": "Stopped (nothing written)"}); return
                payload: dict[str, Any] = {"model": sd}
                stoi = itos = None
                args = None
                if meta.get("stoi"):
                    try:
                        stoi = json.loads(meta["stoi"])
                    except (ValueError, TypeError):
                        stoi = None
                if meta.get("itos"):
                    try:
                        itos = json.loads(meta["itos"])
                    except (ValueError, TypeError):
                        itos = None
                if meta.get("args"):
                    try:
                        args = json.loads(meta["args"])
                    except (ValueError, TypeError):
                        args = None
                if not stoi:
                    args = _infer_args(sd, args)
                    stoi, itos = {}, {}
                    emit("log", {"text": "源文件不含词表：结构按张量形状推断（%s），对话验证无法直接加载"
                                         % _fmt_args(args),
                                 "text_en": "Source has no vocabulary: structure inferred from tensor shapes (%s); "
                                            "chat verification cannot load it directly" % _fmt_args(args),
                                 "level": "warn"})
                else:
                    args = _infer_args(sd, args)
                payload.update({"stoi": stoi or {}, "itos": itos or {}, "args": args or {}})
                n = _pt_save(out, payload)
                emit("log", {"text": "已写入 pt", "text_en": "Wrote pt", "level": "ok"})
            _conv_log(cfg, path, out)                # 转换日志：转换导入/转换导出/转换（2026-10-21）
            emit("result", {"ok": True, "out": out, "name": os.path.basename(out),
                            "bytes": n, "size_mb": round(n / 1048576.0, 2),
                            "target": target, "mode": mode,
                            "ms": int((time.time() - t0) * 1000)})
        except Exception as exc:                   # noqa: BLE001
            emit("error", {"text": "%s: %s" % (type(exc).__name__, exc)})

    _host.start_task(TASK_NAME, job, stop=lambda: stop_evt.set(), group=TASK_GROUP)
    return {"ok": True, "task": TASK_NAME, "out": out, "target": target}


def check(query: dict) -> dict:
    """命名查重（用户 2026-10-18）：目标目录里是否已有同名文件。"""
    d = str(query.get("dir") or "").strip().strip('"')
    n = str(query.get("name") or "").strip()
    ext = str(query.get("ext") or "").strip().lower()
    if ext not in (".pt", ".safetensors"):
        ext = ".pt"
    if not d or not n:
        return {"ok": True, "exists": False}
    path = os.path.join(d, os.path.splitext(n)[0] + ext)
    return {"ok": True, "exists": os.path.exists(path), "path": path}


def stop(_body: dict) -> dict:
    return _host.stop_task(TASK_NAME)


def status(query: dict) -> dict:
    since = 0
    try:
        since = int(query.get("since") or 0)
    except (TypeError, ValueError):
        since = 0
    st = _host.task_status(TASK_NAME, since)
    st["ok"] = True
    return st


# ---------------------------------------------------------------- 小工具

def _fmt_int(n: Any) -> str:
    try:
        v = int(n or 0)
    except (TypeError, ValueError):
        return "—"
    if v >= 1_000_000_000:
        return "%.2fB" % (v / 1e9)
    if v >= 1_000_000:
        return "%.1fM" % (v / 1e6)
    if v >= 1000:
        return "%.1fK" % (v / 1e3)
    return str(v)


def _fmt_args(a: dict) -> str:
    keys = ("d_model", "n_head", "n_layer", "d_ff", "block_size")
    return " · ".join("%s=%s" % (k, a.get(k)) for k in keys if a.get(k) is not None)


def register(router) -> None:                      # noqa: ANN001
    router.get("/api/modelconv/config", config)
    router.post("/api/modelconv/probe", probe)
    router.post("/api/modelconv/start", start)
    router.get("/api/modelconv/check", check)
    router.post("/api/modelconv/stop", stop)
    router.get("/api/modelconv/status", status)
