from .anthropic import AnthropicLLM, DEFAULT_MODEL
from .base import LLM, LLMError, LLMResponse, ToolCall
from .scripted import ScriptedLLM

__all__ = ["LLM", "LLMError", "LLMResponse", "ToolCall", "ScriptedLLM", "AnthropicLLM", "DEFAULT_MODEL"]


def make_llm(kind: str) -> LLM:
    if kind == "scripted":
        return ScriptedLLM()
    if kind == "anthropic":
        return AnthropicLLM()
    raise ValueError(f"unknown llm backend {kind!r}")
