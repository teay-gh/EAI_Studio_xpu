"""engine/core/atomic.py —— 原子写 JSON / 文本（2026-10-24）。

为什么单独抽出来：
  `config.json` 早先改成「临时文件 + flush/fsync + os.replace + 进程内锁」，
  但 `state/ui_state.json`、`data/_weight_hist.json`、`data/_collect_use.json`、
  `logs/runtime_*.json` 还是 `open(path, 'w') + json.dump` 直写 —— 写一半崩溃就留半截 JSON，
  读取方大多用宽泛异常兜成空值，于是静默丢状态。统一走这里。

口径（与 config.save_config 完全一致，别各写一套）：
  · 同进程多线程：_LOCK（RLock）串行化 —— 桥是 ThreadingHTTPServer；
  · 跨进程：各写各的临时文件再原子替换，读者要么看到旧文件要么看到新文件；
  · 临时文件与目标同目录（同卷）—— Windows/POSIX 的 os.replace 都是原子替换；
  · Windows 上目标被占用（读配置、杀软扫描）会抛 PermissionError(WinError 5/32)，
    指数退避重试 12 次（10ms → 400ms 封顶）后仍失败才向上抛；
  · 读侧 open_retry 用同一套退避，读者不会因为「替换瞬间」读不到。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

_LOCK = threading.RLock()      # config.save_config / load_config 共用这一把（见 config.py 的别名）
_RETRY = 12


def _backoff(i: int) -> None:
    time.sleep(min(0.01 * (2 ** i), 0.4))


def retry_replace(tmp: str, target: str) -> None:
    last: Exception | None = None
    for i in range(_RETRY):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as e:
            last = e
            _backoff(i)
    if last is not None:
        raise last


def open_retry(path: str):
    """容忍「替换瞬间」的瞬时占用（Windows 上 open 不带 FILE_SHARE_DELETE）。"""
    last: Exception | None = None
    for i in range(_RETRY):
        try:
            return open(path, "r", encoding="utf-8")
        except PermissionError as e:
            last = e
            _backoff(i)
    if last is not None:
        raise last
    raise OSError("open failed: %s" % path)


def write_text(path: str, text: str, *, encoding: str = "utf-8") -> None:
    """原子写文本：同目录临时文件 → flush/fsync → 原子替换。"""
    ap = os.path.abspath(path)
    directory = os.path.dirname(ap)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _LOCK:
        tmp = "%s.tmp-%d-%d" % (ap, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w", encoding=encoding) as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())        # 落盘后再替换：断电也不会留下空文件
            retry_replace(tmp, ap)
        except BaseException:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise


def write_json(path: str, data: Any, *, indent: int = 2, ensure_ascii: bool = False) -> None:
    write_text(path, json.dumps(data, ensure_ascii=ensure_ascii, indent=indent))


def read_json(path: str, default: Any = None) -> Any:
    """读 JSON：与写共用同一把锁。

    同一进程里「读的同时另一个人在 os.replace」在 Windows 上会互相顶（open 不带
    FILE_SHARE_DELETE，替换被拒 WinError 5/32），所以读也要拿锁；跨进程读者拿不到锁，
    靠 open_retry 的退避兜。读不到 / 坏 JSON 一律返回 default（调用方本来就当空处理）。
    """
    try:
        with _LOCK:
            with open_retry(path) as f:
                return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        # 文件不存在是正常态（首次运行），不记；其它情况（坏 JSON / 读不了）要留证据，
        # 否则这些状态文件「突然变空」永远查不到原因（2026-10-24，排查报告第 2 条）。
        if not isinstance(e, FileNotFoundError):
            try:
                from . import errlog as _errlog     # noqa: PLC0415
                _errlog.log_exc("json.read", e, path=path)
            except Exception:                        # noqa: BLE001
                pass
        return default
