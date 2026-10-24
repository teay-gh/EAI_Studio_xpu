"""engine/core/errlog.py —— 宽泛异常落盘（2026-10-24）。

背景（排查报告第 2 条）：全仓 443 处 `except Exception`（bridge_api 345 + engine 98），
兜底本身没错，但**堆栈从不落盘** —— 真出问题时前端只看到一句人话，排查只能靠用户截图。
这里给「兜底也要留证据」一个统一出口：

    from core import errlog
    try:
        ...
    except Exception as exc:            # noqa: BLE001
        errlog.log_exc("train.start", exc, stage=stage, round=round_no)
        return {"ok": False, "error": str(exc)}

落点：`<EAI_Studio>/logs/bridge_error.log`
  · 追加写，不覆盖；超过 2 MB 轮到 `.1`（只留一份历史，避免无限长大）；
  · 一行一条 + 完整 traceback；同签名（tag + 异常类型 + 消息）60 秒内只记首条，
    之后只在再次出现时补一行「另有 N 次」，防止逐帧异常把日志刷爆；
  · 字段摘要只记调用方显式传的短字段（stage / round / path …），**不记请求体与 api_key**；
  · 写日志本身任何异常都吞掉 —— 日志坏了也不能拖垮业务。

设计口径：只记「被兜底吞掉的异常」。正常路径一个字节都不写，日志要能一眼看出出过事。
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Any

MAX_BYTES = 2 * 1024 * 1024          # 2 MB 轮转
THROTTLE_SEC = 60.0                  # 同签名节流窗口

_LOCK = threading.Lock()
_SEEN: dict[tuple[str, str, str], list[float]] = {}   # 签名 → [上次写盘时间, 被压掉的次数]

# <应用根>/logs/bridge_error.log（core/errlog.py 的上三级 = EAI_Studio）
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_DIR = os.path.join(_APP_ROOT, "logs")
LOG_PATH = os.path.join(LOG_DIR, "bridge_error.log")


def _rotate_if_needed() -> None:
    try:
        if os.path.getsize(LOG_PATH) <= MAX_BYTES:
            return
        bak = LOG_PATH + ".1"
        try:
            if os.path.isfile(bak):
                os.remove(bak)
        except OSError:
            pass
        os.replace(LOG_PATH, bak)
    except OSError:
        pass


def _short(v: Any, limit: int = 160) -> str:
    s = str(v)
    s = s.replace("\r", " ").replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def _fmt_fields(fields: dict[str, Any]) -> str:
    if not fields:
        return ""
    keys = sorted(fields)
    def one(k: str) -> str:
        v = fields[k]
        if isinstance(v, (int, float, bool)) or v is None:
            return "%s=%s" % (k, v)                       # 数字/布尔不加引号，日志更好扫
        return "%s=%s" % (k, repr(_short(v)))
    return " " + " ".join(one(k) for k in keys)


def log_exc(tag: str, exc: Any = None, tb: Any = None, **fields: Any) -> None:
    """记一条「被兜底吞掉」的异常（含 traceback）。永不抛异常。"""
    try:
        if tb is not None:
            text = "".join(traceback.format_exception(type(exc), exc, tb))
        else:
            text = traceback.format_exc()
            if text.strip() in ("NoneType: None", "None"):
                if exc is not None:
                    text = "".join(traceback.format_exception(type(exc), exc,
                                                              getattr(exc, "__traceback__", None)))
                else:
                    text = "(no traceback)"
        _write(tag, "%s: %s" % (type(exc).__name__ if exc is not None else "Exception",
                                _short(exc)), text, fields)
    except Exception:                            # noqa: BLE001  日志绝不能影响业务
        pass


def log_line(tag: str, msg: str, **fields: Any) -> None:
    """记一条不带异常的信息（例如「配置读坏、已回退出厂默认值」）。永不抛异常。"""
    try:
        _write(tag, _short(msg, 400), "", fields)
    except Exception:                            # noqa: BLE001
        pass


def _write(tag: str, head: str, tb_text: str, fields: dict[str, Any]) -> None:
    sig_name = head.split(":", 1)[0]
    sig = (tag, sig_name, head[:120])
    now = time.time()
    with _LOCK:
        hit = _SEEN.get(sig)
        if hit and now - hit[0] < THROTTLE_SEC:
            hit[1] += 1                     # 压掉，等下次超窗时补一行总数
            return
        suppressed = int(hit[1]) if hit else 0
        _SEEN[sig] = [now, 0]
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
        except OSError:
            return
        _rotate_if_needed()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        thread = threading.current_thread().name
        parts = ["[%s] [pid %d] [%s] [%s] %s%s" % (stamp, os.getpid(), thread, tag, head,
                                                   _fmt_fields(fields))]
        if suppressed:
            parts.append("  （同类异常另有 %d 次被节流，%ds 内不再逐条记）" % (suppressed, THROTTLE_SEC))
        if tb_text:
            parts.extend(["  " + ln for ln in tb_text.rstrip().splitlines()])
        try:
            with open(LOG_PATH, "a", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(parts) + "\n")
        except OSError:
            pass


def tail(n: int = 40) -> list[str]:
    """读日志尾部（给自检脚本用；出错返回空表）。"""
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []
