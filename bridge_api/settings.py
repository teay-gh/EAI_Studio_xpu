#!/usr/bin/env python3
"""全局设置页的桥接口（EAI_Studio/bridge_api/settings.py）

对外接口（GET / POST，全部只在本机 127.0.0.1）：
  GET  /api/settings/state          工程 / 最近工程 / 路径 / API 默认值 / 设备（不 import torch，秒回）
  GET  /api/settings/device         真探测设备（懒 import torch，结果缓存）
  POST /api/settings/device         {"value": "auto|cpu|cuda|xpu"} → 写 config.train.device
  POST /api/settings/api            {"base_url": "...", "api_key": "..."} → 只改非空项；
                                     base_url 只补空的 profile，api_key 覆盖全部 profile
  POST /api/settings/project        {"path": "..."} → 切到该工程（写 app.recent_projects）
  POST /api/settings/project/create {"path": "...", "name": "..."} → 新建工程骨架
  POST /api/settings/project/open   在资源管理器里打开当前工程目录

说明：这是**真写 config.json** 的页面（与 Tk 版「保存设置」同语义），写前会留一份
`config.json.bak` 备份。
"""
from __future__ import annotations

import ctypes
import io
import json
import os
import platform
import shutil
import sys
import time
from typing import Any

import _host

# ---------------------------------------------------------------------------
# 版本形态（用户 2026-10-23）：以后 EAI 出「XPU 版」和「CUDA 版」两个安装包，
# 代码是同一套 —— 打包 CUDA 版时把这一行改成 "cuda" 即可（安装包脚本替换）。
# 设备段选只认本版本的加速器：XPU 版点 CUDA → 提示「请切换 CUDA 版本」。
# ---------------------------------------------------------------------------
EDITION = "xpu"

SECTIONS = ("article", "response")          # 各含 titler/writer/reviewer 等 profile
API_SECTIONS = ("assistant", "article", "response")


# ---------------------------------------------------------------- 工具

def _mask(key: str) -> str:
    key = key or ""
    return "•" * 12 if key else ""


def _profiles(cfg: dict) -> list[tuple]:
    out = []
    for section in ("article", "response"):
        for role, profile in (cfg.get(section) or {}).items():
            if isinstance(profile, dict) and ("base_url" in profile or "model" in profile):
                out.append((section, role, profile))
    return out


def _backup_config() -> str:
    """写前备份（最多留最近 5 份，放在 EAI_Studio/engine/config_backups/）。"""
    src = _host.CONFIG_PATH
    if not os.path.isfile(src):
        return ""
    d = os.path.join(_host.IDE_DIR, "config_backups")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "config.%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    try:
        shutil.copy2(src, dst)
        olds = sorted(f for f in os.listdir(d) if f.startswith("config."))
        for f in olds[:-5]:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
        return dst
    except OSError:
        return ""


# ---------------------------------------------------------------- 读

