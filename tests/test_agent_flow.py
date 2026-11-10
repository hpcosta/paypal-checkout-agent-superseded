import json
import unittest

from paypal_checkout_agent.agent import run_turn
from paypal_checkout_agent.llm.base import LLM, LLMResponse, ToolCall
from paypal_checkout_agent.llm.scripted import NOTE
from tests.helpers import make


class ScriptedFlowTests(unittest.TestCase):
    def setUp(self):
        self.llm, self.pay, self.tools = make()
        self.messages = []
        self.trace = []

    def say(self, text):
        return run_turn(self.llm, self.tools, self.messages, text, lambda n, a, o: self.trace.append((n, o["ok"])))

    def test_full_journey_purchase_approve_capture_refund(self):
        reply = self.say("I'd like 2 mugs and a tee please")
        self.assertIn("44.00 USD", reply)
        self.assertIn("mock-paypal.invalid", reply)
        self.assertEqual([n for n, _ in self.trace], ["search_catalog", "build_cart", "create_order"])
        oid = list(self.tools.orders)[0]

        self.trace.clear()
        reply = self.say("I approved it, please capture")  # buyer has NOT approved yet
        self.assertIn("not been approved", reply)
        self.assertEqual(self.pay.get_order(oid)["status"], "PAYER_ACTION_REQUIRED")

        self.pay.simulate_buyer_approval(oid)
        self.trace.clear()
        reply = self.say("ok I approved it now, capture the payment")
        self.assertIn("Payment captured: 44.00 USD", reply)
        self.assertEqual([n for n, _ in self.trace], ["get_order", "capture_order"])
        self.assertEqual(self.pay.get_order(oid)["status"], "COMPLETED")

        self.trace.clear()
        reply = self.say("How do refunds work?")
        self.assertIn("refund policy", reply)
        self.assertIn("Refundable now: 44.00 USD", reply)
        self.assertEqual([n for n, _ in self.trace], ["explain_refund"])

        reply = self.say("refund $4.00 please")
        self.assertIn("4.00 USD", reply)
        self.assertIn("Still refundable: 40.00", reply)
        reply = self.say("please refund it")
        self.assertIn("40.00 USD", reply)
        reply = self.say("refund me again")
        self.assertIn("fully refunded", reply)

    def test_every_scripted_reply_discloses_the_test_double(self):
        for text in ("2 mugs", "how do refunds work?", "refund it", "hello there"):
            self.assertTrue(self.say(text).startswith(NOTE), text)

    def test_quantity_and_currency_parsing(self):
        self.say("three stickers and a notebook in EUR")
        cart = self.tools.cart.to_public()
        self.assertEqual(cart["currency"], "EUR")
        lines = {l["item_id"]: l["quantity"] for l in cart["lines"]}
        self.assertEqual(lines, {"STK-001": 3, "NTB-001": 1})
        self.assertEqual(cart["total"], "17.00")

    def test_guardrail_refusal_is_reported_not_worked_around(self):
        reply = self.say("I want 50 mugs")
        self.assertIn("refused", reply)
        self.assertIn("integer out of range", reply)
        self.assertEqual(self.pay.calls, [])
        reply = self.say("I want 5 hoodies")
        self.assertIn("exceeds the limit", reply)
        self.assertEqual(self.pay.calls, [])

    def test_refund_without_capture(self):
        self.assertIn("nothing to refund", self.say("please refund me"))

    def test_unknown_request(self):
        self.assertIn("could not find", self.say("a unicorn"))

    def test_declined_capture_message(self):
        llm, pay, tools = make(confirm=lambda _a: False)
        msgs = []
        run_turn(llm, tools, msgs, "1 mug")
        pay.simulate_buyer_approval(list(tools.orders)[0])
        reply = run_turn(llm, tools, msgs, "I approved it, capture")
        self.assertIn("declined", reply)


class HostileModelTests(unittest.TestCase):
    """A misbehaving model must not get past the executor."""

    class Hostile(LLM):
        name = "hostile"

        def __init__(self, calls):
            self.calls = list(calls)

        def complete(self, system, messages, tools):
            if self.calls:
                name, args = self.calls.pop(0)
                return LLMResponse("", [ToolCall(f"t{len(self.calls)}", name, args)])
            return LLMResponse("done")

    def test_hostile_calls_are_all_refused_and_reach_no_payment_api(self):
        _l, pay, tools = make()
        llm = self.Hostile([
            ("build_cart", {"items": [{"item_id": "MUG-001", "quantity": 1, "unit_amount": "0.01"}], "currency": "USD"}),
            ("create_order", {"cart_id": "cart-0", "amount": {"value": "0.01"}}),
            ("capture_order", {"order_id": "ORDER-FROM-ELSEWHERE"}),
            ("refund_capture", {"capture_id": "CAP-FROM-ELSEWHERE", "amount": "100.00"}),
            ("transfer_funds", {"to": "someone", "amount": "1.00"}),
        ])
        seen = []
        run_turn(llm, tools, [], "go", lambda n, a, o: seen.append(o["ok"]))
        self.assertEqual(seen, [False] * 5)
        self.assertEqual(pay.calls, [])

    def test_tool_results_roundtrip_as_json_blocks(self):
        llm, _pay, tools = make()
        msgs = []
        run_turn(llm, tools, msgs, "a mug")
        results = [b for m in msgs if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_result"]
        self.assertTrue(results)
        for b in results:
            json.loads(b["content"])

    def test_step_limit(self):
        class Loop(LLM):
            def complete(self, system, messages, tools):
                return LLMResponse("", [ToolCall("t", "search_catalog", {"query": "mug"})])
        _l, _p, tools = make()
        self.assertIn("too many steps", run_turn(Loop(), tools, [], "x", max_steps=3))


if __name__ == "__main__":
    unittest.main()
