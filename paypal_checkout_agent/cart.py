"""Cart built only from catalogue prices."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from . import catalog
from .guardrails import GuardrailError, Policy
from .money import fmt


@dataclass(frozen=True)
class CartLine:
    item_id: str
    name: str
    unit_price: Decimal
    quantity: int

    @property
    def subtotal(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass(frozen=True)
class Cart:
    currency: str
    lines: tuple

    @property
    def total(self) -> Decimal:
        return sum((l.subtotal for l in self.lines), Decimal("0.00"))

    @property
    def cart_id(self) -> str:
        raw = self.currency + "|" + "|".join(f"{l.item_id}x{l.quantity}@{fmt(l.unit_price)}" for l in self.lines)
        return "cart-" + hashlib.sha256(raw.encode()).hexdigest()[:12]

    def to_public(self) -> dict:
        return {
            "cart_id": self.cart_id,
            "currency": self.currency,
            "lines": [
                {"item_id": l.item_id, "name": l.name, "unit_price": fmt(l.unit_price),
                 "quantity": l.quantity, "subtotal": fmt(l.subtotal)}
                for l in self.lines
            ],
            "total": fmt(self.total),
        }


def build(lines: list, currency: str, policy: Policy) -> Cart:
    """lines: [{'item_id':..., 'quantity':...}]. Duplicate ids are merged. Prices come from the catalogue."""
    if currency not in policy.currencies:
        raise GuardrailError(f"currency {currency!r} is not allowed; allowed: {list(policy.currencies)}")
    merged: dict = {}
    for entry in lines:
        merged[entry["item_id"]] = merged.get(entry["item_id"], 0) + entry["quantity"]
    if len(merged) > policy.max_lines:
        raise GuardrailError(f"too many distinct items (max {policy.max_lines})")
    built = []
    for item_id, qty in merged.items():
        try:
            item = catalog.get(item_id)
        except KeyError as exc:
            raise GuardrailError(str(exc)) from None
        if qty > policy.max_line_quantity:
            raise GuardrailError(f"quantity for {item_id} exceeds the per-item limit of {policy.max_line_quantity}")
        built.append(CartLine(item.item_id, item.name, item.price(currency), qty))
    cart = Cart(currency, tuple(built))
    if cart.total > policy.max_order_total:
        raise GuardrailError(
            f"order total {fmt(cart.total)} {currency} exceeds the limit of {fmt(policy.max_order_total)} {currency}"
        )
    return cart
