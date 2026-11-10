"""The structured tools the model may call, and the guard-railed executor behind them.

The model chooses tools and arguments. Everything with consequences is decided here:
prices come from the catalogue, ids must belong to this session, money-moving steps need
the shopper's confirmation through a callback the model cannot answer, and every argument
is validated against the tool's schema (extra properties are rejected).
"""
from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Callable, Optional

from . import cart as cartmod
from . import catalog
from .guardrails import GuardrailError, Policy, check_id, validate
from .money import fmt, to_money
from .payments import PaymentAdapter, PaymentError, summarize_order, summarize_refund

_ID = {"type": "string", "pattern": r"^[A-Za-z0-9_-]{1,64}$"}

TOOLS = [
    {
        "name": "search_catalog",
        "description": "Search the shop catalogue by keywords. Returns item ids, names and prices. Use it to find item ids.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 120},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10}}},
    },
    {
        "name": "build_cart",
        "description": "Build the shopper's cart from catalogue item ids and quantities. Prices are applied from the "
                       "catalogue; you cannot set or change a price.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["items", "currency"], "properties": {
            "items": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
                "type": "object", "additionalProperties": False, "required": ["item_id", "quantity"], "properties": {
                    "item_id": _ID, "quantity": {"type": "integer", "minimum": 1, "maximum": 10}}}},
            "currency": {"type": "string", "enum": ["USD", "EUR"]}}},
    },
    {
        "name": "create_order",
        "description": "Create a PayPal sandbox order for the current cart and return the buyer approval URL. "
                       "The shopper must open that URL and approve before payment can be captured.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["cart_id"], "properties": {
            "cart_id": _ID}},
    },
    {
        "name": "get_order",
        "description": "Read the current status of an order created in this session (for example whether the shopper approved it).",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["order_id"], "properties": {
            "order_id": _ID}},
    },
    {
        "name": "capture_order",
        "description": "Capture the payment for an order the shopper has approved. Asks the shopper to confirm first.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["order_id"], "properties": {
            "order_id": _ID}},
    },
    {
        "name": "explain_refund",
        "description": "Explain the shop's refund policy and, when a capture id is given, how much of it can still be refunded.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {"capture_id": _ID}},
    },
    {
        "name": "refund_capture",
        "description": "Refund a captured payment in full (omit amount) or in part. Asks the shopper to confirm first.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["capture_id"], "properties": {
            "capture_id": _ID,
            "amount": {"type": "string", "pattern": r"^[0-9]{1,5}\.[0-9]{2}$"},
            "reason": {"type": "string", "maxLength": 140}}},
    },
]
_SCHEMAS = {t["name"]: t["input_schema"] for t in TOOLS}

REFUND_POLICY = (
    "Sample shop refund policy (synthetic, for this demo): a captured payment can be refunded in full or in part, "
    "up to the amount captured, at most 3 refunds per payment. The refund goes back to the buyer's original PayPal "
    "funding source; how quickly it shows up depends on that source. Refunds are made against the capture id, which "
    "the shop receives when payment is captured."
)

Confirm = Callable[[str], bool]


