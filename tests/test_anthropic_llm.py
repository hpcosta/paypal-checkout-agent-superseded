import json
import unittest

from paypal_checkout_agent.agent import SYSTEM_PROMPT, run_turn
from paypal_checkout_agent.llm import DEFAULT_MODEL, AnthropicLLM, LLMError
from paypal_checkout_agent.tools import TOOLS
from tests.helpers import make

KEY = "fake-anthropic-key-0000"  # secret-scan: allow


class FakePost:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def __call__(self, url, headers, body, timeout):
        self.sent.append((url, dict(headers), json.loads(body)))
        return self.replies.pop(0)


def reply(content, status=200):
    return status, json.dumps({"content": content, "stop_reason": "tool_use"}).encode()


class AnthropicBackendTests(unittest.TestCase):
    def test_default_model_and_env_override(self):
        self.assertEqual(AnthropicLLM(api_key=KEY).model, DEFAULT_MODEL)
        import os
        os.environ["CHECKOUT_AGENT_MODEL"] = "some-other-model"
        try:
            self.assertEqual(AnthropicLLM(api_key=KEY).model, "some-other-model")
        finally:
            del os.environ["CHECKOUT_AGENT_MODEL"]

    def test_request_shape_and_tool_use_parsing(self):
        post = FakePost([reply([{"type": "text", "text": "Looking."},
                                {"type": "tool_use", "id": "toolu_1", "name": "search_catalog", "input": {"query": "mug"}}])])
        llm = AnthropicLLM(api_key=KEY, post=post)
        out = llm.complete("sys", [{"role": "user", "content": "a mug"}], TOOLS)
        url, headers, body = post.sent[0]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], KEY)
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(body["model"], DEFAULT_MODEL)
        self.assertEqual(body["system"], "sys")
        self.assertEqual({t["name"] for t in body["tools"]}, {t["name"] for t in TOOLS})
        for t in body["tools"]:
            self.assertEqual(set(t), {"name", "description", "input_schema"})
        self.assertEqual(out.text, "Looking.")
        self.assertEqual((out.tool_calls[0].id, out.tool_calls[0].name, out.tool_calls[0].arguments),
                         ("toolu_1", "search_catalog", {"query": "mug"}))

    def test_agent_loop_with_model_driven_tool_calls(self):
        post = FakePost([
            reply([{"type": "tool_use", "id": "t1", "name": "build_cart",
                    "input": {"items": [{"item_id": "MUG-001", "quantity": 1}], "currency": "USD"}}]),
            reply([{"type": "text", "text": "Your cart is 12.50 USD."}]),
        ])
        _l, _pay, tools = make()
        text = run_turn(AnthropicLLM(api_key=KEY, post=post), tools, [], "one mug")
        self.assertEqual(text, "Your cart is 12.50 USD.")
        second = post.sent[1][2]["messages"]
        block = second[-1]["content"][0]
        self.assertEqual((block["type"], block["tool_use_id"], block["is_error"]), ("tool_result", "t1", False))
        self.assertEqual(json.loads(block["content"])["cart"]["total"], "12.50")
        self.assertEqual(post.sent[0][2]["system"], SYSTEM_PROMPT)

    def test_errors_do_not_leak_the_key(self):
        post = FakePost([(401, json.dumps({"error": {"type": "authentication_error", "message": "invalid x-api-key"}}).encode())])
        llm = AnthropicLLM(api_key=KEY, post=post)
        with self.assertRaises(LLMError) as cm:
            llm.complete("s", [{"role": "user", "content": "x"}], TOOLS)
        self.assertNotIn(KEY, str(cm.exception))
        self.assertNotIn(KEY, repr(llm))
        self.assertIn("401", str(cm.exception))

    def test_missing_key(self):
        import os
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(LLMError):
                AnthropicLLM()
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
