#!/usr/bin/env python3
"""EAI Studio · 桥的**引擎宿主**（下划线开头 → 插件加载器自动跳过，不当页面接口用）

给 `bridge_api/<page>.py` 提供"真的能干活"的后端能力，全部复用 ENGINE_DIR 自己的引擎，
不重写一套：

  - 配置：core.config（load_config / save_config，与 App 同一份 config.json、同一套默认值合并）
  - 工程：core.project（Project / FOLDERS / 语料文件名；当前工程 = config.app.recent_projects[0]）
  - 训练：**子进程** `python -u <ENGINE_DIR>/core/train.py --config <runtime.json>`，
          优雅停止 = 往 `<工程>/logs/stop.flag` 写 1（与 pages/train.py 完全一致）
  - 采集 / 推理：**进程内线程**（core.article_collector / response_collector / transformer），
          事件通过环形缓冲交给页面轮询

插件用法（bridge_api/ 已在 sys.path）：

    import _host
    def register(router):
        router.post("/api/xxx/start", lambda body: _host.start_training("pretrain", "full", body))
        router.get("/api/xxx/status", lambda q: _host.training_status("pretrain", int(q.get("since") or 0)))

线程/进程宿主都是"同名只留最近一个"，页面用 `since` 游标增量取日志，不会重复也不会丢。
"""

from __future__ import annotations

import collections
import copy
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

# 2026-10-24：原子写助手（见下方 _write_json_atomic）。None = 还没探测过；False = 导入不到，退回直写。
_ATOMIC_WRITER = None


def _write_json_atomic(path: str, data: Any, indent: int = 2) -> None:
    """原子写 JSON：走 core/atomic.py，和 config.json 同一套（临时文件 + fsync + os.replace）。

    core 包要等 bridge 把 engine 目录挂上 sys.path 才 import 得到（同本文件 :203 的写法），
    所以这里延迟导入并缓存结果；万一导入不到（单独跑某个工具脚本），退回直写也不至于崩。
    """
    global _ATOMIC_WRITER
    if _ATOMIC_WRITER is None:
        try:
            from core import atomic as _a          # noqa: PLC0415
            _ATOMIC_WRITER = _a.write_json
        except Exception:                           # noqa: BLE001
            _ATOMIC_WRITER = False
    if _ATOMIC_WRITER:
        _ATOMIC_WRITER(path, data, indent=indent)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def _log_exc(tag: str, exc: Any, **fields: Any) -> None:
    """兜底异常落盘（2026-10-24，排查报告第 2 条）：核心实现在 core/errlog.py。

    `core` 要等 bridge 把 engine 挂进 sys.path 才 import 得到 —— 同 _write_json_atomic 的懒加载。
    """
    try:
        from core import errlog                         # noqa: PLC0415
        errlog.log_exc(tag, exc, **fields)
    except Exception:                                   # noqa: BLE001
        pass


def _read_json_atomic(path: str, default: Any = None) -> Any:
    """读 JSON（2026-10-24）：和 _write_json_atomic 共用一把锁，避免「读的时候正好在替换文件」。"""
    try:
        from core import atomic as _a              # noqa: PLC0415
        return _a.read_json(path, default)
    except Exception:                               # noqa: BLE001
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:                           # noqa: BLE001
            return default


# ---------------------------------------------------------------- 路径

HOST_FILE = os.path.abspath(__file__)
API_DIR = os.path.dirname(HOST_FILE)             # ...\EAI\EAI_Studio\bridge_api
WEB_DIR = os.path.dirname(API_DIR)               # ...\EAI\EAI_Studio
EAI_ROOT = os.path.dirname(WEB_DIR)              # ...\EAI


# ---------------------------------------------------------------- 设备解析（唯一口径）

def default_project_root() -> str:
    """旧数据安置点：<用户目录>/EAI Projects/default。目录不存在时自动创建。

    2026-10-21 撤兜底：它**不再**是「没有工程时的兜底工程」，只是迁移旧版散落数据的落点；
    工程解析不再回落到这里（用户 2026-10-21）。

    以前是 EAI_Studio/engine/projects/default —— 工程数据落在软件目录里，用户 2026-10-18 要求挪出。
    """
    root = os.path.join(USER_PROJECTS_DIR, "default")
    try:
        os.makedirs(root, exist_ok=True)
        return root
    except OSError:
        return os.path.join(LEGACY_PROJECTS_DIR, "default")


def _merge_move(src: str, dst: str) -> int:
    """把 src 下的文件/目录合并进 dst（同名不覆盖），返回搬走的条目数。"""
    import shutil                                        # noqa: PLC0415
    moved = 0
    if not os.path.isdir(src):
        return 0
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        try:
            if os.path.isdir(s):
                if os.path.isdir(d):
                    moved += _merge_move(s, d)
                else:
                    shutil.move(s, d)
                    moved += 1
            elif not os.path.exists(d):
                shutil.move(s, d)
                moved += 1
        except OSError:
            continue
    return moved


def model_log(cfg: Optional[dict], op: str, name: str, path: str) -> None:
    """模型操作日志账（用户 2026-10-21）：<工程>/data/_model_logs.json，供总览日志显示。

    op 口径：「转换导入」= 转换结果进工程（把模型加进来）；「转换导出」= 工程内权重转出去；
    「转换」= 其余转换；「导出」= 导出模型（导出就是导出）。
    写失败不影响主流程。
    """
    try:
        root = project_root(cfg)
    except Exception:                                # noqa: BLE001  没工程就写不了日志
        return
    if not root or not name:
        return
    p = os.path.join(root, "data", "_model_logs.json")
    try:
        items = json.load(open(p, "r", encoding="utf-8")) if os.path.isfile(p) else []
        if not isinstance(items, list):
            items = []
    except Exception:                                # noqa: BLE001  坏档 = 从头记
        items = []
    items.append({"op": op, "name": name, "path": path or "", "mtime": time.time()})
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(items[-500:], f, ensure_ascii=False)
    except Exception:                                # noqa: BLE001  日志写失败不影响主流程
        pass


def migrate_engine_data() -> dict:
    """把旧版散在软件目录里的工程数据搬到兜底工程（一次性，幂等）。

    只搬运「工程自身产生」的东西：工程目录树、权重、训练日志、runtime 快照、示例语料。
    config.json / config_backups/ / torch_backend.json / state.json 属于应用级配置，留在软件目录。
    """
    dst = default_project_root()
    report = {}
    pairs = [
        (os.path.join(LEGACY_PROJECTS_DIR, "default"), dst),
        (os.path.join(IDE_DIR, "checkpoints"), os.path.join(dst, "checkpoints")),
        (os.path.join(IDE_DIR, "logs"), os.path.join(dst, "logs")),
        (os.path.join(IDE_DIR, "configs"), os.path.join(dst, "logs", "runtime")),
        (os.path.join(IDE_DIR, "data"), os.path.join(dst, "data")),
    ]
    try:
        for src, tgt in pairs:
            n = _merge_move(src, tgt)
            if n:
                report[os.path.relpath(src, IDE_DIR)] = n
                try:
                    os.rmdir(src)
                except OSError:
                    pass
    except Exception as exc:                             # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
    if report:
        try:
            cfg = load_cfg()
            old = os.path.join(LEGACY_PROJECTS_DIR, "default")
            # 旧兜底工程的绝对路径 → 新位置
            rebind_paths(cfg, [old], dst)
            rec = [dst if (isinstance(x, str) and os.path.abspath(x) == os.path.abspath(old)) else x
                   for x in ((cfg.get("app") or {}).get("recent_projects") or [])]
            # 2026-10-21 撤兜底：default 只当旧数据安置点，不再自动进 recent_projects
            # 冒充「已选工程」——没建工程就是没建，前端按 project_chosen 弹「创建工程 / 导入工程」。
            cfg.setdefault("app", {})["recent_projects"] = rec
            # project_ui 已改存工程自己的 ui_state.json，这里只做一次迁移搬运
            legacy_ui = (cfg.get("app") or {}).get("project_ui") or {}
            for k in list(legacy_ui):
                if k == os.path.normcase(os.path.abspath(old)):
                    try:
                        save_ui_state(dst, legacy_ui.get(k) or {})
                    except Exception:                    # noqa: BLE001
                        pass
                    legacy_ui.pop(k, None)
            save_cfg(cfg)
        except Exception:                                # noqa: BLE001
            pass
    return report


def pick_device(want: str = "auto") -> str:
    """训练 / 推理 / 总览**共用**的设备解析（懒 import torch，不拖慢桥启动）。

    规则：显式指定且可用 → 用它（cpu 永远可用）；否则 auto = CUDA → XPU → MPS → CPU。
    与 `EAI_Studio/engine/core/train.py` 的 pick_device 同口径 —— 别再各写一份，改这里就全站生效。
    """
    want = str(want or "auto").strip().lower()
    if want not in ("auto", "cpu", "cuda", "xpu", "mps"):
        want = "auto"
    try:
        import torch  # noqa: PLC0415
        avail = {
            "cuda": bool(torch.cuda.is_available()),
            "xpu": bool(getattr(torch, "xpu", None) and torch.xpu.is_available()),
            "mps": bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()),
            "cpu": True,
        }
    except Exception:                            # noqa: BLE001
        return "cpu"
    note_torch_backend(torch)                    # 落盘：设置页/工作台总览据此免 torch 秒回
    if want != "auto" and avail.get(want):
        return want
    for name in ("cuda", "xpu", "mps", "cpu"):
        if avail.get(name):
            return name
    return "cpu"


def note_torch_backend(torch_mod: Any) -> None:
    """把这次真探测的结果落盘 engine/torch_backend.json（实现见 engine/core/backend_marker.py）。

    读它的地方是设置页设备卡（`settings.device`）与工作台总览（`bridge.device_info`）——
    那两处因此不必 import torch；torch（实测工作集 830MB）只在训练 / 对话真用到时才加载。
    """
    try:
        ensure_ide_path()
        from core import backend_marker      # type: ignore
        backend_marker.note(torch_mod)
    except Exception:                        # noqa: BLE001  落盘失败不影响设备解析
        pass


IDE_DIR = os.path.join(WEB_DIR, "engine")                # 2026-10-23：不再假设目录名叫 EAI_Studio，改名/换盘都成立

