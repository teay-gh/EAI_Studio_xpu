"""OpenAI 兼容 Chat Completions 客户端（仅用标准库）。"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any


class ChatError(RuntimeError):
    pass


class ChatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout: float = 90.0,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout)

    @classmethod
    def from_profile(cls, profile: dict[str, Any], timeout: float = 90.0) -> "ChatClient":
        return cls(
            base_url=profile.get("base_url", ""),
            api_key=profile.get("api_key", ""),
            model=profile.get("model", ""),
            temperature=float(profile.get("temperature", 0.7)),
            max_tokens=int(profile.get("max_tokens", 2048)),
            timeout=timeout,
        )

    def _endpoint(self) -> str:
        if not self.base_url:
            raise ChatError("base_url 未配置")
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: Any | None = None,
    ) -> str:
        if not self.model:
            raise ChatError("model 未配置")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        if stop is not None:
            payload["stop"] = stop

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
        }

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                body = self._post_once(data, headers)
                return self._extract_text(body)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:800]
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    last_err = ChatError(f"HTTP {exc.code}: {detail}")
                    time.sleep(1.2 * (attempt + 1))
                    continue
                raise ChatError(f"HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                last_err = ChatError(f"网络错误: {exc.reason}")
                if attempt < 2:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                raise last_err from exc
            except TimeoutError as exc:
                raise ChatError(f"请求超时（>{self.timeout:.0f}s），请调大超时或降低 max_tokens") from exc
            except ChatError as exc:
                raise exc
        raise ChatError(str(last_err or "未知错误"))

    def _post_once(self, data: bytes, headers: dict[str, str]) -> str:
        """一次 HTTP POST，附带总时长硬超时。"""
        req = urllib.request.Request(self._endpoint(), data=data, method="POST", headers=headers)
        result: dict[str, Any] = {}
        error: dict[str, Any] = {}

        def worker() -> None:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    result["body"] = resp.read().decode("utf-8")
            except Exception as exc:  # noqa: BLE001
                error["exc"] = exc

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        th.join(self.timeout)
        if th.is_alive():
            raise TimeoutError(f"request exceeded {self.timeout}s")
        if "exc" in error:
            raise error["exc"]
        return str(result.get("body", ""))

    @staticmethod
    def _extract_text(body: str) -> str:
        try:
            obj = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ChatError(f"响应不是合法 JSON: {body[:400]}") from exc

        choices = obj.get("choices") or []
        if not choices:
            raise ChatError(f"响应缺少 choices: {body[:400]}")

        choice0 = choices[0]
        message = choice0.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            text = "".join(parts).strip()
        else:
            text = (choice0.get("text") or "").strip()

        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        if isinstance(reasoning, str):
            reasoning = reasoning.strip()
        else:
            reasoning = ""
        finish = choice0.get("finish_reason") or ""

        if text:
            return text

        if finish == "length":
            if reasoning:
                raise ChatError(
                    "回答被 max_tokens 截断：模型还在推理（reasoning_content），尚未输出正文 content。"
                    "请调大该模型的 max_tokens（推理模型通常需要 2048 以上）。"
                )
            raise ChatError(
                "回答被 max_tokens 截断：finish_reason=length 且 content 为空。请调大 max_tokens。"
            )
        if reasoning:
            raise ChatError(
                "模型只返回了 reasoning_content，没有正文 content。"
                "请调大 max_tokens，或更换非推理模型 / 在提示词中要求直接输出答案。"
            )
        raise ChatError(f"模型返回空内容: {body[:400]}")

    def ping(self) -> str:
        """连通性测试：发一条短消息。"""
        return self.chat(
            [{"role": "user", "content": "请只回复两个字：在线"}],
            max_tokens=max(256, min(self.max_tokens, 512)),
            temperature=0,
        )
