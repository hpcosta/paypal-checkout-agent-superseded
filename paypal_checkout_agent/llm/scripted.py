"""Scripted test double for the language model.

This is NOT a language model. It is a small deterministic rule set that emulates the
tool-calling decisions a real model makes for this shop's flows, so the whole agent loop,
the guardrails and the payment adapters can be tested offline. Every reply it writes says so.
"""
from __future__ import annotations

import json
import re

from ..catalog import stem
from .base import LLM, LLMResponse, ToolCall

NOTE = "[scripted test double: rule-based stand-in for a language model, not a real one]"
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_QUESTION = re.compile(r"^(how|what|when|why|can|could|do|does|is|are|will|explain|tell)\b|\?")
_REFUND_NOW = re.compile(r"\b(please refund|refund (it|that|this|me|my|\$|€|\d)|give me my money back|i want (a|my) refund)")
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _text_of(message: dict) -> str:
    c = message["content"]
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c if b.get("type") == "text")


def _tool_results(messages: list) -> list:
    """All (tool_name, result_dict) pairs so far, oldest first."""
    names, out = {}, []
    for m in messages:
        if isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    names[b["id"]] = b["name"]
                elif b.get("type") == "tool_result":
                    try:
                        res = json.loads(b["content"])
                    except (ValueError, TypeError):
                        res = {}
                    out.append((names.get(b["tool_use_id"], "?"), res))
    return out


def _latest(results: list, tool: str, key_path: tuple):
    for name, res in reversed(results):
        if name == tool and res.get("ok"):
            cur = res
            for k in key_path:
                cur = cur.get(k) if isinstance(cur, dict) else None
            if cur:
                return cur
    return None


def _quantities(text: str, items: list) -> list:
    tokens = re.findall(r"[a-z0-9€$.\-]+", text.lower())
    lines = []
    for item in items:
        keys = {stem(t.lower()) for t in item["tags"]} | {stem(w.lower()) for w in item["name"].split() if w.lower() != "sample"}
        qty = 1
        for i, tok in enumerate(tokens):
            if stem(tok) in keys:
                prev = tokens[i - 1].rstrip("x") if i else ""
                if prev.isdigit():
                    qty = int(prev)
                elif prev in _WORDS:
                    qty = _WORDS[prev]
                break
        lines.append({"item_id": item["item_id"], "quantity": qty})
    return lines


class ScriptedLLM(LLM):
    name = "scripted"
    is_test_double = True

    def complete(self, system: str, messages: list, tools: list) -> LLMResponse:
        last = messages[-1]
        results = _tool_results(messages)
        user_text = next((_text_of(m) for m in reversed(messages)
                          if m["role"] == "user" and _text_of(m)), "")
        if isinstance(last["content"], list) and any(b.get("type") == "tool_result" for b in last["content"]):
            return self._after_tool(user_text, results)
        return self._new_request(user_text, results)

    # -- a fresh shopper message ------------------------------------------------------

    def _new_request(self, text: str, results: list) -> LLMResponse:
        low = text.lower().strip()
        capture_id = _latest(results, "capture_order", ("order", "capture_id"))
        order_id = _latest(results, "create_order", ("order", "order_id"))
        if re.search(r"\brefund|money back|reimburse", low):
            if _REFUND_NOW.search(low) and not _QUESTION.search(low):
                if not capture_id:
                    return self._say("There is no captured payment in this session yet, so there is nothing to refund.")
                args = {"capture_id": capture_id, "reason": "Refund requested by shopper"}
                m = re.search(r"[$€]\s*(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s*(?:usd|eur|dollars|euros)", low)
                if m:
                    args["amount"] = f"{float(m.group(1) or m.group(2)):.2f}"
                return self._call("refund_capture", args)
            return self._call("explain_refund", {"capture_id": capture_id} if capture_id else {})
        if order_id and re.search(r"\b(approved|i paid|capture|go ahead|complete the payment|done|finish)\b", low):
            return self._call("get_order", {"order_id": order_id})
        return self._call("search_catalog", {"query": text[:120] or "item"})

    # -- after a tool result -------------------------------------------------------------

    def _after_tool(self, user_text: str, results: list) -> LLMResponse:
        name, res = results[-1]
        if not res.get("ok"):
            err = res.get("error", {})
            return self._say(f"I could not do that ({name} was refused: {err.get('message', 'unknown error')}).")
        if name == "search_catalog":
            items = res["items"]
            if not items:
                return self._say("I could not find anything like that in the catalogue. I can help you buy catalogue "
                                 "items, approve and capture the payment, and explain or issue refunds.")
            currency = "EUR" if re.search(r"€|\beur\b|\beuros?\b", user_text.lower()) else "USD"
            return self._call("build_cart", {"items": _quantities(user_text, items), "currency": currency})
        if name == "build_cart":
            return self._call("create_order", {"cart_id": res["cart"]["cart_id"]})
        if name == "create_order":
            o = res["order"]
            return self._say(f"Your order {o['order_id']} for {o['total']} {o['currency']} is ready. "
                             f"Please open this approval link and approve the payment, then tell me: {o.get('approval_url')}")
        if name == "get_order":
            o = res["order"]
            if o["status"] == "APPROVED":
                return self._call("capture_order", {"order_id": o["order_id"]})
            if o["status"] == "COMPLETED":
                return self._say(f"Order {o['order_id']} is already paid (capture {o.get('capture_id')}).")
            return self._say(f"Order {o['order_id']} is {o['status']}: it has not been approved yet. "
                             f"Please open {o.get('approval_url')} and approve it.")
        if name == "capture_order":
            o = res["order"]
            return self._say(f"Payment captured: {o['captured_amount']} {o['currency']} for order {o['order_id']}. "
                             f"Capture id {o['capture_id']} (needed for any refund).")
        if name == "explain_refund":
            extra = ""
            if "capture" in res:
                c = res["capture"]
                extra = f" Refundable now: {c['refundable_now']} {c['currency']}."
            return self._say(res["policy"] + extra)
        if name == "refund_capture":
            r = res["refund"]
            return self._say(f"Refund {r['refund_id']} {r['status']}: {r['amount']} {r['currency']}. "
                             f"Still refundable: {res['refundable_remaining']}.")
        return self._say("Done.")

    # -- helpers ----------------------------------------------------------------------------

    @staticmethod
    def _say(text: str) -> LLMResponse:
        return LLMResponse(f"{NOTE} {text}")

    def _call(self, name: str, args: dict) -> LLMResponse:
        self._n = getattr(self, "_n", 0) + 1
        return LLMResponse("", [ToolCall(f"toolu_scripted_{self._n:04d}", name, args)])