# ---------------------------------------------------------------- 数据落盘位置
# 用户要求（2026-10-18）：每个工程产生的**一切数据**都只放各自的工程文件夹，
# 软件目录（EAI_Studio/engine）里不再存工程数据。历史上这些散在软件目录里：
#   engine/projects/default/   兜底工程（chats/checkpoints/data/logs/exports + default.eproj）
#   engine/checkpoints/*.pt    旧权重
#   engine/logs/*.csv          旧训练日志
#   engine/configs/*.json      旧 runtime 快照
#   engine/data/*.jsonl        旧示例语料
# 现在兜底工程改到 <用户目录>/EAI Projects/default，并在启动时把这些旧数据整体搬过去。
USER_PROJECTS_DIR = os.path.join(os.path.expanduser("~"), "EAI Projects")
LEGACY_PROJECTS_DIR = os.path.join(IDE_DIR, "projects")
CONFIG_PATH = os.path.join(IDE_DIR, "config.json")
TRAIN_SCRIPT = os.path.join(IDE_DIR, "core", "train.py")
RUN_PY = sys.executable                          # 桥就是 venv 的 python（有 torch）

MAX_LINES = 4000                                 # 每个作业保留的日志行数


def ensure_ide_path() -> None:
    """把 EAI_Studio/engine 放进 sys.path，让 core.* 可导入（和 App 一样）。"""
    if IDE_DIR not in sys.path:
        sys.path.insert(0, IDE_DIR)


# ---------------------------------------------------------------- 配置 / 工程

def load_cfg() -> dict:
    """读 config.json（缺失/损坏回落到默认值，与 App 行为一致）。"""
    ensure_ide_path()
    from core.config import load_config  # type: ignore
    return load_config(CONFIG_PATH)


def save_cfg(cfg: dict) -> None:
    ensure_ide_path()
    from core.config import save_config  # type: ignore
    save_config(CONFIG_PATH, cfg)




# ---------- 工程切换：路径改绑（用户 2026-10-18） ----------
# 现象：新建/切换工程后，训练页仍显示旧工程的输出权重、语料体量、Loss 行、基座权重。
# 根因：config.train 里的 pretrain_corpus / *_out / *_init_from / *_log_csv / data_roots
# 以及 article/response 采集的 output_dir 都是**绝对路径**，全都指向上一个工程；切工程只换了
# app.recent_projects，没人改这些路径。这里在切换时把「旧工程根下的路径」整体改绑到新工程根。
def _under(path: str, root: str) -> bool:
    try:
        p = os.path.normcase(os.path.abspath(path))
        r = os.path.normcase(os.path.abspath(root))
        return p == r or p.startswith(r + os.sep)
    except Exception:                       # noqa: BLE001
        return False


def _same(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:                       # noqa: BLE001
        return False


def recent_roots(cfg: dict) -> list:
    """config 里出现过的工程根（recent_projects）。路径改绑按「谁包含这个路径」来判，不能只看当前工程。"""
    out = []
    for item in ((cfg.get("app") or {}).get("recent_projects") or []):
        if isinstance(item, str) and item:
            try:
                out.append(os.path.abspath(item))
            except Exception:               # noqa: BLE001
                pass
    return out


def rebind_paths(cfg: dict, roots, new_root: str) -> int:
    """把 config 里落在任一旧工程根下的绝对路径改绑到新工程根（保持相对位置）。返回改动条数。

    roots 接受单个根或根列表：配置里的路径可能指向上一个工程，也可能指向更早的工程
    （例如当前工程是 A、但路径还停在 B），所以要按「哪个根包含它」来匹配，最长根优先。
    """
    if isinstance(roots, str):
        roots = [roots]
    new_abs = os.path.abspath(new_root)
    cand = []
    for r in (roots or []):
        if not r:
            continue
        try:
            ra = os.path.abspath(r)
        except Exception:                   # noqa: BLE001
            continue
        if not _same(ra, new_abs):
            cand.append(ra)
    cand.sort(key=len, reverse=True)        # 工程嵌套时取最靠近的那个根
    if not cand:
        return 0
    changed = [0]

    def rebind(v: str):
        for r in cand:
            if _under(v, r):
                rel = os.path.relpath(os.path.abspath(v), r)
                changed[0] += 1
                return new_abs if rel in (".", "") else os.path.join(new_abs, rel)
        return None

    def walk(node) -> None:
        if isinstance(node, dict):
            for k in list(node.keys()):
                v = node[k]
                if isinstance(v, str):
                    nv = rebind(v)
                    if nv is not None:
                        node[k] = nv
                else:
                    walk(v)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str):
                    nv = rebind(v)
                    if nv is not None:
                        node[i] = nv
                else:
                    walk(v)

    app = cfg.get("app") or {}
    recent = app.get("recent_projects")
    if isinstance(recent, list):
        app["recent_projects"] = []          # 历史列表不动（否则记录会被改成新工程）
    try:
        walk(cfg)
    finally:
        if isinstance(recent, list):
            app["recent_projects"] = recent
    return changed[0]


def reset_project_dirs(cfg: dict, root: str) -> None:
    """把训练/采集的输入输出钉到新工程，并清掉指向旧工程文件的「已选权重」。"""
    def j(*parts: str) -> str:
        return os.path.join(root, *parts)

    art, resp, corp = j("data", "article"), j("data", "response"), j("data", "corpora")
    train = cfg.setdefault("train", {})
    train["data_roots"] = [art, resp, corp]
    train["pretrain_corpus"] = os.path.join(corp, "pretrain_corpus.txt")
    train["sft_corpus"] = os.path.join(corp, "sft_corpus.txt")
    train["pretrain_log_csv"] = j("logs", "pretrain_loss.csv")
    train["sft_log_csv"] = j("logs", "sft_loss.csv")
    train["lora_log_csv"] = j("logs", "lora_loss.csv")
    # 基座/初始权重：新工程里那些文件并不存在，留着会让页面显示旧工程的权重名
    for key in ("sft_init_from", "continue_init_from"):
        if train.get(key):
            train[key] = ""
    for sect, sub, out in (("article", "collection", art), ("response", "collection", resp)):
        node = cfg.setdefault(sect, {}).setdefault(sub, {})
        node["output_dir"] = out


# ---------- 采集参数按工程隔离（用户 2026-10-18） ----------
# 现象：新建工程后「采集中心」的主题里还有别的工程填的「明朝历史/秦朝历史」。
# 根因：article.collection / response.collection（主题、数量、数据集名…）存在全局 config.json，
# 切工程只换 recent_projects，这些值被所有工程共用。
# 做法：切工程时把当前工程的采集参数快照进 config.app.project_ui[<工程根>]，
# 再加载目标工程的快照（没有快照 = 回到默认：主题为空）。
# 2026-10-18 补：流水线步骤（pipeline.steps）同样存在全局 config.json，
# 新建/切换工程后「训练流水线」还留着上一个工程的步骤和输出权重名 —— 一并按工程快照。
COLLECT_KINDS = ("article", "response")

# 训练页的「输出权重名」也属于工程（用户 2026-10-18）：
# 现象 —— 新建工程从没选过模型，对话验证却显示 eproai_pretrain3.pt / 你.pt。
# 根因 —— 这四个名字存在全局 config.train 里，reset_project_dirs 只重置语料/日志路径，
#         没有处理输出权重名，于是上一个工程起的名字被新工程继承。
TRAIN_OUT_KEYS = ("pretrain_out", "sft_out", "continue_out", "lora_out")
# 对话验证页的工程级参数（生成长度 / 上下文长度，用户 2026-10-18）
CHAT_PARAM_KEYS = ("sample_chars", "chat_max_len")


# 2026-10-18（用户要求）：这些「只对当前工程有意义」的 UI 快照不再写进软件目录的 config.json，
# 改存工程自己的 <工程根>/state/ui_state.json（2026-10-19 起；工程根只留 .eproj 一个文件）；
# config.app.project_ui 只作为老数据来源，读到就搬走。
# 2026-10-19（用户要求）：工程根目录里只能有 <工程名>.eproj 一个文件，其余全是文件夹，
# 所以 UI 快照挪进 <工程根>/state/ui_state.json；老位置读到就地搬过去。
UI_STATE_DIR = "state"
UI_STATE_FILE = "ui_state.json"


def ui_state_path(root: str) -> str:
    root = os.path.abspath(root)
    new_path = os.path.join(root, UI_STATE_DIR, UI_STATE_FILE)
    old_path = os.path.join(root, UI_STATE_FILE)
    if os.path.isfile(old_path) and not os.path.isfile(new_path):
        try:
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            os.replace(old_path, new_path)
        except OSError:
            pass
    return new_path


def migrate_corpus_paths() -> dict:
    """把配置里残留的 <工程>/data/corpora/<语料> 改写成 <工程>/corpora/<语料>（用户 2026-10-19）。

    合并产物已挪出 data/，老配置里的绝对路径会让训练页「语料体量」显示缺失、训练也读不到。
    """
    cfg = load_cfg()
    train = cfg.get("train")
    if not isinstance(train, dict):
        return {}
    moved = {}
    for key in ("pretrain_corpus", "sft_corpus"):
        value = str(train.get(key) or "").strip()
        if not value:
            continue
        norm = value.replace("\\", "/")
        if "/data/corpora/" not in norm:
            continue
        root = norm.split("/data/corpora/")[0]
        name = norm.rsplit("/", 1)[-1]
        new_path = os.path.join(root.replace("/", os.sep), "corpora", name)
        if os.path.isfile(new_path) or not os.path.isfile(value):
            train[key] = new_path
            moved[key] = new_path
    if moved:
        save_cfg(cfg)
    return moved