def state(_query: dict) -> dict:
    cfg = _host.load_cfg()
    try:                                             # 2026-10-21 撤兜底：没工程时本端点照样回包
        proj = _host.project(cfg)
        proj_info = {"root": proj.root, "name": proj.name,
                     "exists": os.path.isdir(proj.root),
                     "is_project": os.path.isfile(proj.project_file),
                     "logs": os.path.isfile(os.path.join(proj.logs_dir, "runtime_pretrain_full.json"))}
    except Exception:                                # noqa: BLE001  前端按 project_chosen 弹「创建工程 / 导入工程」
        proj = None
        _cur = _host.current_root(cfg)               # 2026-10-23：当前工程被删掉时如实报，别让页面串到别的工程
        proj_info = ({} if not _cur or os.path.isdir(_cur) else
                     {"root": _cur, "name": os.path.basename(_cur.rstrip("\\/")) or _cur,
                      "exists": False, "is_project": False, "logs": False, "missing": True})
    app_cfg = cfg.get("app") or {}
    train = cfg.get("train") or {}
    assistant = cfg.get("assistant") or {}
    from core.project import recent_list  # type: ignore

    profiles = []
    for section, role, profile in _profiles(cfg):
        profiles.append({
            "section": section, "role": role,
            "base_url": profile.get("base_url") or "",
            "api_key_set": bool(profile.get("api_key")),
        })
    base_urls = sorted({p["base_url"] for p in profiles if p["base_url"]})
    keys = {p["api_key_set"] for p in profiles}
    return {
        "ok": True,
        "project": proj_info,
        "recent": [p for p in recent_list(cfg) if os.path.isdir(p)],
        # 用户有没有明确选过工程（recent_projects 里存在真实目录）。
        # false 时前端把内容变灰并弹「创建工程 / 导入工程」（用户 2026-10-17）；
        # 2026-10-21 撤兜底：后端不再回落默认工程——没建工程就没建，功能不给用。
        "project_chosen": (not bool(app_cfg.get("project_closed")))
                          and not bool(proj_info.get("missing"))
                          and any(isinstance(p, str) and p and os.path.isdir(p)
                                  for p in (app_cfg.get("recent_projects") or [])),
        "auto_distill_dirs": bool(app_cfg.get("auto_distill_dirs", True)),
        # 托管：勾上后托管 AI 只做风险提示、不再中止（卡死仍会停）——用户 2026-10-17
        "host_force_complete": bool(app_cfg.get("host_force_complete", False)),
        # 未完成功能（2026-10-21）：关=隐藏 托管/格式转换，默认关
        "unfinished": bool(app_cfg.get("unfinished", False)),
        "device": {
            "value": (train.get("device") or "auto"),
            "options": ["auto", "cpu", "cuda", "xpu"],
            "hint": "auto = 有 xpu/cuda 就用，否则 cpu",
        },
        "api": {
            "base_urls": base_urls,
            # 表单显示「默认 API」= assistant 段优先；它为空才回落到 profile 里的地址
            "base_url": (assistant.get("base_url") or "").strip()
                        or (base_urls[0] if base_urls else ""),
            "assistant_base_url": (assistant.get("base_url") or ""),
            "assistant_model": (assistant.get("model") or ""),
            "assistant_key_set": bool((assistant.get("api_key") or "").strip()),
            "all_have_key": keys == {True} and bool(keys),
            "any_have_key": True in keys,
            "key_mask": _mask("x" * 51),
            "profiles": profiles,
            "assistant_base_url": ((cfg.get("assistant") or {}).get("base_url") or ""),
        },
        "app": {
            "name": app_cfg.get("name") or "EAI Studio",
            "version": app_cfg.get("version") or "",
            "edition": EDITION,          # xpu / cuda：设置页版本号后面跟这个（用户 2026-10-23）
            "theme": app_cfg.get("theme") or "dark",
            "lang": app_cfg.get("lang") or "zh",
        },
        "paths": {"config": _host.CONFIG_PATH, "ide": _host.IDE_DIR, "web": _host.WEB_DIR},
    }


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _clean(name: str) -> str:
    """去掉商标注记：(R) / (TM) / ® / ™ —— 界面上不显示这些。"""
    s = str(name or "")
    for bad in ("(R)", "(r)", "(TM)", "(tm)", "®", "™"):
        s = s.replace(bad, "")
    return " ".join(s.split())


def _cpu_name() -> str:
    """CPU 型号（Windows 走注册表最准，退化到 platform.processor()）。"""
    if sys.platform == "win32":
        try:
            import winreg  # noqa: PLC0415
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except Exception:                       # noqa: BLE001
            pass
    return (platform.processor() or platform.machine() or "").strip()


def _mem_gb() -> float:
    """物理内存总量（GB，保留一位小数）。"""
    if sys.platform != "win32":
        return 0.0
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return round(st.ullTotalPhys / 1024 ** 3, 1)
    except Exception:                           # noqa: BLE001
        return 0.0


def _wmi_igpu() -> str:
    """核显名（XPU 不可用时才走这里：从显卡清单里挑 Intel/AMD 那一条）。"""
    if sys.platform != "win32":
        return ""
    try:
        import subprocess  # noqa: PLC0415
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20).stdout
        for line in out.splitlines():
            n = line.strip()
            if any(k in n.lower() for k in ("intel", "amd", "radeon", "arc", "graphics", "uhd", "iris")):
                return n
    except Exception:                           # noqa: BLE001
        pass
    return ""


BACKEND_MARKER = os.path.join(_host.IDE_DIR, "torch_backend.json")


