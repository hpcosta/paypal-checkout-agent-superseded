import unittest

from paypal_checkout_agent.guardrails import Policy
from tests.helpers import approved_order, make


class ToolGuardTests(unittest.TestCase):
    def test_model_cannot_set_a_price(self):
        _llm, pay, tools = make()
        r = tools.dispatch("build_cart", {"items": [{"item_id": "MUG-001", "quantity": 1, "price": "0.01"}], "currency": "USD"})
        self.assertFalse(r["ok"])
        self.assertIn("unexpected property", r["error"]["message"])
        r = tools.dispatch("build_cart", {"items": [{"item_id": "MUG-001", "quantity": 1}], "currency": "USD", "total": "0.01"})
        self.assertFalse(r["ok"])
        cart = tools.dispatch("build_cart", {"items": [{"item_id": "MUG-001", "quantity": 1}], "currency": "USD"})["cart"]
        r = tools.dispatch("create_order", {"cart_id": cart["cart_id"], "amount": "0.01"})
        self.assertFalse(r["ok"])
        self.assertEqual(pay.calls, [])  # nothing reached the payment adapter

    def test_order_total_equals_catalogue_total(self):
        _llm, pay, tools = make()
        oid = approved_order(tools, pay, [{"item_id": "MUG-001", "quantity": 2}, {"item_id": "TEE-001", "quantity": 1}])
        order = pay.get_order(oid)
        self.assertEqual(order["purchase_units"][0]["amount"]["value"], "44.00")

    def test_create_order_requires_matching_cart(self):
        _llm, pay, tools = make()
        self.assertFalse(tools.dispatch("create_order", {"cart_id": "cart-nope"})["ok"])
        self.assertEqual(pay.calls, [])

    def test_unknown_tool_and_non_object_arguments(self):
        _llm, _pay, tools = make()
        self.assertFalse(tools.dispatch("drop_tables", {})["ok"])
        self.assertFalse(tools.dispatch("get_order", "ORDER-1")["ok"])

    def test_cannot_touch_ids_from_outside_the_session(self):
        _llm, pay, tools = make()
        for name in ("get_order", "capture_order"):
            r = tools.dispatch(name, {"order_id": "SOMEONE-ELSES-ORDER"})
            self.assertFalse(r["ok"])
            self.assertIn("not created in this session", r["error"]["message"])
        self.assertFalse(tools.dispatch("refund_capture", {"capture_id": "FOREIGN-CAPTURE"})["ok"])

    def test_capture_before_approval_is_refused(self):
        _llm, pay, tools = make()
        cart = tools.dispatch("build_cart", {"items": [{"item_id": "MUG-001", "quantity": 1}], "currency": "USD"})["cart"]
        oid = tools.dispatch("create_order", {"cart_id": cart["cart_id"]})["order"]["order_id"]
        r = tools.dispatch("capture_order", {"order_id": oid})
        self.assertFalse(r["ok"])
        self.assertIn("not approved", r["error"]["message"])
        self.assertNotIn(("capture_order", tools._rid("capture", oid)), pay.calls)

    def test_declined_confirmation_moves_no_money(self):
        _llm, pay, tools = make(confirm=lambda _a: False)
        oid = approved_order(tools, pay)
        r = tools.dispatch("capture_order", {"order_id": oid})
        self.assertFalse(r["ok"])
        self.assertIn("declined", r["error"]["message"])
        self.assertEqual(pay.get_order(oid)["status"], "APPROVED")

    def test_default_confirmation_is_deny(self):
        from paypal_checkout_agent.payments import MockPayments
        from paypal_checkout_agent.tools import CheckoutTools
        pay = MockPayments()
        tools = CheckoutTools(pay)
        oid = approved_order(tools, pay)
        self.assertFalse(tools.dispatch("capture_order", {"order_id": oid})["ok"])

    def test_capture_is_idempotent(self):
        _llm, pay, tools = make()
        oid = approved_order(tools, pay)
        a = tools.dispatch("capture_order", {"order_id": oid})
        b = tools.dispatch("capture_order", {"order_id": oid})
        self.assertTrue(a["ok"] and b["ok"])
        self.assertEqual(a["order"]["capture_id"], b["order"]["capture_id"])
        self.assertEqual(len(tools.captures), 1)

    def test_refund_limits(self):
        _llm, pay, tools = make()
        oid = approved_order(tools, pay)  # 25.00
        cap = tools.dispatch("capture_order", {"order_id": oid})["order"]["capture_id"]
        self.assertFalse(tools.dispatch("refund_capture", {"capture_id": cap, "amount": "25.01"})["ok"])
        self.assertFalse(tools.dispatch("refund_capture", {"capture_id": cap, "amount": "0.00"})["ok"])
        # the executor refuses before the provider is ever asked
        self.assertFalse([c for c in pay.calls if c[0] == "refund_capture"])
        r = tools.dispatch("refund_capture", {"capture_id": cap, "amount": "10.00"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["refundable_remaining"], "15.00")
        self.assertFalse(tools.dispatch("refund_capture", {"capture_id": cap, "amount": "15.01"})["ok"])
        full = tools.dispatch("refund_capture", {"capture_id": cap})
        self.assertEqual(full["refund"]["amount"], "15.00")
        self.assertIn("fully refunded", tools.dispatch("refund_capture", {"capture_id": cap})["error"]["message"])

    def test_refund_count_limit(self):
        _llm, pay, tools = make()
        oid = approved_order(tools, pay)
        cap = tools.dispatch("capture_order", {"order_id": oid})["order"]["capture_id"]
        for _ in range(3):
            self.assertTrue(tools.dispatch("refund_capture", {"capture_id": cap, "amount": "1.00"})["ok"])
        r = tools.dispatch("refund_capture", {"capture_id": cap, "amount": "1.00"})
        self.assertFalse(r["ok"])
        self.assertIn("limit", r["error"]["message"])

    def test_refund_declined(self):
        answers = iter([True, False])
        _llm, pay, tools = make(confirm=lambda _a: next(answers))
        oid = approved_order(tools, pay)
        cap = tools.dispatch("capture_order", {"order_id": oid})["order"]["capture_id"]
        self.assertFalse(tools.dispatch("refund_capture", {"capture_id": cap})["ok"])
        self.assertEqual(tools.captures[cap]["refunded"], 0)

    def test_explain_refund_reports_balance(self):
        _llm, pay, tools = make()
        oid = approved_order(tools, pay)
        cap = tools.dispatch("capture_order", {"order_id": oid})["order"]["capture_id"]
        tools.dispatch("refund_capture", {"capture_id": cap, "amount": "5.00"})
        info = tools.dispatch("explain_refund", {"capture_id": cap})
        self.assertEqual(info["capture"]["refundable_now"], "20.00")
        self.assertIn("policy", info)
        self.assertNotIn("capture", tools.dispatch("explain_refund", {}))

    def test_custom_policy_limits(self):
        from decimal import Decimal
        _llm, _pay, tools = make(policy=Policy(max_order_total=Decimal("10.00")))
        r = tools.dispatch("build_cart", {"items": [{"item_id": "MUG-001", "quantity": 1}], "currency": "USD"})
        self.assertFalse(r["ok"])
        self.assertIn("exceeds the limit", r["error"]["message"])


class MinimalRefundAnswerTests(unittest.TestCase):
    def test_refund_without_amount_in_the_answer_reports_the_requested_amount(self):
        from paypal_checkout_agent.guardrails import Policy
        from paypal_checkout_agent.payments import MockPayments
        from paypal_checkout_agent.tools import CheckoutTools
        from tests.helpers import approved_order

        class MinimalRefund(MockPayments):
            def refund_capture(self, *a, **k):
                full = super().refund_capture(*a, **k)
                return {"id": full["id"], "status": full["status"]}  # the live sandbox's default answer

        pay = MinimalRefund()
        tools = CheckoutTools(pay, Policy(), confirm=lambda _a: True, session_id="test-session")
        oid = approved_order(tools, pay)
        cap = tools.dispatch("capture_order", {"order_id": oid})["order"]["capture_id"]
        out = tools.dispatch("refund_capture", {"capture_id": cap, "amount": "5.00"})
        self.assertTrue(out["ok"])
        self.assertEqual((out["refund"]["amount"], out["refund"]["currency"]), ("5.00", "USD"))
        self.assertEqual(out["refundable_remaining"], "20.00")


if __name__ == "__main__":
    unittest.main()
