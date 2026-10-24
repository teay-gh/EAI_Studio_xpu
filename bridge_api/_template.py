"""bridge_api/_template.py —— 复制成 <页面key>.py（如 article.py）后改内容。

契约（由 bridge.py 的 load_plugins() 自动发现，文件名不以 _ 开头的 .py 都会被加载）：
  - 模块必须有 `register(router)`。
  - `router.get(path, fn)`：fn(query: dict) -> 可 JSON 序列化的对象；query 是 ?a=1&b=2 的解析结果。
  - `router.post(path, fn)`：fn(body: dict) -> 同上；body 是请求体 JSON（非对象会被包成 {"value": ...}），
    另外 body["_query"] 是 URL 查询参数。
  - 抛异常 → 桥自动回 500 JSON（{"error": ..., "type": ...}），不会让服务崩。

铁律：
  * 只读本地真实数据；要跑重活（蒸馏 / 训练 / 对话）时按需 import（EAI_Studio/engine 已在 sys.path 里，
    可 `import core.storage`）。首次 import torch 之类可能十来秒，可接受；失败就返回 {"error": "..."}。
  * **绝不允许返回编造的假数据**。跑不了就把错误照实返回，渲染层照实显示。
"""
from __future__ import annotations

import os

# 2026-10-23：模板里也不写死盘符 —— 由本文件位置推导（bridge_api/ 的上一级 + engine）
ENGINE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine")


def register(router) -> None:
    def ping(query: dict) -> dict:
        return {"ok": True, "query": query}

    def echo(body: dict) -> dict:
        return {"ok": True, "received": {k: v for k, v in body.items() if k != "_query"}}

    router.get("/api/template/ping", ping)
    router.post("/api/template/echo", echo)