def _read_backend_marker() -> dict:
    """读 engine/torch_backend.json —— 上一次真探测留下的设备能力（本机有没有 cuda / xpu）。

    写入方是 `_host.pick_device`（训练 / 对话验证第一次 import torch 时）与 `engine/tools/setup_torch.py`。
    设置页只读它，**全程不 import torch**：点开设置毫秒级返回，也不把 ~1GB 的 torch 常驻在桥进程里。
    """
    try:
        with io.open(BACKEND_MARKER, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:                           # noqa: BLE001
        return {}


def _resolve_device(want: str, cuda: bool, xpu: bool) -> str:
    """纯计算版设备解析，与 `_host.pick_device` 同序（Windows 无 mps）：显式可用就用它，否则 CUDA → XPU → CPU。"""
    if want in ("cuda", "xpu") and (cuda if want == "cuda" else xpu):
        return want
    if want == "cpu":
        return "cpu"
    return "cuda" if cuda else ("xpu" if xpu else "cpu")


def device(_query: dict) -> dict:
    """设备卡：读落盘探测结果（免 torch）；从来没探测过就如实报「未知」，绝不为了显示去 import torch。"""
    cfg = _host.load_cfg()
    want = str((cfg.get("train") or {}).get("device") or "auto").strip().lower()
    if want not in ("auto", "cpu", "cuda", "xpu"):
        want = "auto"
    cached = _read_backend_marker()
    known = bool(cached)
    cuda = bool(cached.get("cuda_available"))
    xpu = bool(cached.get("xpu_available"))
    out: dict[str, Any] = {
        "ok": True, "want": want, "known": known,
        "source": "cache" if known else "none",
        "checked_at": str(cached.get("checked_at") or ""),
        "torch": str(cached.get("torch") or ""),
        "cuda": cuda, "xpu": xpu,
        "resolved": _resolve_device(want, cuda, xpu) if known else want,
        "desc": "", "threads": int(cached.get("threads") or 0), "error": None,
        "cpu": _clean(_cpu_name()), "mem_gb": _mem_gb(), "igpu": "", "gpu": "",
    }
    if xpu:                                     # XPU 行只认「支持 XPU 的核显」：不可用就是空（界面显示「无」）
        out["igpu"] = _clean(str(cached.get("xpu_name") or ""))
    if cuda:                                    # GPU 行只列支持 CUDA 架构的独显
        out["gpu"] = _clean(str(cached.get("cuda_name") or ""))
    out["desc"] = out["igpu"] or out["gpu"] or "CPU"
    return out


def set_device(body: dict) -> dict:
    value = str(body.get("value") or "").strip()
    if value not in ("auto", "cpu", "cuda", "xpu"):
        return {"ok": False, "error": "设备只能是 auto / cpu / cuda / xpu"}
    # 本版本只认自己的加速器（用户 2026-10-23）：XPU 版不写 cuda，CUDA 版不写 xpu
    if value in ("cuda", "xpu") and value != EDITION:
        return {"ok": False, "error": "请切换 %s 版本" % value.upper()}
    bak = _backup_config()
    cfg = _host.load_cfg()
    cfg.setdefault("train", {})["device"] = value
    _host.save_cfg(cfg)
    return {"ok": True, "device": value, "backup": bak,
            "note": "已写入 config.json；下次启动训练生效。"}


def set_app(body: dict) -> dict:
    """写 config.app 里的界面/数据集行为开关（不传的项保持原值）。"""
    body = body or {}
    changed = {}
    cfg = _host.load_cfg()
    app_cfg = cfg.setdefault("app", {})
    if "auto_distill_dirs" in body:
        app_cfg["auto_distill_dirs"] = bool(body.get("auto_distill_dirs"))
        changed["auto_distill_dirs"] = app_cfg["auto_distill_dirs"]
    if "host_force_complete" in body:
        app_cfg["host_force_complete"] = bool(body.get("host_force_complete"))
        changed["host_force_complete"] = app_cfg["host_force_complete"]
    if "unfinished" in body:               # 未完成功能开关（2026-10-21）：关=隐藏托管/格式转换
        app_cfg["unfinished"] = bool(body.get("unfinished"))
        changed["unfinished"] = app_cfg["unfinished"]
    if "lang" in body:                     # 界面语言（2026-10-18）：zh / en，落盘后重启自动沿用
        lv = str(body.get("lang") or "").strip().lower()
        if lv not in ("zh", "en"):
            return {"ok": False, "error": "lang 只能是 zh 或 en"}
        app_cfg["lang"] = lv
        changed["lang"] = lv
    if "theme" in body:                    # 界面切主题要落盘，否则助手拿到的「当前主题」永远是旧的
        t = str(body.get("theme") or "").strip()
        if t not in ("dark", "light"):
            return {"ok": False, "error": "theme 只能是 dark 或 light"}
        app_cfg["theme"] = t
        changed["theme"] = t
    if not changed:
        return {"ok": False, "error": "没有可写的项"}
    bak = _backup_config()
    _host.save_cfg(cfg)
    return {"ok": True, "changed": changed, "backup": bak,
            "note": "已写入 config.json；数据集页下次打开生效。"}


def set_api(body: dict) -> dict:
    """base_url 只补空的 profile；api_key 非空则覆盖全部 profile。不传的项保持原值。"""
    base_url = str(body.get("base_url") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    if not base_url and not api_key:
        return {"ok": False, "error": "base_url 与 api_key 都为空，无需保存（留空=保留原值）"}
    if api_key and (api_key.startswith("•") or set(api_key) == {"•"}):
        return {"ok": False, "error": "api_key 收到的是掩码，请输入真实密钥"}
    bak = _backup_config()
    cfg = _host.load_cfg()
    changed = {"base_url": [], "api_key": []}
    for section, role, profile in _profiles(cfg):
        if base_url and not (profile.get("base_url") or "").strip():
            profile["base_url"] = base_url
            changed["base_url"].append("%s.%s" % (section, role))
        if api_key:
            profile["api_key"] = api_key
            changed["api_key"].append("%s.%s" % (section, role))
    assistant = cfg.setdefault("assistant", {})
    # 「默认 API」是用户显式填的，assistant 段要**覆盖**写入；
    # profile 那些则是"仅补空值"（别把各角色已配好的地址冲掉）。
    if base_url and (assistant.get("base_url") or "").strip() != base_url:
        assistant["base_url"] = base_url
        changed["base_url"].append("assistant")
    if api_key:
        assistant["api_key"] = api_key
        changed["api_key"].append("assistant")
    # AI 助手是否使用这份配置，取决于 base_url 与 model 都非空
    # （core/assistant.py resolve_backend 的首条判定），所以保存时顺带补 model：
    # 优先沿用 assistant.model，其次取已有 profile 里的模型名。
    # 补 model 时只认 base_url 对得上的 profile：否则会把这个地址用不了的模型名写进
    # assistant（实测踩过：deepseek 的地址配了 mimo 的模型名 → 助手 HTTP 400）。
    if not (assistant.get("model") or "").strip() and base_url:
        for _s, _r, prof in _profiles(cfg):
            if ((prof.get("base_url") or "").strip().rstrip("/") == base_url.rstrip("/")
                    and (prof.get("model") or "").strip()):
                assistant["model"] = prof["model"].strip()
                changed.setdefault("model", []).append("assistant")
                break
    _host.save_cfg(cfg)
    return {"ok": True, "changed": changed, "backup": bak,
            "note": "api_key 已写入 %d 个 profile（值不回显）" % len(changed["api_key"])}


def _reset_host_state() -> None:
    """清空托管页的内存状态（用户 2026-10-18：新工程不该带着上个工程的方案/决策/日志）。"""
    try:
        import host as _hostmod  # type: ignore
        _hostmod.reset_state()
    except Exception:                               # noqa: BLE001
        pass


def set_project(body: dict) -> dict:
    path = str(body.get("path") or "").strip()
    # 2026-10-23（用户）：打开工程前先审查 .eproj 工程文件，没有就不许打开
    try:
        path = _host.require_project(path)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    bak = _backup_config()
    cfg = _host.load_cfg()
    old_root = _host.current_root(cfg)                      # 切换前先记下旧工程根（路径改绑用；旧工程可能已被删）
    from core.project import push_recent  # type: ignore
    push_recent(cfg, path)
    (cfg.setdefault("app", {}))["project_closed"] = False   # 选/建工程 = 重新打开（用户 2026-10-17）
    info = _host.switch_project(cfg, old_root, os.path.abspath(path))   # 训练/采集路径改绑到新工程
    _host.save_cfg(cfg)
    _reset_host_state()                                     # 托管内存状态不能跨工程
    proj = _host.project(_host.load_cfg())
    return {"ok": True, "project": {"root": proj.root, "name": proj.name},
            "recent": [p for p in (cfg.get("app") or {}).get("recent_projects") or []],
            "rebound": info.get("rebound", 0), "backup": bak}


def create_project(body: dict) -> dict:
    path = str(body.get("path") or "").strip()
    name = str(body.get("name") or "").strip()
    if not path:
        return {"ok": False, "error": "需要 path（工程目录）"}
    # 2026-10-23（用户）：这个文件夹已经是工程就别再建（会覆盖原工程文件）
    if _host.project_marker(path):
        nm = os.path.basename(os.path.abspath(path).rstrip("\\/")) or path
        return {"ok": False, "error": "「%s」已经是 EAI 工程，用「切换工程」打开" % nm}
    from core.project import create_project as _create, push_recent  # type: ignore
    proj = _create(path, name)
    cfg = _host.load_cfg()
    old_root = _host.current_root(cfg)                      # 新建前先记下旧工程根（路径改绑用；旧工程可能已被删）
    push_recent(cfg, proj.root)
    (cfg.setdefault("app", {}))["project_closed"] = False
    info = _host.switch_project(cfg, old_root, proj.root)    # 训练/采集路径改绑到新工程
    _host.save_cfg(cfg)
    _reset_host_state()
    _host.hold_project(proj.root)                           # 新工程立刻占住（2026-10-23）：删不掉/移不走
    return {"ok": True, "project": {"root": proj.root, "name": proj.name},
            "rebound": info.get("rebound", 0),
            "note": "已创建工程骨架（data/ checkpoints/ logs/ exports/ + 工程名.eproj）"}


def close_project(_body: dict) -> dict:
    """关闭当前工程（用户 2026-10-17，侧栏工程块右上角的 ×）：
    只在 config 里记一个 project_closed 标记，不动磁盘、不删 recent_projects——
    关掉后前端把内容变灰并弹「创建工程 / 导入工程」，再选工程时标记自动清掉。"""
    bak = _backup_config()
    cfg = _host.load_cfg()
    proj = _host.project(cfg)
    (cfg.setdefault("app", {}))["project_closed"] = True
    _host.save_cfg(cfg)
    _host.release_project()                     # 关掉工程就别再占着文件夹（2026-10-23）
    return {"ok": True, "closed": {"root": proj.root, "name": proj.name}, "backup": bak}


def open_project(_body: dict) -> dict:
    proj = _host.project()
    try:
        os.startfile(proj.root)                 # noqa: S606  在资源管理器里打开
        return {"ok": True, "opened": proj.root}
    except Exception as exc:                    # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def api_test(body: dict) -> dict:
    """测试 API 连通性（真发请求，不编造结果）。

    第一跳 GET {base_url}/models（快且不耗 token）；它 404/405/400 时退到第二跳，
    发一个 max_tokens=1 的最小 chat 请求。密钥优先用表单里刚填的，其次用已存的
    assistant 段，再其次任意 profile —— **绝不回显密钥**。
    """
    import json as _json
    import time as _t
    import urllib.error
    import urllib.request

    cfg = _host.load_cfg()
    assistant = cfg.get("assistant") or {}
    base = (str(body.get("base_url") or "").strip()
            or (assistant.get("base_url") or "").strip()).rstrip("/")
    key = _resolve_key(cfg, base, str(body.get("api_key") or ""))
    if not base:
        return {"ok": False, "error": "没有 base_url 可测（先填地址或先保存）"}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key

    def _one(url: str, data: bytes | None, timeout: float):
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")

    t0 = _t.time()
    try:
        status, text = _one(base + "/models", None, 15.0)
        n = 0
        try:
            payload = _json.loads(text)
            data = payload.get("data") if isinstance(payload, dict) else None
            n = len(data) if isinstance(data, list) else 0
        except Exception:                                     # noqa: BLE001
            n = 0
        return {"ok": True, "route": "/models", "http": status, "models": n,
                "ms": int((_t.time() - t0) * 1000), "has_key": bool(key), "base_url": base}
    except urllib.error.HTTPError as exc:
        if exc.code not in (400, 404, 405):                   # 401/403 就是终局结论
            return {"ok": False, "route": "/models", "http": exc.code,
                    "ms": int((_t.time() - t0) * 1000),
                    "error": "HTTP %d %s" % (exc.code, exc.reason)}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "route": "/models",
                "ms": int((_t.time() - t0) * 1000),
                "error": "%s: %s" % (type(exc).__name__, exc)}
    t1 = _t.time()
    model = (assistant.get("model") or "").strip() or "gpt-4o-mini"
    payload = _json.dumps({"model": model,
                           "messages": [{"role": "user", "content": "ping"}],
                           "max_tokens": 1}).encode("utf-8")
    try:
        status, text = _one(base + "/chat/completions", payload, 25.0)
        return {"ok": True, "route": "/chat/completions", "http": status, "model": model,
                "ms": int((_t.time() - t1) * 1000), "has_key": bool(key), "base_url": base}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:                                     # noqa: BLE001
            pass
        return {"ok": False, "route": "/chat/completions", "http": exc.code,
                "ms": int((_t.time() - t1) * 1000),
                "error": ("HTTP %d %s · %s" % (exc.code, exc.reason, detail)).strip()}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "route": "/chat/completions",
                "ms": int((_t.time() - t1) * 1000),
                "error": "%s: %s" % (type(exc).__name__, exc)}