class CheckoutTools:
    def __init__(self, payments: PaymentAdapter, policy: Optional[Policy] = None,
                 confirm: Optional[Confirm] = None, session_id: Optional[str] = None):
        self.payments = payments
        self.policy = policy or Policy()
        self._confirm = confirm or (lambda _action: False)  # default deny: money never moves unattended
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.cart: Optional[cartmod.Cart] = None
        self.orders: dict = {}    # order_id -> {"total": Decimal, "currency": str}
        self.captures: dict = {}  # capture_id -> {"currency", "amount", "refunded", "count"}
        self.audit: list = []     # (tool, ok) per call, for the CLI trace and tests

    # -- helpers --------------------------------------------------------------------

    def _rid(self, *parts: str) -> str:
        raw = "|".join((self.session_id,) + parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:40]

    # -- dispatch -------------------------------------------------------------------

    def dispatch(self, name: str, arguments) -> dict:
        """Run one tool call. Never raises: failures come back as {'ok': False, 'error': {...}}."""
        try:
            if name not in _SCHEMAS:
                raise GuardrailError(f"unknown tool {name!r}")
            validate(_SCHEMAS[name], arguments)
            data = getattr(self, "_t_" + name)(**arguments)
            self.audit.append((name, True))
            return {"ok": True, **data}
        except GuardrailError as exc:
            self.audit.append((name, False))
            return {"ok": False, "error": {"type": "guardrail", "message": str(exc)}}
        except PaymentError as exc:
            self.audit.append((name, False))
            return {"ok": False, "error": {"type": "payment", **exc.to_public()}}

    # -- tools ----------------------------------------------------------------------

    def _t_search_catalog(self, query: str, limit: int = 5) -> dict:
        found = catalog.search(query, limit)
        return {"items": [{"item_id": i.item_id, "name": i.name, "tags": list(i.tags),
                           "prices": {c: fmt(p) for c, p in i.prices.items()}} for i in found]}

    def _t_build_cart(self, items: list, currency: str) -> dict:
        self.cart = cartmod.build(items, currency, self.policy)
        return {"cart": self.cart.to_public()}

    def _t_create_order(self, cart_id: str) -> dict:
        if self.cart is None or self.cart.cart_id != cart_id:
            raise GuardrailError("cart_id does not match the current cart; call build_cart first")
        if self.cart.total > self.policy.max_order_total or self.cart.currency not in self.policy.currencies:
            raise GuardrailError("cart violates policy")
        order = self.payments.create_order(self.cart, self._rid("create", cart_id))
        summary = summarize_order(order)
        self.orders[summary["order_id"]] = {"total": self.cart.total, "currency": self.cart.currency}
        return {"order": summary,
                "next_step": "Ask the shopper to open approval_url and approve the payment, then tell you."}

    def _known_order(self, order_id: str) -> dict:
        check_id(order_id, "order_id")
        if order_id not in self.orders:
            raise GuardrailError("order_id was not created in this session")
        return self.orders[order_id]

    def _t_get_order(self, order_id: str) -> dict:
        self._known_order(order_id)
        return {"order": summarize_order(self.payments.get_order(order_id))}

    def _t_capture_order(self, order_id: str) -> dict:
        expected = self._known_order(order_id)
        current = summarize_order(self.payments.get_order(order_id))
        if current["status"] == "COMPLETED" and current.get("capture_id"):
            self._remember_capture(current, expected)
            return {"order": current, "note": "already captured"}
        if current["status"] != "APPROVED":
            raise GuardrailError(
                f"order status is {current['status']}, not APPROVED: the shopper has not approved it yet; "
                "ask them to open the approval URL")
        if not self._confirm(f"Capture {fmt(expected['total'])} {expected['currency']} for order {order_id}?"):
            raise GuardrailError("the shopper declined the capture; nothing was charged")
        done = summarize_order(self.payments.capture_order(order_id, self._rid("capture", order_id)))
        if done.get("capture_status") != "COMPLETED":
            raise GuardrailError(f"capture did not complete (status {done.get('capture_status')})")
        self._remember_capture(done, expected)
        return {"order": done}

    def _remember_capture(self, summary: dict, expected: dict) -> None:
        cid = summary["capture_id"]
        if to_money(summary["captured_amount"]) != expected["total"] or summary["currency"] != expected["currency"]:
            raise GuardrailError("captured amount differs from the order total; stopping for review")
        self.captures.setdefault(cid, {"currency": expected["currency"], "amount": expected["total"],
                                       "refunded": Decimal("0.00"), "count": 0})

    def _known_capture(self, capture_id: str) -> dict:
        check_id(capture_id, "capture_id")
        if capture_id not in self.captures:
            raise GuardrailError("capture_id was not captured in this session")
        return self.captures[capture_id]

    def _t_explain_refund(self, capture_id: Optional[str] = None) -> dict:
        out = {"policy": REFUND_POLICY}
        if capture_id is not None:
            cap = self._known_capture(capture_id)
            out["capture"] = {"capture_id": capture_id, "currency": cap["currency"], "captured": fmt(cap["amount"]),
                              "already_refunded": fmt(cap["refunded"]),
                              "refundable_now": fmt(cap["amount"] - cap["refunded"]),
                              "refunds_made": cap["count"], "refunds_allowed": self.policy.max_refunds_per_capture}
        return out

    def _t_refund_capture(self, capture_id: str, amount: Optional[str] = None, reason: str = "Refund requested by shopper") -> dict:
        cap = self._known_capture(capture_id)
        remaining = cap["amount"] - cap["refunded"]
        if remaining <= 0:
            raise GuardrailError("this capture is already fully refunded")
        if cap["count"] >= self.policy.max_refunds_per_capture:
            raise GuardrailError("refund count limit reached for this capture")
        value = remaining if amount is None else to_money(amount)
        if value <= 0 or value > remaining:
            raise GuardrailError(f"refund must be between 0.01 and the refundable balance {fmt(remaining)} {cap['currency']}")
        if not self._confirm(f"Refund {fmt(value)} {cap['currency']} of capture {capture_id}?"):
            raise GuardrailError("the shopper declined the refund; nothing was refunded")
        result = self.payments.refund_capture(
            capture_id, None if amount is None else value, cap["currency"], reason,
            self._rid("refund", capture_id, str(cap["count"]), fmt(value)))
        cap["refunded"] += value
        cap["count"] += 1
        refund = summarize_refund(result)
        if refund.get("amount") is None:  # a minimal API answer carries no amount: report what was requested
            refund["amount"], refund["currency"] = fmt(value), cap["currency"]
        return {"refund": refund, "refundable_remaining": fmt(cap["amount"] - cap["refunded"])}
