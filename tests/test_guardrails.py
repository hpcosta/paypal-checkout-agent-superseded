import unittest
from decimal import Decimal

from paypal_checkout_agent import cart as cartmod
from paypal_checkout_agent.guardrails import GuardrailError, Policy, check_id, validate
from paypal_checkout_agent.money import fmt, to_money
from paypal_checkout_agent.tools import TOOLS


class SchemaTests(unittest.TestCase):
    def schema(self, name):
        return next(t["input_schema"] for t in TOOLS if t["name"] == name)

    def test_every_tool_forbids_extra_properties(self):
        for t in TOOLS:
            self.assertIs(t["input_schema"]["additionalProperties"], False, t["name"])

    def test_extra_property_rejected(self):
        with self.assertRaises(GuardrailError):
            validate(self.schema("create_order"), {"cart_id": "cart-abc", "amount": "0.01"})

    def test_missing_required_rejected(self):
        with self.assertRaises(GuardrailError):
            validate(self.schema("capture_order"), {})

    def test_wrong_types_rejected(self):
        s = self.schema("build_cart")
        for bad in (
            {"items": [{"item_id": "MUG-001", "quantity": "2"}], "currency": "USD"},
            {"items": [{"item_id": "MUG-001", "quantity": True}], "currency": "USD"},
            {"items": [{"item_id": "MUG-001", "quantity": 0}], "currency": "USD"},
            {"items": [], "currency": "USD"},
            {"items": [{"item_id": "MUG-001", "quantity": 1}], "currency": "GBP"},
            {"items": [{"item_id": "MUG 001; drop", "quantity": 1}], "currency": "USD"},
        ):
            with self.assertRaises(GuardrailError, msg=str(bad)):
                validate(s, bad)

    def test_amount_pattern(self):
        s = self.schema("refund_capture")
        validate(s, {"capture_id": "CAP-1", "amount": "5.00"})
        for bad in ("5", "-1.00", "5.0", "1e3", "5.00 "):
            with self.assertRaises(GuardrailError, msg=bad):
                validate(s, {"capture_id": "CAP-1", "amount": bad})

    def test_check_id(self):
        self.assertEqual(check_id("ABC-123_x", "id"), "ABC-123_x")
        for bad in ("", "a b", "../x", "x" * 65, None):
            with self.assertRaises(GuardrailError):
                check_id(bad, "id")


class MoneyAndCartTests(unittest.TestCase):
    def test_money(self):
        self.assertEqual(fmt(to_money("12.5")), "12.50")
        with self.assertRaises(ValueError):
            to_money(1.1)
        with self.assertRaises(ValueError):
            to_money("NaN")

    def test_prices_come_from_catalogue(self):
        cart = cartmod.build([{"item_id": "MUG-001", "quantity": 2}, {"item_id": "TEE-001", "quantity": 1}], "USD", Policy())
        self.assertEqual(cart.total, Decimal("44.00"))
        eur = cartmod.build([{"item_id": "MUG-001", "quantity": 2}], "EUR", Policy())
        self.assertEqual(eur.total, Decimal("23.00"))

    def test_duplicates_merge_and_limits(self):
        cart = cartmod.build([{"item_id": "STK-001", "quantity": 3}, {"item_id": "STK-001", "quantity": 2}], "USD", Policy())
        self.assertEqual(cart.lines[0].quantity, 5)
        with self.assertRaises(GuardrailError):
            cartmod.build([{"item_id": "STK-001", "quantity": 6}, {"item_id": "STK-001", "quantity": 6}], "USD", Policy())
        with self.assertRaises(GuardrailError):  # 5 hoodies = 210.00 > 200.00
            cartmod.build([{"item_id": "HDY-001", "quantity": 5}], "USD", Policy())
        with self.assertRaises(GuardrailError):
            cartmod.build([{"item_id": "NOPE-1", "quantity": 1}], "USD", Policy())
        with self.assertRaises(GuardrailError):
            cartmod.build([{"item_id": "MUG-001", "quantity": 1}], "JPY", Policy())


if __name__ == "__main__":
    unittest.main()