def _resolve_key(cfg: dict, base: str, given: str = "") -> str:
    """测连通 / 列模型用的密钥：显式给的 > assistant 段（地址一致）> 任意地址一致的 profile。

    只借 base_url 对得上的那份 —— 否则会拿 A 家的密钥去测 B 家的地址，报个 401 让人误以为
    地址填错了。**绝不回显密钥。**
    """
    key = str(given or "").strip()
    assistant = cfg.get("assistant") or {}
    if not key and (assistant.get("base_url") or "").strip().rstrip("/") == base:
        key = (assistant.get("api_key") or "").strip()
    if not key:
        for _s, _r, prof in _profiles(cfg):
            if ((prof.get("base_url") or "").strip().rstrip("/") == base
                    and (prof.get("api_key") or "").strip()):
                key = str(prof["api_key"]).strip()
                break
    return key


def _model_ids(payload: Any) -> list:
    """从 /models 响应里稳妥取模型 id（OpenAI 风格 data[*].id 为主）。"""
    items = None
    if isinstance(payload, dict):
        for field in ("data", "models", "result"):
            if isinstance(payload.get(field), list):
                items = payload[field]
                break
    elif isinstance(payload, list):
        items = payload
    out: list = []
    for it in items or []:
        if isinstance(it, str):
            mid = it.strip()
        elif isinstance(it, dict):
            mid = str(it.get("id") or it.get("name") or it.get("model") or "").strip()
        else:
            mid = ""
        if mid and mid not in out:
            out.append(mid)
    return out


