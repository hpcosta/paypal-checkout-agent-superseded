"""Offline, deterministic, in-memory payment adapter that mirrors the Orders v2 shape.

It is a test double: no network, no money, no PayPal. Buyer approval is simulated by
calling `simulate_buyer_approval`, standing in for the buyer logging in on PayPal.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..cart import Cart
from ..money import fmt
from .base import PaymentAdapter, PaymentError


class MockPayments(PaymentAdapter):
    name = "mock"

    def __init__(self):
        self._orders: dict = {}
        self._captures: dict = {}  # capture_id -> {"order_id", "currency", "amount", "refunded"}
        self._idem: dict = {}
        self._n = 0
        self.calls: list = []  # (operation, key) for assertions

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n:04d}"

    def _idempotent(self, op: str, request_id: str, fn):
        key = (op, request_id)
        if key in self._idem:
            return self._idem[key]
        result = fn()
        self._idem[key] = result
        return result

    def create_order(self, cart: Cart, request_id: str) -> dict:
        self.calls.append(("create_order", request_id))

        def make():
            oid = self._next("MOCKORDER")
            total, cur = fmt(cart.total), cart.currency
            order = {
                "id": oid,
                "intent": "CAPTURE",
                "status": "PAYER_ACTION_REQUIRED",
                "purchase_units": [{
                    "reference_id": "default",
                    "amount": {"currency_code": cur, "value": total,
                               "breakdown": {"item_total": {"currency_code": cur, "value": total}}},
                    "items": [{"name": l.name, "sku": l.item_id, "quantity": str(l.quantity),
                               "unit_amount": {"currency_code": cur, "value": fmt(l.unit_price)}} for l in cart.lines],
                }],
                "links": [
                    {"rel": "self", "method": "GET", "href": f"mock://orders/{oid}"},
                    {"rel": "payer-action", "method": "GET", "href": f"https://mock-paypal.invalid/checkoutnow?token={oid}"},
                ],
            }
            self._orders[oid] = order
            return order

        return self._idempotent("create", request_id, make)

    def _order(self, order_id: str) -> dict:
        try:
            return self._orders[order_id]
        except KeyError:
            raise PaymentError("RESOURCE_NOT_FOUND", "The specified resource does not exist.", 404) from None

    def get_order(self, order_id: str) -> dict:
        self.calls.append(("get_order", order_id))
        return self._order(order_id)

    def simulate_buyer_approval(self, order_id: str) -> None:
        order = self._order(order_id)
        if order["status"] == "PAYER_ACTION_REQUIRED":
            order["status"] = "APPROVED"

    def capture_order(self, order_id: str, request_id: str) -> dict:
        self.calls.append(("capture_order", request_id))

        def do():
            order = self._order(order_id)
            if order["status"] == "COMPLETED":
                return order
            if order["status"] != "APPROVED":
                raise PaymentError("ORDER_NOT_APPROVED", "Payer has not yet approved the Order for payment.", 422)
            amount = order["purchase_units"][0]["amount"]
            cap_id = self._next("MOCKCAP")
            order["status"] = "COMPLETED"
            order["purchase_units"][0]["payments"] = {"captures": [{
                "id": cap_id, "status": "COMPLETED",
                "amount": {"currency_code": amount["currency_code"], "value": amount["value"]},
            }]}
            self._captures[cap_id] = {"order_id": order_id, "currency": amount["currency_code"],
                                      "amount": Decimal(amount["value"]), "refunded": Decimal("0.00")}
            return order

        return self._idempotent("capture", request_id, do)

    def refund_capture(self, capture_id: str, amount: Optional[Decimal], currency: str, note: str, request_id: str) -> dict:
        self.calls.append(("refund_capture", request_id))

        def do():
            cap = self._captures.get(capture_id)
            if cap is None:
                raise PaymentError("RESOURCE_NOT_FOUND", "The specified resource does not exist.", 404)
            remaining = cap["amount"] - cap["refunded"]
            if remaining <= 0:
                raise PaymentError("CAPTURE_FULLY_REFUNDED", "The capture has already been fully refunded.", 422)
            value = remaining if amount is None else amount
            if currency != cap["currency"]:
                raise PaymentError("CURRENCY_MISMATCH", "Refund currency must match the capture.", 422)
            if value > remaining:
                raise PaymentError("REFUND_AMOUNT_EXCEEDED", "Refund amount exceeds the refundable balance.", 422)
            cap["refunded"] += value
            return {"id": self._next("MOCKREF"), "status": "COMPLETED",
                    "amount": {"currency_code": cap["currency"], "value": fmt(value)}, "note_to_payer": note}

        return self._idempotent("refund", request_id, do)
