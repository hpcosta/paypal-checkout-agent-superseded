"""LLM backend contract. Messages use the Anthropic content-block layout internally."""
from __future__ import annotations

from dataclasses import dataclass, field


class LLMError(Exception):
    """The language-model backend failed (never carries credentials)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list = field(default_factory=list)

    def to_assistant_message(self) -> dict:
        blocks = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        for c in self.tool_calls:
            blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
        return {"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]}


class LLM:
    name = "abstract"
    is_test_double = False

    def complete(self, system: str, messages: list, tools: list) -> LLMResponse:
        raise NotImplementedError