def models_list(body: dict) -> dict:
    """列出 base_url 上的模型 id（真请求 GET {base_url}/models，给页面做下拉框）。

    body: {"base_url"?, "api_key"?}；密钥解析与 api_test 同口径。不支持 /models 的服务
    （400/404/405/501）照实返回 ok=False + note，页面据此保留手填输入框。
    """
    import json as _json
    import time as _t
    import urllib.error
    import urllib.request

    body = body or {}
    cfg = _host.load_cfg()
    assistant = cfg.get("assistant") or {}
    base = (str(body.get("base_url") or "").strip()
            or (assistant.get("base_url") or "").strip()).rstrip("/")
    if not base:
        return {"ok": False, "error": "没有 base_url（先填地址或先保存）"}
    key = _resolve_key(cfg, base, str(body.get("api_key") or ""))
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    t0 = _t.time()
    try:
        req = urllib.request.Request(base + "/models", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            status = resp.status
            text = resp.read().decode("utf-8", "replace")
        ids = _model_ids(_json.loads(text))
        return {"ok": True, "base_url": base, "http": status, "models": ids,
                "count": len(ids), "ms": int((_t.time() - t0) * 1000),
                "has_key": bool(key)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "base_url": base, "http": exc.code,
                "ms": int((_t.time() - t0) * 1000),
                "error": "HTTP %d %s" % (exc.code, exc.reason),
                "note": ("该服务不支持 /models 列表，请手填模型名"
                         if exc.code in (400, 404, 405, 501) else None)}
    except Exception as exc:                    # noqa: BLE001
        return {"ok": False, "base_url": base, "ms": int((_t.time() - t0) * 1000),
                "error": "%s: %s" % (type(exc).__name__, exc)}


def register(router) -> None:
    router.get("/api/settings/state", state)
    router.get("/api/settings/device", device)
    router.post("/api/settings/device", set_device)
    router.post("/api/settings/app", set_app)
    router.post("/api/settings/api", set_api)
    router.post("/api/settings/api/test", api_test)
    router.post("/api/settings/models", models_list)
    router.post("/api/settings/project", set_project)
    router.post("/api/settings/project/close", close_project)
    router.post("/api/settings/project/create", create_project)
    router.post("/api/settings/project/open", open_project)