def register_project_icon(ico_path: str) -> dict:
    """让 .eproj 在资源管理器里显示成 EAI 应用图标（用户 2026-10-19）。

    只写 HKCU 当前用户级关联（不碰系统级、不需要管理员），ProgID = EAIStudio.Project，
    DefaultIcon 指向应用自带的 assets/eai.ico；写完通知 shell 刷新图标缓存。
    """
    if os.name != "nt":
        return {"ok": False, "error": "非 Windows，跳过"}
    try:
        import winreg
    except ImportError:
        return {"ok": False, "error": "无 winreg"}
    if not os.path.isfile(ico_path):
        return {"ok": False, "error": "图标文件不存在：%s" % ico_path}
    prog_id = "EAIStudio.Project"
    classes = r"Software\Classes"
    sep = "\\"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, classes + sep + ".eproj") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, prog_id)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, classes + sep + prog_id) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "EAI Studio 工程")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, classes + sep + prog_id + sep + "DefaultIcon") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, '"%s",0' % os.path.abspath(ico_path))
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass
    return {"ok": True, "prog_id": prog_id, "icon": os.path.abspath(ico_path)}


def load_ui_state(root: str) -> dict:
    """读工程自己的 UI 快照；老版本存在 config.app.project_ui 里的顺手迁到工程目录。"""
    if not root or not os.path.isdir(root):
        return {}
    path = ui_state_path(root)
    if os.path.isfile(path):
        data = _read_json_atomic(path, {})          # 读也拿锁（2026-10-24）
        return data if isinstance(data, dict) else {}
    try:
        cfg = load_cfg()
        ui = (cfg.get("app") or {}).get("project_ui") or {}
        key = os.path.normcase(os.path.abspath(root))
        data = ui.get(key) or ui.get(os.path.abspath(root)) or {}
        if isinstance(data, dict) and data:
            save_ui_state(root, data)
            for k in (key, os.path.abspath(root)):
                ui.pop(k, None)
            save_cfg(cfg)
            return data
    except Exception:                                    # noqa: BLE001
        pass
    return {}


def save_ui_state(root: str, data: dict) -> None:
    if not root or not os.path.isdir(root) or not isinstance(data, dict):
        return
    try:
        path = ui_state_path(root)
        _write_json_atomic(path, data, 2)          # 原子写（2026-10-24）
    except OSError as exc:
        _log_exc("ui_state.save", exc, root=root)  # 快照写不下去要能查（2026-10-24）
        pass


def _default_train_out() -> dict:
    tr = _defaults_train()
    return {k: (tr.get(k) or "") for k in TRAIN_OUT_KEYS}


def _defaults_train() -> dict:
    ensure_ide_path()
    from core.config import DEFAULT_CONFIG  # type: ignore
    return DEFAULT_CONFIG.get("train") or {}


def _default_collection(kind: str) -> dict:
    from core.config import DEFAULT_CONFIG  # type: ignore
    return dict(((DEFAULT_CONFIG.get(kind) or {}).get("collection") or {}))


def load_collect_form(root: str, kind: str) -> dict:
    """采集表单快照（用户 2026-10-19）：切页 / 切工程回来，已填内容要还在。

    与 snapshot_collect 分开存（键 collect_form），那份是「开始采集时落进 config」的口径，
    这份是「用户正在填」的口径，互不覆盖。
    """
    if not root:
        return {}
    snap = load_ui_state(root)
    forms = snap.get("collect_form")
    got = (forms or {}).get(kind) if isinstance(forms, dict) else None
    return dict(got) if isinstance(got, dict) else {}


def save_collect_form(root: str, kind: str, fields: dict) -> None:
    if not root or not isinstance(fields, dict):
        return
    snap = load_ui_state(root)
    forms = snap.get("collect_form")
    if not isinstance(forms, dict):
        forms = {}
    merged = dict(forms.get(kind) or {})
    merged.update({str(k): v for k, v in fields.items() if k != "project"})
    forms[kind] = merged
    snap["collect_form"] = forms
    save_ui_state(root, snap)


def snapshot_collect(cfg: dict, root: str) -> dict:
    """把当前采集参数记到该工程名下（output_dir 由 reset_project_dirs 重新钉）。"""
    if not root:
        return {}
    snap = load_ui_state(root)
    for kind in COLLECT_KINDS:
        coll = (cfg.get(kind) or {}).get("collection")
        if isinstance(coll, dict):
            snap[kind] = {k: v for k, v in coll.items() if k != "output_dir"}
    # 流水线步骤（2026-10-18）：步骤里带输出权重名和每步参数，必须按工程存
    pl = (cfg.get("pipeline") or {}).get("steps")
    if isinstance(pl, list):
        snap["pipeline"] = {"steps": copy.deepcopy(pl)}
    # 输出权重名（2026-10-18）：各工程自己的名字，切工程时不互相继承
    tr = cfg.get("train") or {}
    snap["train_out"] = {k: tr.get(k) for k in TRAIN_OUT_KEYS if tr.get(k)}
    # 对话验证页的两个滑条参数（用户 2026-10-18）：生成长度 / 上下文长度也按工程走
    snap["chat"] = {k: tr[k] for k in CHAT_PARAM_KEYS if tr.get(k) is not None}
    save_ui_state(root, snap)          # 落在 <工程根>/state/ui_state.json（2026-10-19 起）
    return snap


def apply_collect(cfg: dict, root: str) -> None:
    """换成目标工程的采集参数；没记录过就用默认（主题为空，不再串别的工程）。"""
    snap = load_ui_state(root)
    for kind in COLLECT_KINDS:
        coll = _default_collection(kind)
        got = snap.get(kind)
        if isinstance(got, dict):
            coll.update(got)
        cfg.setdefault(kind, {})["collection"] = coll
    # 流水线步骤（2026-10-18）：没记录过的工程 = 空流水线，不再显示上一个工程的步骤
    pl = (snap.get("pipeline") or {}).get("steps")
    cfg.setdefault("pipeline", {})["steps"] = copy.deepcopy(pl) if isinstance(pl, list) else []
    # 输出权重名（2026-10-18）：没有快照的工程回到出厂默认名，不再显示别的工程起的名字
    tr = cfg.setdefault("train", {})
    saved = snap.get("train_out") or {}
    defaults = _default_train_out()
    for k in TRAIN_OUT_KEYS:
        tr[k] = saved.get(k) or defaults.get(k) or ""
    # 对话参数：没记录的工程回到出厂值（sample_chars 120 / chat_max_len 0=跟随权重）
    saved_chat = snap.get("chat") or {}
    dflt_chat = _defaults_train()
    for k in CHAT_PARAM_KEYS:
        if k in saved_chat:
            tr[k] = saved_chat[k]
        elif dflt_chat.get(k) is not None:
            tr[k] = dflt_chat[k]


def model_defaults(kind: str, keys) -> dict:
    """三张模型卡的出厂默认值（「填充示例」= 恢复默认，用户 2026-10-18）。"""
    from core.config import DEFAULT_CONFIG  # type: ignore
    sect = DEFAULT_CONFIG.get(kind) or {}
    out = {}
    for k in keys:
        prof = sect.get(k) or {}
        out[k] = {kk: prof.get(kk) for kk in
                  ("base_url", "model", "temperature", "max_tokens", "system_prompt")}
        # 提示词按语言各存一份（用户 2026-10-20）：中文界面用 system_prompt，英文界面用 system_prompt_en
        out[k]["system_prompt_en"] = prof.get("system_prompt_en") or prof.get("system_prompt")
    return out


def switch_project(cfg: dict, old_root, new_root: str) -> dict:
    """切/建工程时要做的统一收尾：路径改绑 + 钉住输入输出 + 清宿主内存状态。"""
    roots = [old_root] + recent_roots(cfg)
    snapshot_collect(cfg, str(old_root or ""))      # 旧工程的采集参数先存起来
    moved = rebind_paths(cfg, roots, new_root)
    apply_collect(cfg, new_root)                    # 换成新工程自己的（没记录 = 空主题）
    reset_project_dirs(cfg, new_root)
    # 已选权重：改绑后新工程里没有这个文件 → 清空，页面回到「未选择」
    train = cfg.setdefault("train", {})
    for key in ("sft_init_from", "continue_init_from"):
        v = train.get(key) or ""
        if v and not os.path.isfile(v):
            train[key] = ""
    # 对话也要跟着工程走（2026-10-18）：清掉 AI 面板的内存会话，旧会话文件留在原工程
    try:
        from bridge_api import assistant as _asst
        _asst.reset_session()
    except Exception:                             # noqa: BLE001
        pass
    return {"rebound": moved, "old_root": str(old_root), "new_root": new_root}



# 检查点数量按「训练方式」各存各的（用户 2026-10-20）。
# 原来四个训练页共用 config.train.ckpt_count，设一页会把别的页也改掉。
CKPT_KEYS = {"pretrain": "pretrain_ckpt_count", "continue": "continue_ckpt_count",
             "sft": "sft_ckpt_count", "lora": "lora_ckpt_count"}


def ckpt_key(stage: str, method: str = "") -> str:
    """当前页该用哪个检查点键：LoRA 单独一份，其余按 stage。"""
    stage_s, method_s = str(stage or "").lower(), str(method or "").lower()
    if stage_s == "sft" and method_s == "lora":
        return CKPT_KEYS["lora"]
    return CKPT_KEYS.get(stage_s, "ckpt_count")


def get_ckpt_count(cfg: dict, stage: str, method: str = "") -> int:
    """读本页的检查点数量；老配置只有共用的 ckpt_count 时用它兜底（迁移）。"""
    train = cfg.get("train") or {}
    val = train.get(ckpt_key(stage, method))
    if val is None:
        val = train.get("ckpt_count")
    try:
        num = int(float(val or 0))
    except (TypeError, ValueError):
        num = 0
    return max(0, min(5, num))


def set_ckpt_count(cfg: dict, stage: str, method: str, value) -> int:
    """写本页的检查点数量（0~5），顺手清掉老的共用键。"""
    train = cfg.setdefault("train", {})
    key = ckpt_key(stage, method)
    if "ckpt_count" in train:          # 迁移：老的共用一份先拆给四页，别让别的页突然变 0
        legacy = train.get("ckpt_count")
        for k in CKPT_KEYS.values():
            train.setdefault(k, legacy)
        train.pop("ckpt_count", None)
    try:
        num = int(float(value if value is not None else 0))
    except (TypeError, ValueError):
        num = 0
    num = max(0, min(5, num))
    train[key] = num
    if key != "ckpt_count":
        train.pop("ckpt_count", None)
    return num


