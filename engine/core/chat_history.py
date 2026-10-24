"""AI 过往对话存储。

规则：
- 有工程：会话写入 <工程>/chats/*.json，长期保留
- 无工程：仅内存缓存，软件关闭后清空
- 先对话后开工程：把内存缓存迁入工程 chats/
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any


_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")   # 会话 id 只允许安全字符（不含点，防 ../ 路径）


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class ChatHistory:
    def __init__(self) -> None:
        self._memory: list[dict[str, Any]] = []  # 无工程时的缓存（含当前会话）
        self._project_root: str | None = None

    # ---------- 工程绑定 ----------
    def bind_project(self, root: str | None) -> None:
        """绑定/解绑工程。绑定时把内存会话迁入工程。"""
        root = os.path.abspath(root) if root else None
        if root == self._project_root:
            return
        prev = self._project_root
        self._project_root = root
        # 2026-10-18 修：切工程时丢掉内存里属于旧工程的会话。
        # 原来只换 root，_memory 里的旧会话仍会被 list_all() 列出来（标成新工程名），
        # 甚至被 save_messages 写进新工程的 chats/ —— 对话串工程就是这么来的。
        if prev is not None:
            self._memory = [c for c in self._memory if not c.get("_project")]
        if root:
            os.makedirs(os.path.join(root, "chats"), exist_ok=True)
            self._migrate_memory_to_project()

    @property
    def chats_dir(self) -> str | None:
        if not self._project_root:
            return None
        return os.path.join(self._project_root, "chats")

    def _migrate_memory_to_project(self) -> None:
        d = self.chats_dir
        if not d:
            return
        for conv in self._memory:
            if conv.get("_migrated"):
                continue
            self._write_project_conv(conv)
            conv["_migrated"] = True

    def _write_project_conv(self, conv: dict[str, Any]) -> None:
        d = self.chats_dir
        if not d:
            return
        os.makedirs(d, exist_ok=True)
        cid = conv.get("id") or uuid.uuid4().hex[:12]
        conv["id"] = cid
        path = os.path.join(d, f"{cid}.json")
        conv["_project"] = self._project_root      # 标记归属（下划线字段不落盘）
        payload = {k: v for k, v in conv.items() if not k.startswith("_")}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---------- 会话 ----------
    def new_session(self) -> dict[str, Any]:
        """开一段新会话；返回会话对象。"""
        conv = {
            "id": uuid.uuid4().hex[:12],
            "title": "新对话",
            "created": _now(),
            "updated": _now(),
            "messages": [],
        }
        self._memory.append(conv)
        return conv

    def save_messages(self, conv_id: str, messages: list[dict[str, str]]) -> None:
        conv = self.find(conv_id)
        if conv is None:
            return
        conv["messages"] = list(messages)
        conv["updated"] = _now()
        if messages:
            first_user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
            if first_user:
                conv["title"] = first_user.strip().replace("\n", " ")[:24] or conv["title"]
        if self._project_root:
            conv["_migrated"] = True
            self._write_project_conv(conv)

    def find(self, conv_id: str) -> dict[str, Any] | None:
        for c in self._memory:
            if c.get("id") == conv_id:
                return c
        # 工程里可能尚未载入
        d = self.chats_dir
        if d:
            path = os.path.join(d, f"{conv_id}.json")
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        data["_migrated"] = True
                        data["_project"] = self._project_root
                        self._memory.append(data)
                        return data
                except (OSError, json.JSONDecodeError):
                    pass
        return None

    def list_all(self) -> list[dict[str, Any]]:
        """按更新时间倒序列出全部会话（内存 + 工程）。"""
        seen: dict[str, dict[str, Any]] = {}
        for c in self._memory:
            # 只认属于当前工程的会话（2026-10-18：切工程后旧会话不再串进来）
            if c.get("_project") and c.get("_project") != self._project_root:
                continue
            seen[c.get("id", "")] = c
        d = self.chats_dir
        if d and os.path.isdir(d):
            for name in os.listdir(d):
                if not name.endswith(".json"):
                    continue
                cid = name[:-5]
                if cid in seen:
                    continue
                try:
                    with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict) and data.get("id"):
                        data["_migrated"] = True
                        data["_project"] = self._project_root
                        seen[data["id"]] = data
                        self._memory.append(data)
                except (OSError, json.JSONDecodeError):
                    continue
        items = list(seen.values())
        items.sort(key=lambda x: x.get("updated") or x.get("created") or "", reverse=True)
        return items

    def delete(self, conv_id: str) -> bool:
        """删除一个工程会话（文件 + 内存缓存一起清）。

        用户在 AI 面板的历史列表里点 ✕ 走这里。只认 <工程>/chats/<id>.json，
        id 必须是安全字符，避免拼出 ../ 之类的路径；无工程的内存会话不删。
        """
        cid = str(conv_id or "").strip()
        if not cid or not _SAFE_ID.fullmatch(cid):
            return False
        d = self.chats_dir
        ok = False
        if d:
            path = os.path.join(d, cid + ".json")
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    ok = True
                except OSError:
                    return False
        before = len(self._memory)
        self._memory = [c for c in self._memory if str(c.get("id") or "") != cid]
        return ok or len(self._memory) != before

    def clear_memory_only(self) -> None:
        """退出时调用：清掉无工程会话；有工程的已落盘不受影响。"""
        if self._project_root:
            self._memory = [c for c in self._memory if c.get("_migrated")]
        else:
            self._memory.clear()
