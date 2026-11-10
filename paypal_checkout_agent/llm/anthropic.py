"""Anthropic Messages API backend with tool use (standard library only).

The key comes from ANTHROPIC_API_KEY. The model id comes from CHECKOUT_AGENT_MODEL
(default below). The key is sent only in the x-api-key header and is never logged.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .base import LLM, LLMError, LLMResponse, ToolCall

DEFAULT_MODEL = "claude-sonnet-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


def _urllib_post(url: str, headers: dict, body: bytes, timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"could not reach the Anthropic API ({type(exc).__name__})") from None


class AnthropicLLM(LLM):
    name = "anthropic"

    def __init__(self, api_key=None, model=None, post=None, max_tokens: int = 1024, timeout: float = 60.0):
        self._key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        if not self._key:
            raise LLMError("ANTHROPIC_API_KEY is not set in the environment")
        self.model = model or os.environ.get("CHECKOUT_AGENT_MODEL") or DEFAULT_MODEL
        self._post = post or _urllib_post
        self._max_tokens, self._timeout = max_tokens, timeout

    def __repr__(self) -> str:
        return f"AnthropicLLM(model={self.model!r}, api_key=<hidden>)"

    def complete(self, system: str, messages: list, tools: list) -> LLMResponse:
        payload = {"model": self.model, "max_tokens": self._max_tokens, "system": system,
                   "messages": messages, "tools": tools}
        headers = {"x-api-key": self._key, "anthropic-version": API_VERSION, "content-type": "application/json"}
        status, raw = self._post(API_URL, headers, json.dumps(payload).encode(), self._timeout)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise LLMError(f"Anthropic API returned a non-JSON response (HTTP {status})") from None
        if status != 200:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise LLMError(f"Anthropic API error HTTP {status}: {err.get('type', 'error')}: {str(err.get('message', ''))[:200]}")
        text_parts, calls = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(block["id"], block["name"], block.get("input") or {}))
        return LLMResponse("".join(text_parts), calls)