def apply_ckpt_count(cfg: dict, stage: str, method: str, updates: dict) -> dict:
    """把页面传来的 ckpt_count 落到本页专属键，返回挖掉该键的 updates（用户 2026-10-20）。

    页面/流水线仍然只认识一个字段名 ckpt_count，隔离在桥这一层做。
    """
    if not isinstance(updates, dict) or "ckpt_count" not in updates:
        return updates
    out = dict(updates)
    set_ckpt_count(cfg, stage, method, out.pop("ckpt_count"))
    return out


def save_train(cfg: dict, updates: dict, persist: bool = True) -> dict:
    """把页面表单的值写进 config.train（类型按 Tk 版 pages/train.py::_save 的规则转换）。

    persist=False = 只改传进来的这份 cfg（用户 2026-10-17：训练流水线的每一步参数彼此独立、
    也不能污染各训练页的配置），此时**不落盘**。
    """
    train = cfg.setdefault("train", {})
    floats = ("pretrain_lr", "sft_lr", "continue_lr", "dropout", "lora_alpha", "lora_dropout", "lora_scale",
              "lora_lr",                          # 2026-10-23：漏了这个键 → 走 else 分支 int() 掉成 0，
                                                  # 页面存一次学习率就变 0（等于没训）——实测 LoRA 页显示 0
              "grad_clip", "weight_decay", "lora_alpha_scale",
              "val_ratio", "repetition_penalty")   # 2026-10-19：指令微调页两个横条
    strings = ("lora_targets", "pretrain_corpus", "sft_corpus", "sft_init_from",
               "continue_corpus", "continue_init_from", "continue_out", "continue_log_csv",
               "pretrain_out", "sft_out", "lora_out", "pretrain_log_csv", "sft_log_csv",
               "lora_log_csv", "device", "method", "amp_dtype")
    for name, raw in (updates or {}).items():
        if name.startswith("_") or raw is None:
            continue
        if isinstance(raw, str) and raw.strip() == "\u2014":
            continue   # 前端占位破折号「—」绝不落库（显示用，不是值）
        if isinstance(raw, bool):
            train[name] = raw
        elif name in floats:
            try:
                train[name] = float(raw)
            except (TypeError, ValueError):
                train[name] = raw
        elif name in strings:
            train[name] = str(raw).strip()
        else:
            try:
                train[name] = int(float(raw))
            except (TypeError, ValueError):
                train[name] = str(raw).strip()
    if persist:
        save_cfg(cfg)
    return train


def project_marker(root: str) -> str:
    """工程文件（<名>.eproj，旧名 project.eproj 也算）的绝对路径；不是工程返回空串。

    2026-10-23（用户）：判断是不是工程只认工程文件本身，不再看有没有 data/checkpoints/chats
    这些文件夹 —— 随便一个带 data 的目录以前会被当成工程打开。
    """
    if not root or not os.path.isdir(root):
        return ""
    try:
        ensure_ide_path()
        from core.project import find_project_file  # type: ignore
        return find_project_file(root) or ""
    except Exception:                              # noqa: BLE001
        return ""


def require_project(path: str) -> str:
    """打开 / 切换工程前的审查（用户 2026-10-23）：目录里必须有 .eproj 工程文件，否则抛错。

    返回工程根的绝对路径；失败抛 RuntimeError（文案直接给前端显示）。
    """
    ap = os.path.abspath(str(path or "").strip())
    if not ap or not os.path.isdir(ap):
        raise RuntimeError("目录不存在：%s" % (path or ""))
    if not project_marker(ap):
        name = os.path.basename(ap.rstrip("\\/")) or ap
        raise RuntimeError("「%s」不是 EAI 工程：没有 .eproj 工程文件" % name)
    return ap


def is_project_dir(path: str) -> bool:
    """是不是工程目录（2026-10-18 起用于过滤 recent_projects；2026-10-23 改严格：
    必须有 .eproj 工程文件才算，带 data/checkpoints 的空壳目录不算）。"""
    return bool(project_marker(path))


# 当前工程文件夹的「占用句柄」（用户 2026-10-23）：
# 打开一个不带 FILE_SHARE_DELETE 的目录句柄后，资源管理器、原生文件夹选择框、rmdir/rename
# 全都删不掉也移不走这个文件夹（实测 WinError 32 共享冲突），工程就不会被「从选择框里删掉」。
# 只拿读权限、共享读写 —— 工程内的文件照常读写删。
_PROJ_LOCK = {"path": "", "handle": None}


def hold_project(root: str) -> None:
    """占住工程根目录，防止它被删/被移走（Windows；其它平台空实现）。"""
    if os.name != "nt" or not root:
        return
    try:
        root = os.path.abspath(root)
    except Exception:                                    # noqa: BLE001
        return
    if _PROJ_LOCK["path"] == root and _PROJ_LOCK["handle"]:
        return
    release_project()
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        h = k32.CreateFileW(root, 0x80000000, 0x1 | 0x2, None, 3, 0x02000000, None)
        if h and h != wintypes.HANDLE(-1).value:
            _PROJ_LOCK["path"], _PROJ_LOCK["handle"] = root, h
    except Exception:                                    # noqa: BLE001
        pass


def release_project() -> None:
    """放开工程根目录（关闭工程/切工程时调）。"""
    h = _PROJ_LOCK.get("handle")
    _PROJ_LOCK["handle"] = None
    _PROJ_LOCK["path"] = ""
    if not h:
        return
    try:
        import ctypes
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.CloseHandle.argtypes = [ctypes.c_void_p]
        _k32.CloseHandle(ctypes.c_void_p(h))
    except Exception:                                    # noqa: BLE001
        pass


def current_root(cfg: Optional[dict] = None) -> str:
    """当前工程根 —— 不做存在性验收（切/新建工程取「旧根」用），取不到给空串。"""
    cfg = cfg if cfg is not None else load_cfg()
    app = cfg.get("app") or {}
    for item in (app.get("recent_projects") or []):
        if isinstance(item, str) and item:
            return os.path.abspath(item)
    return ""


def project_root(cfg: Optional[dict] = None) -> str:
    """当前工程：config.app.recent_projects[0]（存在的第一个）。

    2026-10-21 撤兜底：没有工程直接抛「请先创建工程」，不再回落 <用户目录>/EAI Projects/default
    静默冒充工程——工程没创建成功就不给用（前端右侧变灰弹「创建工程 / 导入工程」）。
    """
    cfg = cfg if cfg is not None else load_cfg()
    app = cfg.get("app") or {}
    items = [p for p in (app.get("recent_projects") or []) if isinstance(p, str) and p]
    if not items:
        raise RuntimeError("请先创建工程")           # 撤兜底（2026-10-21）：没工程就没工程
    cur = items[0]
    if not is_project_dir(cur):
        # 2026-10-23（用户）：当前工程被删/被移走时**不许**悄悄换成「第 2 个最近工程」——
        # 一换就会把上一个工程的数据显示到当前工程名下（工程串了）。这里直接报错，等用户重新选。
        name = os.path.basename(os.path.abspath(cur).rstrip("\\/")) or cur
        if os.path.isdir(cur):
            raise RuntimeError("「%s」不是 EAI 工程：没有 .eproj 工程文件" % name)
        raise RuntimeError("当前工程文件夹不存在：%s" % cur)
    return os.path.abspath(cur)


def project(cfg: Optional[dict] = None):
    """core.project.Project 实例（目录树已确保存在）。"""
    ensure_ide_path()
    from core.project import open_project  # type: ignore
    root = project_root(cfg)
    os.makedirs(root, exist_ok=True)
    hold_project(root)                             # 占住工程目录（2026-10-23）：删不掉/移不走
    proj = open_project(root)
    if proj is None:                              # 兜底：理论到不了这里
        from core.project import Project  # type: ignore
        proj = Project(root)
        proj.ensure_tree()
    return proj


def in_project(path: str, root: Optional[str] = None) -> str:
    """相对路径 → 相对工程根；绝对路径原样返回。"""
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(root or project_root(), path)


def logs_dir(cfg: Optional[dict] = None) -> str:
    d = project(cfg).logs_dir
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- 进程作业（训练）

def sys_mem_bytes() -> dict:
    """本机物理内存 {total, avail}（字节）；取不到返回 0 值。

    「剩余可用内存不足 X GB」水位告警的数据源（用户 2026-10-14）。
    Windows 用 GlobalMemoryStatusEx，类 Unix 读 /proc/meminfo。
    """
    try:
        if os.name == "nt":
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            m = _MS()
            m.dwLength = ctypes.sizeof(_MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return {"total": int(m.ullTotalPhys), "avail": int(m.ullAvailPhys)}
        else:
            info: dict = {}
            with open("/proc/meminfo", "r", encoding="ascii", errors="replace") as fh:
                for ln in fh:
                    k, _, rest = ln.partition(":")
                    if k in ("MemTotal", "MemAvailable"):
                        info[k] = int(rest.split()[0]) * 1024
            return {"total": info.get("MemTotal", 0), "avail": info.get("MemAvailable", 0)}
    except Exception:                              # noqa: BLE001
        pass
    return {"total": 0, "avail": 0}


def gpu_mem_bytes(device: str = "") -> Optional[dict]:
    """CUDA 显存 {total, free, used}（nvidia-smi，字节）；非 CUDA / 取不到返回 None。

    「占用显存 / 剩余显存不足 X GB」告警的数据源（用户 2026-10-14）。
    XPU 共享内存（内存即显存）不查 GPU——按本机剩余可用内存走 sys_mem_bytes。
    """
    if str(device or "").strip().lower() != "cuda":
        return None
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total,memory.free,memory.used",
             "--format=csv,noheader,nounits"],
            text=True, encoding="utf-8", errors="replace", timeout=3,
        )
        parts = out.strip().splitlines()[0].split(",")
        total, free, used = (int(p.strip()) * 1024 * 1024 for p in parts[:3])
        return {"total": total, "free": free, "used": used}
    except Exception:                              # noqa: BLE001
        return None


