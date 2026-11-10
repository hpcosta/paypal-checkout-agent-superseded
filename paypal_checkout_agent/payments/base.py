"""Payment adapter contract plus the compact summaries shown to the model.

Adapters return PayPal-shaped dicts (Orders v2 / Payments v2). The summaries below are
what the LLM sees: ids, statuses and amounts, never headers or credentials.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..cart import Cart


class PaymentError(Exception):
    """A payment-provider error (or a refusal to call the provider)."""

    def __init__(self, code: str, message: str, status: Optional[int] = None, debug_id: Optional[str] = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status, self.debug_id = code, message, status, debug_id

    def to_public(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.status is not None:
            d["http_status"] = self.status
        if self.debug_id:
            d["debug_id"] = self.debug_id
        return d


class PaymentAdapter:
    name = "abstract"

    def create_order(self, cart: Cart, request_id: str) -> dict:
        raise NotImplementedError

    def get_order(self, order_id: str) -> dict:
        raise NotImplementedError

    def capture_order(self, order_id: str, request_id: str) -> dict:
        raise NotImplementedError

    def refund_capture(self, capture_id: str, amount: Optional[Decimal], currency: str, note: str, request_id: str) -> dict:
        raise NotImplementedError


def _first_capture(order: dict) -> Optional[dict]:
    for pu in order.get("purchase_units", []):
        caps = pu.get("payments", {}).get("captures", [])
        if caps:
            return caps[0]
    return None


def approval_url(order: dict) -> Optional[str]:
    for link in order.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return link.get("href")
    return None


def summarize_order(order: dict) -> dict:
    pu = (order.get("purchase_units") or [{}])[0]
    amount = pu.get("amount", {})
    out = {
        "order_id": order.get("id"),
        "status": order.get("status"),
        "total": amount.get("value"),
        "currency": amount.get("currency_code"),
    }
    url = approval_url(order)
    if url and order.get("status") not in ("COMPLETED", "VOIDED"):
        out["approval_url"] = url
    cap = _first_capture(order)
    if cap:
        out["capture_id"] = cap.get("id")
        out["capture_status"] = cap.get("status")
        out["captured_amount"] = cap.get("amount", {}).get("value")
    return out


def summarize_refund(refund: dict) -> dict:
    return {
        "refund_id": refund.get("id"),
        "status": refund.get("status"),
        "amount": refund.get("amount", {}).get("value"),
        "currency": refund.get("amount", {}).get("currency_code"),
    }
