# -*- coding: utf-8 -*-
"""总览「日志」管理（用户 2026-10-16）：删除单条、全部清除（二次确认在前端）。

删除落库 <工程>/data/_log_hidden.json：
  keys            被删行的 key（op|detail），上限 500 条 FIFO
  cleared_before  「全部清除」时间水位 —— 清空前生成的行一律不再显示；
                  清空之后的新添加/新蒸馏照常出现
"""

import json
import os
import time


def _hidden_path(project: str) -> str:
    return os.path.join(project, "data", "_log_hidden.json")


def _load(project: str) -> dict:
    try:
        with open(_hidden_path(project), "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return {
                "keys": [str(x) for x in (d.get("keys") or [])],
                "cleared_before": float(d.get("cleared_before") or 0),
            }
    except Exception:  # noqa: BLE001  没有/坏档 = 没删过
        pass
    return {"keys": [], "cleared_before": 0.0}


def _save(project: str, st: dict) -> None:
    d = os.path.join(project, "data")
    os.makedirs(d, exist_ok=True)
    with open(_hidden_path(project), "w", encoding="utf-8") as f:
        json.dump({"keys": list(st.get("keys") or []),
                   "cleared_before": float(st.get("cleared_before") or 0)},
                  f, ensure_ascii=False, indent=1)


def _project(body: dict) -> str:
    import dataset as ds_api                  # 同目录桥模块（bridge 已把目录挂上）
    return ds_api._resolve_project(str((body or {}).get("project") or ""))


def log_delete(body: dict | None) -> dict:
    """POST /api/log/delete {op, detail} —— 删单条日志行。"""
    b = body or {}
    op = str(b.get("op") or "")
    detail = str(b.get("detail") or "")
    if not op and not detail:
        return {"ok": False, "error": "缺少日志标识"}
    try:
        project = _project(b)
        key = "%s|%s" % (op, detail)
        st = _load(project)
        keys = [k for k in st["keys"] if k != key]
        keys.append(key)
        if len(keys) > 500:
            keys = keys[-500:]
        st["keys"] = keys
        _save(project, st)
        return {"ok": True}
    except Exception as exc:                  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def log_clear(body: dict | None) -> dict:
    """POST /api/log/clear —— 全部清除（前端已二次确认）。"""
    try:
        project = _project(body or {})
        _save(project, {"keys": [], "cleared_before": time.time()})
        return {"ok": True}
    except Exception as exc:                  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def register(router) -> None:
    router.post("/api/log/delete", log_delete)
    router.post("/api/log/clear", log_clear)