def proc_mem_bytes(pid: Optional[int]) -> Optional[int]:
    """子进程工作集内存（字节）；取不到返回 None（未运行/无权限/非本平台）。

    Windows 走 psapi.GetProcessMemoryInfo；类 Unix 读 /proc/<pid>/status 的 VmRSS。
    「占用内存」瓷片的数据源（用户 2026-10-14）。
    """
    if not pid:
        return None
    # 2026-10-18：Windows 的 venv python.exe 是 launcher 桩，真正的训练解释器是它的子进程；
    # 只量桩自己会一直显示 4MB（用户看到的「占用内存」不真实）。有 psutil 就把整棵进程树加起来。
    try:
        import psutil                                  # noqa: PLC0415
        root = psutil.Process(int(pid))
        total = int(root.memory_info().rss)
        for child in root.children(recursive=True):
            try:
                total += int(child.memory_info().rss)
            except Exception:                          # noqa: BLE001
                pass
        return total
    except Exception:                                  # noqa: BLE001  没有 psutil / 进程已退出 → 走下面的原生实现
        pass
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                pmc = _PMC()
                pmc.cb = ctypes.sizeof(_PMC)
                if not ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                    return None
                return int(pmc.WorkingSetSize)
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        with open("/proc/%d/status" % int(pid), "r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) * 1024
    except Exception:                              # noqa: BLE001
        return None
    return None


class _Job:
    """一个子进程作业：捕获 stdout/stderr，行号即游标。"""

    def __init__(self, name: str, argv: list, cwd: str, group: str = "",
                 owner: str = "", log_path: str = "") -> None:
        self.name = name
        self.group = group
        self.owner = owner        # ''=用户页面起的；'host'=托管起的（训练页不显示，用户 2026-10-18）
        self.argv = argv
        self.cwd = cwd
        self.lock = threading.Lock()
        self.lines: collections.deque = collections.deque(maxlen=MAX_LINES)
        self.start_line = 0                        # 已被 deque 丢弃的行数（游标基准）
        self.pid: Optional[int] = None
        self.started_at = time.time()
        self.exit_code: Optional[int] = None
        self.error = ""
        self.stop_requested = False
        self.proc: Optional[subprocess.Popen] = None
        self.on_exit: Optional[Callable[[], None]] = None   # 进程结束回调（训练溯源记录）
        # 2026-10-23（用户）：训练输出也落盘一份 —— 桥重启/换工程后，内存里的行会没，
        # 但「训练输出」应像 Loss 日志表一样还在。文件名固定为 train_<stage>_<method>.log
        self.log_path = log_path or ""
        self.fh = None
        if self.log_path:
            try:
                os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
                self.fh = open(self.log_path, "w", encoding="utf-8", errors="replace")
            except OSError:
                self.fh = None

    # ---- 日志 ----
    def feed(self, text: str) -> None:
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        parts = text.split("\n")
        if parts and parts[-1] == "":
            parts.pop()
        with self.lock:
            for line in parts:
                self.lines.append(line)
                if self.fh is not None:            # 同步写盘（逐行 flush，进程被中途杀掉也不丢）
                    try:
                        self.fh.write(line + "\n")
                        self.fh.flush()
                    except (OSError, ValueError):
                        self.fh = None
            self.start_line = max(0, self.lines.__len__() and self._total() - len(self.lines) or 0)

    def _total(self) -> int:
        return self.start_line + len(self.lines)

    def tail(self, since: int = 0) -> tuple:
        with self.lock:
            total = self._total()
            since = max(0, min(int(since or 0), total))
            skip = max(0, since - self.start_line)
            return list(self.lines)[skip:], total

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def status(self, since: int = 0, extra: Optional[dict] = None) -> dict:
        lines, total = self.tail(since)
        out = {
            "name": self.name,
            "running": self.running,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "elapsed": round(time.time() - self.started_at, 1),
            "stop_requested": self.stop_requested,
            "lines": lines,
            "total_lines": total,
            "next": total,
            "error": self.error,
            "mem_bytes": proc_mem_bytes(self.pid) if self.running and self.pid else None,
        }
        if extra:
            out.update(extra)
        return out


_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def _spawn(name: str, argv: list, cwd: str, group: str = "", owner: str = "",
           log_path: str = "") -> _Job:
    job = _Job(name, argv, cwd, group, owner, log_path)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    job.proc = subprocess.Popen(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=flags, env=env,
    )
    job.pid = job.proc.pid
    with _JOBS_LOCK:
        _JOBS[name] = job

    def pump() -> None:
        try:
            assert job.proc is not None and job.proc.stdout is not None
            for line in job.proc.stdout:
                job.feed(line)
        except Exception as exc:                   # noqa: BLE001
            job.error = str(exc)
            _log_exc("train.pump", exc, job=name, pid=job.pid)   # 训练输出泵挂了要能查（2026-10-24）
        finally:
            try:
                job.exit_code = job.proc.wait() if job.proc else None
            except Exception:                      # noqa: BLE001
                pass
            try:
                with job.lock:                     # 训练结束：把日志文件关掉（内容已逐行 flush）
                    fh, job.fh = job.fh, None
                if fh is not None:
                    fh.close()
            except Exception:                      # noqa: BLE001
                pass
            try:
                cb = getattr(job, "on_exit", None)
                if cb:
                    cb()
            except Exception:                      # noqa: BLE001
                pass
            job.feed(f"[宿主] 进程结束，退出码 {job.exit_code}")

    threading.Thread(target=pump, daemon=True, name=f"pump-{name}").start()
    return job


def job(name: str) -> Optional[_Job]:
    with _JOBS_LOCK:
        return _JOBS.get(name)


def running_in_group(group: str, include_host: bool = True) -> Optional[str]:
    """组内正在跑的作业名。

    include_host=False（用户 2026-10-18）：跳过托管起的作业——托管训练不在训练页显示，
    「训练页自己是否在跑」按用户启动的作业算；互斥判定仍用 include_host=True。
    """
    with _JOBS_LOCK:
        for j in _JOBS.values():
            if j.group == group and j.running and j.name:
                if not include_host and getattr(j, "owner", "") == "host":
                    continue
                return j.name
    return None


TRAIN_NAV = {"pretrain": "train_pretrain", "continue": "train_continue",
             "full": "train_full", "lora": "train_lora"}


def _nav_for(stage: str, method: str) -> str:
    """作业名 → 训练中心导航键（用户 2026-10-19 修）。

    作业名是 train:<stage>:<method>：指令微调 = train:sft:full、LoRA = train:sft:lora，
    所以不能只拿 stage 去查（sft 在表里不存在 → nav 为空 → 其它训练页不变灰）。
    """
    if stage == "sft":
        return "train_lora" if method == "lora" else "train_full"
    return TRAIN_NAV.get(stage, "")


def _pipeline_running() -> bool:
    """训练流水线这个进程内任务是否在跑（用户 2026-10-22）。

    流水线不是 _JOBS 里的子进程，是 _TASKS 里的线程任务，所以要单独查——前端据此决定
    「流水线页该不该置灰」（单页训练时置灰，流水线自己在跑时不置灰）。"""
    try:
        t = task("train:pipeline")
    except Exception:                                     # noqa: BLE001
        return False
    return bool(t is not None and getattr(t, "running", False))


def train_running() -> dict:
    """训练中心互斥（用户 2026-10-19）：哪一页在训，其余训练页导航置灰不可点。

    只报 train:* 作业；托管作业有自己的遮罩（owner="host"），这里照样返回，导航仍按所属阶段算。
    `pipeline` 字段（用户 2026-10-22）：流水线自己是否在跑——在跑时流水线页不置灰，
    单页训练在跑时流水线页要跟着一起置灰、点不进。
    """
    pipe = _pipeline_running()
    with _JOBS_LOCK:
        for j in _JOBS.values():
            if not (j.running and j.name and j.name.startswith("train:")):
                continue
            parts = j.name.split(":")
            stage = parts[1] if len(parts) > 1 else ""
            method = parts[2] if len(parts) > 2 else ""
            return {"ok": True, "running": True, "name": j.name, "stage": stage, "method": method,
                    "pid": j.pid, "owner": getattr(j, "owner", ""), "nav": _nav_for(stage, method),
                    "pipeline": pipe}
    return {"ok": True, "running": False, "name": "", "stage": "", "method": "",
            "pid": None, "owner": "", "nav": "", "pipeline": pipe}


def stop_job(job_name: str, force: bool = False) -> dict:
    j = job(job_name)
    if j is None or not j.running:
        return {"ok": True, "stopped": False, "note": "没有正在运行的作业"}
    if force and j.proc is not None:
        try:
            j.proc.terminate()
        except Exception:                          # noqa: BLE001
            pass
        return {"ok": True, "stopped": True, "note": "已强制终止"}
    j.stop_requested = True
    return {"ok": True, "stopped": True, "note": "已请求优雅停止"}


# ---------------------------------------------------------------- 训练（三页共用）

def runtime_cfg(stage: str, method: str, cfg: Optional[dict] = None) -> dict:
    """复刻 pages/train.py::_runtime_stage_cfg —— 训练子进程吃的那份配置。"""
    cfg = cfg if cfg is not None else load_cfg()
    proj = project(cfg)
    train = dict(cfg.get("train") or {})
    root = proj.root

    def p(value: str, fallback: str) -> str:
        value = (value or "").strip()
        if not value:
            return os.path.join(root, fallback.replace("/", os.sep))
        return value if os.path.isabs(value) else os.path.join(root, value.replace("/", os.sep))

    def pc(value: str, name: str) -> str:
        """语料路径：合并产物已挪到 <工程>/corpora/（用户 2026-10-19）。

        老配置里存的是 <工程>/data/corpora/xxx_corpus.txt 绝对路径，这里就地改写，
        否则训练会去读一个已经被移走的老文件。"""
        path = p(value, "corpora/" + name)
        norm = path.replace("\\", "/")
        if "/data/corpora/" in norm:
            path = os.path.join(root, "corpora", norm.rsplit("/", 1)[-1])
        return path

    if stage == "pretrain":
        train.update({
            "stage": "pretrain",
            "corpus": pc(train.get("pretrain_corpus"), "pretrain_corpus.txt"),
            "steps": int(train.get("pretrain_steps", 30000) or 30000),
            "lr": float(train.get("pretrain_lr", 2e-3) or 2e-3),
            "warmup": int(train.get("pretrain_warmup", 500) or 500),
            "eval_every": int(train.get("pretrain_eval_every", 300) or 300),
            "out": p(train.get("pretrain_out"), "checkpoints/eproai_pretrain.pt"),
            "log_csv": p(train.get("pretrain_log_csv"), "logs/pretrain_loss.csv"),
            "init_from": "",
            "method": "full",
        })
    elif str(stage) == "continue":
        # 继续预训练（用户 2026-10-14）：纯文本语料从已有权重续训、全参数更新
        # （技术上即 continued pretraining / 领域自适应；语料走「预训练数据集」的合并产物）
        train.update({
            "stage": "continue",
            "corpus": pc(train.get("pretrain_corpus"), "pretrain_corpus.txt"),
            "steps": int(train.get("continue_steps", 8000) or 8000),
            "lr": float(train.get("continue_lr", 5e-4) or 5e-4),
            "warmup": int(train.get("continue_warmup", 200) or 200),
            "eval_every": int(train.get("continue_eval_every", 200) or 200),
            "init_from": p(train.get("continue_init_from") or train.get("pretrain_out"),
                           "checkpoints/eproai_pretrain.pt"),
            "out": p(train.get("continue_out"), "checkpoints/eproai_continue.pt"),
            "log_csv": p(train.get("continue_log_csv"), "logs/continue_loss.csv"),
            "method": "full",
        })
    else:
        # 2026-10-23：LoRA 与指令微调彻底分家（用户要求）—— 步数/学习率/基座各读各的键，
        # 在 LoRA 页改参数不再连带改动指令微调页（原来共用 sft_steps/sft_lr/sft_init_from）。
        is_lora = (method == "lora")
        k_steps = "lora_steps" if is_lora else "sft_steps"
        k_lr = "lora_lr" if is_lora else "sft_lr"
        k_init = "lora_init_from" if is_lora else "sft_init_from"
        d_steps = 1200 if is_lora else 8000          # LoRA 收敛快，且基座多已 SFT，步数给少
        d_lr = 2e-4 if is_lora else 5e-4
        train.update({
            "stage": "sft",
            "corpus": pc(train.get("sft_corpus"), "sft_corpus.txt"),
            "steps": int(train.get(k_steps, d_steps) or d_steps),
            "lr": float(train.get(k_lr, d_lr) or d_lr),
            "warmup": int(train.get("sft_warmup", 200) or 200),
            "eval_every": int(train.get("sft_eval_every", 200) or 200),
            "init_from": p(train.get(k_init) or train.get("pretrain_out"),
                           "checkpoints/eproai_pretrain.pt"),
            "method": "lora" if is_lora else "full",
        })
        if is_lora:
            train.update({
                "out": p(train.get("lora_out"), "checkpoints/eproai_lora.pt"),
                "log_csv": p(train.get("lora_log_csv"), "logs/lora_loss.csv"),
                "lora_r": int(train.get("lora_r", 8) or 8),
                # 默认参数按「基座已完成指令微调」给（用户 2026-10-23）：α=r（比例 1）、只挂 qkv
                "lora_alpha": float(train.get("lora_alpha", 8) or 8),
                "lora_dropout": float(train.get("lora_dropout", 0.05) or 0.05),
                "lora_targets": train.get("lora_targets") or "qkv",
            })
        else:
            train.update({
                "out": p(train.get("sft_out"), "checkpoints/eproai_model.pt"),
                "log_csv": p(train.get("sft_log_csv"), "logs/sft_loss.csv"),
            })
    # 检查点数量（用户 2026-10-19）：训练中途按等分点存中间权重。
    # 2026-10-20：按本页自己的键读（pretrain_/continue_/sft_/lora_ 各一份），四页不再互相串。
    train["ckpt_count"] = get_ckpt_count({"train": train}, stage, method)
    # 验证集比例 / 重复惩罚（用户 2026-10-19）：指令微调页两个横条，训练子进程直接吃这两个值
    try:
        train["val_ratio"] = min(0.5, max(0.02, float(train.get("val_ratio") or 0.1)))
    except (TypeError, ValueError):
        train["val_ratio"] = 0.1
    try:
        train["repetition_penalty"] = min(2.0, max(1.0, float(train.get("repetition_penalty") or 1.15)))
    except (TypeError, ValueError):
        train["repetition_penalty"] = 1.15
    return train


WEIGHT_HIST_MAX = 6
WEIGHT_HIST_LABELS = {
    "pretrain": "预训练", "continue": "继续预训练",
    "sft": "指令微调", "lora": "LoRA 微调", "merge": "LoRA 合并",
    "prune": "模型裁切", "quant": "量化",
}


def read_weight_hist(project_root: str) -> dict:
    """权重训练溯源（用户 2026-10-16）：data/_weight_hist.json → {文件名: [{"m": 训练方式, "t": 秒}, ...]}。"""
    path = os.path.join(project_root or "", "data", "_weight_hist.json")
    data = _read_json_atomic(path, {})              # 读也拿锁（2026-10-24）
    return data if isinstance(data, dict) else {}


def record_weight_hist(project_root: str, out_path: str, label: str, src: str = "") -> None:
    """产物真被本次作业写盘 → 记一条溯源；每权重只留最近 6 条（旧的先出）。失败静默不影响训练。

    src（用户 2026-10-23）：这次训练是从哪个权重开始的 —— 存文件名进 `from`，
    仓库「详细信息 → 初始权重」显示用；预训练没有来源，留空 → 界面显示「-」。
    """
    try:
        if not project_root or not out_path:
            return
        name = os.path.basename(out_path)
        path = os.path.join(project_root, "data", "_weight_hist.json")
        data = read_weight_hist(project_root)
        rows = [r for r in (data.get(name) or []) if isinstance(r, dict)]
        row = {"m": str(label or "其他"), "t": int(time.time())}
        src_name = os.path.basename(str(src or "").replace("\\", "/")) if src else ""
        if src_name:
            row["from"] = src_name
        rows.append(row)
        data[name] = rows[-WEIGHT_HIST_MAX:]
        _write_json_atomic(path, data, 1)          # 原子写（2026-10-24）
    except Exception as exc:                        # noqa: BLE001
        _log_exc("weight_hist.save", exc, out=out_path)   # 静默失败要留证据（2026-10-24）
        pass


def record_collect_use(project_root: str, kind: str, main_path: str, use: str) -> None:
    """记录蒸馏产物的「训练用途」→ data/_collect_use.json（总览日志行用；失败静默不影响采集）。

    use：语料线 pretrain|continue（训练方式），响应线 sft|lora（微调方式）。
    """
    try:
        if not project_root or not main_path:
            return
        path = os.path.join(project_root, "data", "_collect_use.json")
        data: dict = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                data = loaded if isinstance(loaded, dict) else {}
            except Exception:                        # noqa: BLE001
                data = {}
        data[os.path.basename(main_path)] = {"kind": str(kind or ""), "use": str(use or "")}
        _write_json_atomic(path, data, 1)          # 原子写（2026-10-24）
    except Exception as exc:                         # noqa: BLE001
        _log_exc("collect_use.save", exc, main=main_path)   # 静默失败要留证据（2026-10-24）
        pass


def validate_training(cfg_runtime: dict, method: str) -> Optional[str]:
    """与 Tk 版 _validate 同口径：返回错误文案或 None。"""
    stage = str(cfg_runtime.get("stage") or "")
    corpus = cfg_runtime.get("corpus") or ""
    if not corpus or not os.path.isfile(corpus):
        if stage == "continue":
            return "请先在「数据集」页的「预训练数据集」勾选纯文本文件（继续预训练吃纯文本）。"
        return "请先在「数据集」页合并对应语料。"
    init_from = cfg_runtime.get("init_from") or ""
    if init_from and not os.path.isfile(init_from):
        if method == "lora":
            return f"LoRA 需要基座权重：{init_from} 不存在，请先完成预训练。"
        if stage == "continue":
            return f"继续预训练需要基座权重：{init_from} 不存在，请先完成预训练。"
    if method == "lora" and not init_from:
        return "LoRA 需要基座权重：请先完成预训练，或在 sft_init_from 指定权重。"
    if stage == "continue" and not init_from:
        return "继续预训练必须基于已有权重：请先完成预训练，或指定初始权重。"
    return None


def _ovw_swap(proj_root: str, old: str, tmp: str, job_obj=None) -> bool:
    """覆盖初始权重（用户 2026-10-16）：完成才覆盖——旧权重更名「初始_」前缀移入回收站，新权重顶旧名旧位。
    tmp=新权重产物路径（与 old 不同名）；成功返回 True。失败不抛，写日志并尽量保持可恢复。"""
    old = str(old or "")
    tmp = str(tmp or "")
    if not old or not tmp or os.path.abspath(old) == os.path.abspath(tmp):
        return False
    if not os.path.isfile(tmp):
        return False
    if os.path.isfile(old):
        d = os.path.dirname(old)
        base = os.path.basename(old)
        backup = os.path.join(d, "初始_" + base)          # 旧模型更名带「初始」前缀
        n = 2
        while os.path.exists(backup):
            backup = os.path.join(d, "初始_%s_%d" % (base, n))
            n += 1
        os.replace(old, backup)
        try:
            import _trash
            res = _trash.file_add(proj_root, backup, kind="weights")   # 放回收站
            if not res.get("ok"):
                raise RuntimeError(str(res.get("error") or "回收站写入失败"))
        except Exception as exc:                       # noqa: BLE001
            # 入回收站失败不阻断覆盖：旧权重以「初始_」名留在原目录（可手动处理），日志照实报
            if job_obj is not None:
                job_obj.feed("[宿主] 旧权重入回收站失败（已保留 %s）：%s" % (os.path.basename(backup), exc))
    os.replace(tmp, old)                               # 新模型名称与旧模型一致（顶旧名旧位）
    return True


def start_training(stage: str, method: str, updates: Optional[dict] = None,
                   persist: bool = True, owner: str = "", runtime_tag: str = "") -> dict:
    """保存表单 → 组装 runtime 配置 → 起 core/train.py 子进程。三个训练页共用（全局互斥）。

    persist=False（用户 2026-10-17）：updates 只作用于**本次 runtime 配置**，不写回 config.json
    —— 训练流水线每一步自带全套参数，与各训练页互不影响。
    """
    group = "training"
    busy = running_in_group(group)
    if busy:
        return {"ok": False, "error": f"已有训练在运行（{busy}），请先停止。"}
    cfg = load_cfg()
    if updates:
        updates = apply_ckpt_count(cfg, stage, method, updates)   # 检查点数量按页各存各的（2026-10-20）
        save_train(cfg, updates, persist=persist)
        if persist:
            cfg = load_cfg()

    proj = project(cfg)

    # ---- 训练前自动重建对应语料（用户口径：点「开始训练」就该自动合并，不必先去数据集页手动合并）----
    # 与数据集页的合并任务**共用** bridge_api/_corpus.py：同一份扫描清单、同一套护栏、同样的字节格式。
    # 勾选按「训练方式」各存各的（数据集页四页签 2026-10-15）：
    # pretrain/continue/sft/lora 各选各的数据集；合并产物仍按两大类（pretrain=纯文本语料 / sft·lora=问答语料）
    stage_s = str(stage or "").strip().lower()
    method_s = str(method or "").strip().lower()
    kind = ("lora" if method_s == "lora" else "sft") if stage_s == "sft" else stage_s
    merge_notes: list = []
    try:
        import _corpus
        mres = _corpus.merge(
            kind, proj.root,
            emit=lambda k, p: merge_notes.append(str((p or {}).get("text") or "")),
        )
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "error": "训练前自动合并语料失败：%s: %s" % (type(exc).__name__, exc)}
    if not mres.get("ok"):
        return {"ok": False, "error": "训练前自动合并语料失败：" + str(mres.get("error") or "未知错误")}

    if mres.get("replaced"):
        merge_summary = "[宿主] 训练前自动重建 %s 语料：写入 %d 个文件 · 跳过 %d%s · %d 字节 → %s" % (
            kind, int(mres.get("written") or 0), int(mres.get("skipped") or 0),
            ("（含重复去掉 %d）" % int(mres.get("duplicates") or 0)) if int(mres.get("duplicates") or 0) else "",
            int(mres.get("out_bytes") or 0), mres.get("out"))
    elif int(mres.get("skipped") or 0) > 0:
        merge_summary = "[宿主] 训练前自动合并 %s 语料：%d 个文件都不符合本阶段格式（已跳过），沿用现有语料：%s（%d 字节）" % (
            kind, int(mres.get("skipped") or 0), mres.get("out"), int(mres.get("out_bytes") or 0))
    else:
        merge_summary = "[宿主] 训练前未发现可合并的文件，沿用现有语料：%s（%d 字节）" % (
            mres.get("out"), int(mres.get("out_bytes") or 0))

    rt = runtime_cfg(stage, method, cfg)
    # 口径（2026-10-14）：训练语料地址只来自本次「数据集合并」产物，不认 config 里的旧路径
    if mres.get("out"):
        rt["corpus"] = mres.get("out")
    err = validate_training(rt, method)
    if err:
        return {"ok": False, "error": err}

    # 覆盖初始权重（用户 2026-10-16）：勾选后「完成训练才覆盖」——训练先写临时文件 __ovw__<名>，
    # 跑完成功才把旧权重更名「初始_」前缀入回收站、新权重顶旧名（新模型名称与旧模型一致）。
    ovw_old = str(rt.get("init_from") or "").strip()
    ovw_flag = rt.get("overwrite_init")
    ovw_on = bool(ovw_flag) and str(ovw_flag).lower() not in ("0", "false", "") \
        and stage_s != "pretrain" and method_s != "lora" and bool(ovw_old)
    # 锁（用户 2026-10-20）：被锁的权重不许被覆盖，前端灰掉之外后端再挡一道
    if ovw_on:
        try:
            import repo as repo_api
            if repo_api.is_locked_path(proj.root, ovw_old):
                return {"ok": False, "error": "初始权重「%s」已锁定，不能覆盖。请先解锁，或取消「覆盖初始权重」。"
                        % os.path.basename(ovw_old)}
        except Exception:                        # noqa: BLE001
            pass
    ovw_tmp = ""
    if ovw_on:
        ovw_tmp = os.path.join(os.path.dirname(ovw_old), "__ovw__" + os.path.basename(ovw_old))
        if os.path.isfile(ovw_tmp):
            try:
                os.remove(ovw_tmp)                   # 清掉上次失败残留的临时产物
            except OSError:
                pass
        rt["out"] = ovw_tmp

    os.makedirs(proj.logs_dir, exist_ok=True)
    flag = os.path.join(proj.logs_dir, "stop.flag")
    if os.path.exists(flag):
        try:
            os.remove(flag)                        # 清掉上次的停止信号
        except OSError:
            pass
    rt_suffix = "" if persist else ("__%s" % (runtime_tag or "pipeline"))   # 不覆盖各页的 runtime 快照
    rt_path = os.path.join(proj.logs_dir,
                           f"runtime_{stage}_{rt.get('method', method)}{rt_suffix}.json")
    _write_json_atomic(rt_path, rt, 2)             # 原子写（2026-10-24）

    name = f"train:{stage}:{rt.get('method', method)}"
    argv = [RUN_PY, "-u", TRAIN_SCRIPT, "--config", rt_path]
    # 2026-10-23：训练输出同时落盘（桥重启后 训练输出 不消失，与 Loss 日志表一致）
    job_obj = _spawn(name, argv, IDE_DIR, group=group, owner=owner,
                     log_path=_train_log_path(proj, stage, rt.get("method", method)))
    # 训练溯源（用户 2026-10-16）：跑完且产物真写盘 → data/_weight_hist.json 每权重留最近 6 条
    hist_label = WEIGHT_HIST_LABELS.get("lora" if method_s == "lora" else stage_s, "其他")
    hist_cands = [str(rt.get("out") or "")]
    if method_s == "lora":
        hist_cands.append(str(rt.get("out") or "").replace(".pt", ".lora.pt"))
    hist_started = time.time()
    hist_root = proj.root

    # ---- 自动续训（用户 2026-10-19：出一次错就重试，最多 5 次）----
    MAX_RESUME = 5
    job_started = [time.time()]
    total_steps_all = int(rt.get("steps") or 0)          # 全局总步数（续训后 rt["steps"] 只剩剩余步数）

    def _done_steps() -> int:
        """已完成步数：优先读引擎随权重一起写的 <产物>.progress.json（断点真值）；
        退路是 loss CSV 最后一行。两者都只认本次运行开始之后写的，避免读到上一轮残留。"""
        out_path = str(rt.get("out") or "")
        for cand in (os.path.splitext(out_path)[0] + ".progress.json",
                     os.path.splitext(out_path)[0] + ".lora.progress.json"):
            try:
                if os.path.isfile(cand) and os.path.getmtime(cand) >= job_started[0] - 1:
                    with open(cand, "r", encoding="utf-8") as fh:
                        return max(0, int(json.load(fh).get("step") or 0))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        try:
            csvp = str(rt.get("log_csv") or "")
            if os.path.isfile(csvp) and os.path.getmtime(csvp) >= job_started[0] - 1:
                with open(csvp, "r", encoding="utf-8") as fh:
                    rows = [ln.strip() for ln in fh if ln.strip()]
                if len(rows) >= 2:
                    return max(0, int(float(rows[-1].split(",")[0])))
        except (OSError, ValueError, IndexError):
            pass
        return 0

    def _restart(cur, attempt: int) -> None:
        """崩了就接着跑：全量从 <产物>.pt 续、LoRA 从 <产物>.lora.pt 续，续不上就重跑。"""
        total = total_steps_all or int(rt.get("steps") or 0)
        done = _done_steps()
        out_path = str(rt.get("out") or "")
        adapter = os.path.splitext(out_path)[0] + ".lora.pt"
        nxt = dict(rt)
        resumed = False
        if done > 0 and total and done < total:
            if method_s == "lora" and os.path.isfile(adapter):
                nxt["resume_adapter"] = adapter
                resumed = True
            elif method_s != "lora" and os.path.isfile(out_path):
                nxt["init_from"] = out_path
                resumed = True
        if resumed:
            nxt["steps"] = max(1, total - done)
            nxt["start_step"] = done                 # 步数接着往上数（Loss 曲线不跳回 1）
            nxt["append_csv"] = True
            msg = ("[宿主] 训练中断（退出码 %s）→ 自动续训 第 %d/%d 次：从第 %d 步继续，剩 %d 步。"
                   % (cur.exit_code, attempt, MAX_RESUME, done, nxt["steps"]))
        else:
            msg = ("[宿主] 训练中断（退出码 %s）→ 没有可用断点，整轮重跑（第 %d/%d 次）。"
                   % (cur.exit_code, attempt, MAX_RESUME))
        cur.feed(msg)
        try:
            with open(rt_path, "w", encoding="utf-8") as fh:
                json.dump(nxt, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            cur.feed("[宿主] 自动续训放弃：重写运行配置失败（%s）。" % exc)
            return
        job_started[0] = time.time()
        nxt_job = _spawn(name, argv, IDE_DIR, group=group, owner=owner)
        nxt_job.feed(msg)                    # 新作业的第一行：页面读的是新作业的日志
        attach(nxt_job, attempt)

    def attach(j, attempt: int = 0) -> None:
        """给作业挂上「退出 → 记账 → 视情况续训」的收尾逻辑。"""
        def _finished() -> None:
            resumable = (not j.stop_requested) and (j.exit_code != 0)
            if resumable and attempt >= MAX_RESUME:
                j.feed("[宿主] 已连续自动重试 %d 次仍失败，停止重试（断点权重保留在磁盘上）。" % MAX_RESUME)
                resumable = False
            _on_exit(j, final=not resumable)
            if resumable:
                _restart(j, attempt + 1)
        j.on_exit = _finished

    def _on_exit(target, final: bool) -> None:
        """收尾记账：只有「真正结束」（成功 / 用户停止 / 重试用尽）才结算覆盖与溯源；
        中途崩溃要留着断点文件给续训用，不能清。"""
        j = target
        cands = list(hist_cands)
        if ovw_on and ovw_tmp:
            done = (j.exit_code == 0) or bool(j.stop_requested)
            if final and done:
                try:
                    if _ovw_swap(hist_root, ovw_old, ovw_tmp, j):
                        cands = [ovw_old]
                        j.feed("[宿主] 覆盖初始权重：新模型 → %s；旧模型已更名「初始_」前缀并移入回收站"
                               % os.path.basename(ovw_old))
                except Exception as exc:             # noqa: BLE001
                    j.feed("[宿主] 覆盖初始权重失败：%s: %s" % (type(exc).__name__, exc))
            elif final:
                try:
                    if os.path.isfile(ovw_tmp):
                        os.remove(ovw_tmp)
                    j.feed("[宿主] 训练未成功，未覆盖初始权重（临时产物已清理）。")
                except OSError:
                    pass
        if not final:
            return
        for cand in cands:
            try:
                if cand and os.path.isfile(cand) and os.path.getmtime(cand) >= hist_started - 1:
                    record_weight_hist(hist_root, cand, hist_label, str(rt.get("init_from") or ""))
            except OSError:
                continue

    attach(job_obj, 0)
    job_obj.feed(f"[宿主] 启动 {name} · pid {job_obj.pid} · {rt.get('steps')} 步 · 语料 {os.path.basename(rt.get('corpus') or '')}")
    job_obj.feed(merge_summary)                     # 合并摘要进训练日志（真实统计）
    return {"ok": True, "job": name, "pid": job_obj.pid, "runtime_cfg": rt_path,
            "config": rt, "cmd": argv,
            "auto_merge": {"kind": kind, "replaced": bool(mres.get("replaced")),
                           "written": int(mres.get("written") or 0),
                           "skipped": int(mres.get("skipped") or 0),
                           "out": mres.get("out"), "out_bytes": int(mres.get("out_bytes") or 0),
                           "summary": merge_summary}}


def stop_training() -> dict:
    """优雅停止：写 stop.flag（core/train.py 会保存权重后退出）。"""
    cfg = load_cfg()
    proj = project(cfg)
    flag = os.path.join(proj.logs_dir, "stop.flag")
    os.makedirs(proj.logs_dir, exist_ok=True)
    with open(flag, "w", encoding="utf-8") as f:
        f.write("1")
    name = running_in_group("training")
    if name:
        j = job(name)
        if j:
            j.stop_requested = True
    return {"ok": True, "stop_flag": flag, "job": name,
            "note": "已写入 stop.flag，训练会保存权重后优雅退出。"}


def run_config_overlay(stage: str, method: str = "full") -> dict:
    """正在跑的训练子进程真吃的 runtime json（argv --config 指向的那份，含流水线 persist=False 的参数）。

    用户 2026-10-20：「使用流水线进入某个训练页，训练页的左侧参数应该和流水线设置的一致」。
    只在训练真在跑时给值——页面表单不会被旧运行快照回灌，训练完仍显示各页自己的配置。
    """
    m = "lora" if str(method or "").strip().lower() == "lora" else "full"
    j = job(f"train:{stage}:{m}")
    if j is None or not getattr(j, "running", False):
        return {}
    if getattr(j, "owner", "") == "host":
        return {}
    argv = list(getattr(j, "argv", []) or [])
    rt_path = ""
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 < len(argv):
            rt_path = str(argv[i + 1])
    if not rt_path or not os.path.isfile(rt_path):
        return {}
    try:
        with open(rt_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _train_log_path(proj, stage: str, method: str) -> str:
    """训练输出落盘位置：<工程>/logs/train_<stage>_<method>.log（每次开始训练覆盖）。"""
    return os.path.join(proj.logs_dir, "train_%s_%s.log" % (stage, method))


def _log_tail(path: str, limit: int = 400) -> tuple:
    """读训练输出日志尾部的若干行（桥重启后「训练输出」仍能看到上次的内容）。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            read = min(size, 256 * 1024)
            fh.seek(size - read)
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return [], 0
    lines = data.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines[-limit:], len(lines)


def training_status(stage: str, method: str = "full", since: int = 0) -> dict:
    """训练状态：进程状态 + 增量日志 + 本阶段真实产出/日志文件状态。"""
    cfg = load_cfg()
    proj = project(cfg)
    rt = runtime_cfg(stage, method, cfg)
    name = f"train:{stage}:{rt.get('method', method)}"
    j = job(name)
    if j is not None and getattr(j, "owner", "") == "host":
        j = None                                  # 托管训练：训练页当它不存在（用户 2026-10-18）
    other = running_in_group("training", include_host=False)

    def probe(path: str) -> dict:
        path = path or ""
        try:
            ok = bool(path) and os.path.isfile(path)
            return {"path": path, "exists": ok,
                    "size_bytes": os.path.getsize(path) if ok else 0,
                    "mtime": os.path.getmtime(path) if ok else None}
        except OSError:
            return {"path": path, "exists": False, "size_bytes": 0, "mtime": None}

    out = {
        "ok": True,
        "stage": rt.get("stage"),
        "method": rt.get("method"),
        "project": proj.root,
        "running": bool(other),
        "running_job": other,
        "stop_flag": os.path.isfile(os.path.join(proj.logs_dir, "stop.flag")),
        "config": rt,
        "outputs": {
            "corpus": probe(rt.get("corpus")),
            "out": probe(rt.get("out")),
            "log_csv": probe(rt.get("log_csv")),
            "init_from": probe(rt.get("init_from")) if rt.get("init_from") else None,
        },
    }
    if j is None:
        # 2026-10-23（用户）：作业不在内存里（桥重启过 / 换了工程 / 上次跑完很久）时，
        # 从落盘的训练输出里回填尾部，免得「Loss 日志表有内容、训练输出却空了」。
        # 只看 since==0（首屏/重挂载那一次），之后的增量轮询不会重复追加。
        _lines, _total = ([], 0)
        if int(since or 0) == 0 and job(name) is None:
            _lines, _total = _log_tail(_train_log_path(proj, stage, rt.get("method", method)))
        out.update({"lines": _lines, "total_lines": _total, "next": _total,
                    "pid": None, "exit_code": None, "job_exists": False})
    else:
        out.update({k: v for k, v in j.status(since).items() if k != "name"})
        out["job_exists"] = True
    return out


# ---------------------------------------------------------------- 线程作业（采集 / 推理）

class _Task:
    """一个进程内线程作业：事件环形缓冲，行号即游标。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.lock = threading.Lock()
        self.events: collections.deque = collections.deque(maxlen=MAX_LINES)
        self.start_line = 0
        self.running = True
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.error = ""
        self.stats: dict = {}
        self.result: Any = None
        self._stop: Optional[Callable[[], None]] = None

    def _total(self) -> int:
        return self.start_line + len(self.events)

    def emit(self, kind: str, payload: Optional[dict] = None) -> None:
        with self.lock:
            self.events.append({"kind": kind, "payload": payload or {}, "t": round(time.time(), 3)})
            if len(self.events) == self.events.maxlen:
                self.start_line += 1

    def set_stats(self, **kw) -> None:
        with self.lock:
            self.stats.update(kw)

    def take(self, since: int = 0) -> tuple:
        with self.lock:
            total = self._total()
            since = max(0, min(int(since or 0), total))
            skip = max(0, since - self.start_line)
            return list(self.events)[skip:], total

    def status(self, since: int = 0, extra: Optional[dict] = None) -> dict:
        events, total = self.take(since)
        out = {
            "name": self.name,
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
            "events": events,
            "total_events": total,
            "next": total,
            "stats": dict(self.stats),
            "error": self.error,
            "result": self.result,
        }
        if extra:
            out.update(extra)
        return out


_TASKS: dict = {}
_TASKS_LOCK = threading.Lock()


def start_task(name: str, fn: Callable, stop: Optional[Callable] = None,
               group: str = "") -> _Task:
    """跑一个后台线程：fn(emit, task, group) 里做真活，emit(kind, payload) 推事件。"""
    busy = running_in_group("task:" + group) if group else None
    if busy:
        raise RuntimeError(f"已有任务在运行（{busy}），请先停止。")
    task = _Task(name)
    task._stop = stop
    task.group = group                            # type: ignore[attr-defined]
    with _TASKS_LOCK:
        _TASKS[name] = task
    if group:
        with _JOBS_LOCK:                          # 复用一个组查询表
            pass

    def runner() -> None:
        try:
            fn(task.emit, task, group)
        except Exception as exc:                  # noqa: BLE001
            task.error = f"{type(exc).__name__}: {exc}"
            _log_exc("task." + name, exc, group=group)   # 采集/蒸馏任务崩了留堆栈（2026-10-24）
            task.emit("error", {"text": task.error})
        finally:
            task.running = False
            task.finished_at = time.time()
            task.emit("done", {})

    threading.Thread(target=runner, daemon=True, name=f"task-{name}").start()
    return task


def task(name: str) -> Optional[_Task]:
    with _TASKS_LOCK:
        return _TASKS.get(name)


def task_status(name: str, since: int = 0) -> dict:
    t = task(name)
    if t is None:
        return {"ok": True, "exists": False, "running": False, "events": [], "next": 0}
    out = {"ok": True, "exists": True}
    out.update(t.status(since))
    return out


def stop_task(name: str) -> dict:
    t = task(name)
    if t is None or not t.running:
        return {"ok": True, "stopped": False, "note": "没有正在运行的任务"}
    if t._stop is not None:
        try:
            t._stop()
        except Exception as exc:                  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "stopped": True, "note": "已请求停止"}
    return {"ok": False, "error": "该任务不支持停止"}


def running_task(group: str) -> Optional[str]:
    with _TASKS_LOCK:
        for t in _TASKS.values():
            if getattr(t, "group", "") == group and t.running:
                return t.name
    return None


def info() -> dict:
    """宿主自检：路径解析 + 引擎可用性（给页面/我排查用）。"""
    out = {
        "web_dir": WEB_DIR, "ide_dir": IDE_DIR, "config": CONFIG_PATH,
        "config_exists": os.path.isfile(CONFIG_PATH),
        "train_script": TRAIN_SCRIPT,
        "train_script_exists": os.path.isfile(TRAIN_SCRIPT),
        "python": RUN_PY,
        "project": project_root(),
        "jobs": sorted(_JOBS), "tasks": sorted(_TASKS),
    }
    try:
        out["core_ok"] = bool(project().root)
    except Exception as exc:                      # noqa: BLE001
        out["core_ok"] = False
        out["core_error"] = str(exc)
    return out
