"""PayPal REST SANDBOX adapter (Orders v2 + Payments v2), standard library only.

Status: exercised offline against a fake transport (see tests/test_sandbox_adapter.py) and,
on 2026-11-09, against the live PayPal sandbox end to end (OAuth token, create order, buyer
approval, capture, partial refund). Nothing here can reach the live (production) PayPal API:
the base URL must be exactly https://api-m.sandbox.paypal.com.

Endpoints used:
  POST /v1/oauth2/token                       client-credentials grant
  POST /v2/checkout/orders                    create order (intent CAPTURE)
  GET  /v2/checkout/orders/{id}               show order details
  POST /v2/checkout/orders/{id}/capture       capture payment for an approved order
  POST /v2/payments/captures/{id}/refund      refund a captured payment

Secrets are read from the environment, held in memory, sent only in the Authorization
header, and never logged, printed or placed in exception messages.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Optional

from ..cart import Cart
from ..money import fmt
from .base import PaymentAdapter, PaymentError

log = logging.getLogger("paypal_checkout_agent.sandbox")

SANDBOX_HOST = "api-m.sandbox.paypal.com"
SANDBOX_BASE_URL = "https://" + SANDBOX_HOST
_LIVE_MODE_WORDS = {"live", "production", "prod"}


def validate_sandbox_base_url(url: str) -> str:
    """Return the normalised sandbox base URL, or raise PaymentError for anything else."""
    parsed = urllib.parse.urlsplit((url or "").strip())
    ok = (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() == SANDBOX_HOST
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )
    if not ok:
        raise PaymentError(
            "NON_SANDBOX_BASE_URL",
            f"refusing to run: the base URL must be exactly {SANDBOX_BASE_URL} (sandbox only)",
        )
    return SANDBOX_BASE_URL


class UrllibTransport:
    """Default HTTP transport. Returns (status, body_bytes); never raises on HTTP errors."""

    def request(self, method: str, url: str, headers: dict, body: Optional[bytes], timeout: float):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - https URL validated above
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PaymentError("NETWORK_ERROR", f"could not reach PayPal sandbox ({type(exc).__name__})") from None


class SandboxPayments(PaymentAdapter):
    name = "sandbox"

    def __init__(self, client_id: str, client_secret: str, base_url: str = SANDBOX_BASE_URL,
                 transport=None, return_url: str = "https://example.com/return",
                 cancel_url: str = "https://example.com/cancel", brand_name: str = "Sample Shop (sandbox)",
                 timeout: float = 30.0):
        if not client_id or not client_secret:
            raise PaymentError("MISSING_CREDENTIALS",
                               "set PAYPAL_SANDBOX_CLIENT_ID and PAYPAL_SANDBOX_CLIENT_SECRET in the environment")
        self._base = validate_sandbox_base_url(base_url)
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._transport = transport or UrllibTransport()
        self._return_url, self._cancel_url, self._brand = return_url, cancel_url, brand_name
        self._timeout = timeout
        self._token: Optional[str] = None
        self._token_expiry = 0.0

    def __repr__(self) -> str:  # never include credentials
        return f"SandboxPayments(base_url={self._base!r}, credentials=<hidden>)"

    @classmethod
    def from_env(cls, env=None, transport=None) -> "SandboxPayments":
        env = os.environ if env is None else env
        for var in ("PAYPAL_ENV", "PAYPAL_MODE", "PAYPAL_ENVIRONMENT"):
            if env.get(var, "").strip().lower() in _LIVE_MODE_WORDS:
                raise PaymentError("NON_SANDBOX_BASE_URL", f"refusing to run: {var} selects a live environment")
        for var in ("PAYPAL_BASE_URL", "PAYPAL_API_BASE", "PAYPAL_SANDBOX_BASE_URL"):
            if env.get(var):
                validate_sandbox_base_url(env[var])
        return cls(
            env.get("PAYPAL_SANDBOX_CLIENT_ID", ""),
            env.get("PAYPAL_SANDBOX_CLIENT_SECRET", ""),
            base_url=env.get("PAYPAL_SANDBOX_BASE_URL") or SANDBOX_BASE_URL,
            transport=transport,
            return_url=env.get("PAYPAL_RETURN_URL", "https://example.com/return"),
            cancel_url=env.get("PAYPAL_CANCEL_URL", "https://example.com/cancel"),
        )

    # -- plumbing -----------------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        status, raw = self._transport.request(
            "POST", self._base + "/v1/oauth2/token",
            {"Authorization": "Basic " + self._basic, "Content-Type": "application/x-www-form-urlencoded",
             "Accept": "application/json"},
            b"grant_type=client_credentials", self._timeout)
        log.info("POST /v1/oauth2/token -> %s", status)
        data = self._parse(status, raw)
        token = data.get("access_token")
        if not token:
            raise PaymentError("AUTH_FAILED", "token response had no access_token", status)
        self._token = token
        self._token_expiry = time.time() + float(data.get("expires_in", 300))
        return token

    @staticmethod
    def _parse(status: int, raw: bytes) -> dict:
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        if 200 <= status < 300:
            return data if isinstance(data, dict) else {}
        code = data.get("error") or data.get("name") or "HTTP_ERROR"
        details = data.get("details") or []
        if details and isinstance(details[0], dict) and details[0].get("issue"):
            code = details[0]["issue"]
        msg = data.get("error_description") or data.get("message") or f"PayPal returned HTTP {status}"
        raise PaymentError(str(code), str(msg)[:300], status, data.get("debug_id"))

    def _call(self, method: str, path: str, payload: Optional[dict] = None, request_id: Optional[str] = None,
              prefer_representation: bool = False) -> dict:
        headers = {"Authorization": "Bearer " + self._access_token(), "Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
        if method == "POST":
            headers["Content-Type"] = "application/json"
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if prefer_representation:
            headers["Prefer"] = "return=representation"
        status, raw = self._transport.request(method, self._base + path, headers, body, self._timeout)
        log.info("%s %s -> %s", method, path, status)
        return self._parse(status, raw)

    # -- operations ---------------------------------------------------------------

    def create_order(self, cart: Cart, request_id: str) -> dict:
        cur, total = cart.currency, fmt(cart.total)
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": "default",
                "amount": {"currency_code": cur, "value": total,
                           "breakdown": {"item_total": {"currency_code": cur, "value": total}}},
                "items": [{"name": l.name, "sku": l.item_id, "quantity": str(l.quantity),
                           "unit_amount": {"currency_code": cur, "value": fmt(l.unit_price)}} for l in cart.lines],
            }],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": self._brand,
                "shipping_preference": "NO_SHIPPING",
                "user_action": "PAY_NOW",
                "return_url": self._return_url,
                "cancel_url": self._cancel_url,
            }}},
        }
        return self._call("POST", "/v2/checkout/orders", payload, request_id, prefer_representation=True)

    def get_order(self, order_id: str) -> dict:
        return self._call("GET", "/v2/checkout/orders/" + urllib.parse.quote(order_id, safe=""))

    def capture_order(self, order_id: str, request_id: str) -> dict:
        return self._call("POST", "/v2/checkout/orders/" + urllib.parse.quote(order_id, safe="") + "/capture",
                          None, request_id, prefer_representation=True)

    def refund_capture(self, capture_id: str, amount: Optional[Decimal], currency: str, note: str, request_id: str) -> dict:
        payload: dict = {"note_to_payer": note[:255]}
        if amount is not None:  # omitting the amount refunds the remaining captured amount
            payload["amount"] = {"currency_code": currency, "value": fmt(amount)}
        # Without Prefer: return=representation the live sandbox answers 201 with only id, status
        # and links (observed 2026-11-09), so the refunded amount would be missing.
        return self._call("POST", "/v2/payments/captures/" + urllib.parse.quote(capture_id, safe="") + "/refund",
                          payload, request_id, prefer_representation=True)
