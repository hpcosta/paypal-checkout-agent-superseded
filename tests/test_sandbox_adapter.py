"""Offline tests of the sandbox adapter against a fake transport.

These prove the adapter builds the documented request shapes and keeps secrets out of
logs, reprs and errors. They do NOT prove behaviour against the live PayPal sandbox.
"""
import base64
import json
import logging
import unittest

from paypal_checkout_agent import cart as cartmod
from paypal_checkout_agent.guardrails import Policy
from paypal_checkout_agent.payments import PaymentError, SandboxPayments, summarize_order, summarize_refund, validate_sandbox_base_url

# Built from parts so this file carries no literal non-sandbox endpoint.
LIVE_M = "https://api-m." + "paypal.com"
LIVE = "https://api." + "paypal.com"
CLIENT_ID = "fake-client-id-0000"  # secret-scan: allow
CLIENT_SECRET = "fake-client-secret-0000"  # secret-scan: allow
TOKEN = "fake-access-token-0000"  # secret-scan: allow


class FakeTransport:
    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def request(self, method, url, headers, body, timeout):
        self.requests.append((method, url, dict(headers), body))
        for (m, suffix), (status, payload) in self.routes.items():
            if m == method and url.endswith(suffix):
                return status, json.dumps(payload).encode()
        return 404, b"{}"


ORDER = {"id": "5O190127TN364715T", "status": "PAYER_ACTION_REQUIRED",
         "purchase_units": [{"amount": {"currency_code": "USD", "value": "25.00"}}],
         "links": [{"rel": "self", "href": "https://api-m.sandbox.paypal.com/v2/checkout/orders/5O190127TN364715T"},
                   {"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T"}]}
TOKEN_OK = (200, {"access_token": TOKEN, "expires_in": 32000, "token_type": "Bearer"})


def adapter(routes):
    t = FakeTransport({("POST", "/v1/oauth2/token"): TOKEN_OK, **routes})
    return SandboxPayments(CLIENT_ID, CLIENT_SECRET, transport=t), t


def cart():
    return cartmod.build([{"item_id": "MUG-001", "quantity": 2}], "USD", Policy())


class BaseUrlTests(unittest.TestCase):
    def test_only_the_sandbox_host_is_accepted(self):
        validate_sandbox_base_url("https://api-m.sandbox.paypal.com")
        validate_sandbox_base_url("https://api-m.sandbox.paypal.com/")
        for bad in (LIVE_M, LIVE, "http://api-m.sandbox.paypal.com",
                    "https://api-m.sandbox.paypal.com.evil.example", "https://evil.example/api-m.sandbox.paypal.com",
                    "https://api-m.sandbox.paypal.com:8443", "https://" + "user" + "@api-m.sandbox.paypal.com",
                    "https://api-m.sandbox.paypal.com/v2", "", "ftp://api-m.sandbox.paypal.com"):
            with self.assertRaises(PaymentError, msg=bad) as cm:
                validate_sandbox_base_url(bad)
            self.assertEqual(cm.exception.code, "NON_SANDBOX_BASE_URL")

    def test_constructor_and_env_refuse_live(self):
        with self.assertRaises(PaymentError):
            SandboxPayments(CLIENT_ID, CLIENT_SECRET, base_url=LIVE_M)
        base = {"PAYPAL_SANDBOX_CLIENT_ID": CLIENT_ID, "PAYPAL_SANDBOX_CLIENT_SECRET": CLIENT_SECRET}
        SandboxPayments.from_env(base)
        for extra in ({"PAYPAL_BASE_URL": LIVE_M}, {"PAYPAL_SANDBOX_BASE_URL": LIVE},
                      {"PAYPAL_API_BASE": LIVE_M}, {"PAYPAL_ENV": "live"}, {"PAYPAL_MODE": "Production"}):
            with self.assertRaises(PaymentError, msg=str(extra)):
                SandboxPayments.from_env({**base, **extra})

    def test_missing_credentials(self):
        with self.assertRaises(PaymentError) as cm:
            SandboxPayments.from_env({})
        self.assertEqual(cm.exception.code, "MISSING_CREDENTIALS")


class RequestShapeTests(unittest.TestCase):
    def test_token_request(self):
        a, t = adapter({("POST", "/v2/checkout/orders"): (201, ORDER)})
        a.create_order(cart(), "req-1")
        method, url, headers, body = t.requests[0]
        self.assertEqual((method, url), ("POST", "https://api-m.sandbox.paypal.com/v1/oauth2/token"))
        self.assertEqual(body, b"grant_type=client_credentials")
        self.assertEqual(base64.b64decode(headers["Authorization"].split()[1]).decode(), f"{CLIENT_ID}:{CLIENT_SECRET}")

    def test_create_order_request(self):
        a, t = adapter({("POST", "/v2/checkout/orders"): (201, ORDER)})
        order = a.create_order(cart(), "req-1")
        method, url, headers, body = t.requests[1]
        self.assertEqual(url, "https://api-m.sandbox.paypal.com/v2/checkout/orders")
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(headers["PayPal-Request-Id"], "req-1")
        self.assertEqual(headers["Content-Type"], "application/json")
        payload = json.loads(body)
        self.assertEqual(payload["intent"], "CAPTURE")
        unit = payload["purchase_units"][0]
        self.assertEqual(unit["amount"], {"currency_code": "USD", "value": "25.00",
                                          "breakdown": {"item_total": {"currency_code": "USD", "value": "25.00"}}})
        self.assertEqual(unit["items"][0], {"name": "Sample Ceramic Mug", "sku": "MUG-001", "quantity": "2",
                                            "unit_amount": {"currency_code": "USD", "value": "12.50"}})
        ctx = payload["payment_source"]["paypal"]["experience_context"]
        self.assertEqual(ctx["user_action"], "PAY_NOW")
        s = summarize_order(order)
        self.assertEqual(s["approval_url"], "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T")
        self.assertEqual(s["total"], "25.00")

    def test_legacy_approve_link_is_understood(self):
        legacy = dict(ORDER, status="CREATED", links=[{"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=X"}])
        self.assertEqual(summarize_order(legacy)["approval_url"], "https://www.sandbox.paypal.com/checkoutnow?token=X")

    def test_get_capture_refund_requests(self):
        cap = {"id": "ORD1", "status": "COMPLETED", "purchase_units": [{"amount": {"currency_code": "USD", "value": "25.00"},
               "payments": {"captures": [{"id": "CAP1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "25.00"}}]}}]}
        a, t = adapter({("GET", "/v2/checkout/orders/ORD1"): (200, ORDER),
                        ("POST", "/v2/checkout/orders/ORD1/capture"): (201, cap),
                        ("POST", "/v2/payments/captures/CAP1/refund"): (201, {"id": "REF1", "status": "COMPLETED",
                                                                              "amount": {"currency_code": "USD", "value": "5.00"}})})
        a.get_order("ORD1")
        done = summarize_order(a.capture_order("ORD1", "cap-key"))
        self.assertEqual((done["capture_id"], done["capture_status"]), ("CAP1", "COMPLETED"))
        part = summarize_refund(a.refund_capture("CAP1", __import__("decimal").Decimal("5"), "USD", "note", "ref-key"))
        self.assertEqual(part["amount"], "5.00")
        a.refund_capture("CAP1", None, "USD", "full", "ref-key-2")
        reqs = {(m, u.split(".com")[1]): (h, b) for m, u, h, b in t.requests[1:]}
        self.assertEqual(reqs[("POST", "/v2/checkout/orders/ORD1/capture")][0]["PayPal-Request-Id"], "cap-key")
        refunds = [b for m, u, h, b in t.requests if u.endswith("/refund")]
        self.assertEqual(json.loads(refunds[0])["amount"], {"currency_code": "USD", "value": "5.00"})
        self.assertNotIn("amount", json.loads(refunds[1]))  # omitted amount = refund the remainder
        self.assertEqual(json.loads(refunds[1])["note_to_payer"], "full")
        refund_headers = [h for m, u, h, b in t.requests if u.endswith("/refund")]
        self.assertTrue(all(h.get("Prefer") == "return=representation" for h in refund_headers))

    def test_token_is_cached(self):
        a, t = adapter({("GET", "/v2/checkout/orders/ORD1"): (200, ORDER)})
        a.get_order("ORD1"); a.get_order("ORD1")
        self.assertEqual(sum(1 for r in t.requests if r[1].endswith("/oauth2/token")), 1)


class ErrorAndSecretTests(unittest.TestCase):
    def test_provider_error_is_mapped(self):
        err = {"name": "UNPROCESSABLE_ENTITY", "message": "The requested action could not be performed.",
               "debug_id": "abc123", "details": [{"issue": "ORDER_NOT_APPROVED"}]}
        a, _t = adapter({("POST", "/v2/checkout/orders/ORD1/capture"): (422, err)})
        with self.assertRaises(PaymentError) as cm:
            a.capture_order("ORD1", "k")
        self.assertEqual((cm.exception.code, cm.exception.status, cm.exception.debug_id), ("ORDER_NOT_APPROVED", 422, "abc123"))

    def test_auth_failure(self):
        t = FakeTransport({("POST", "/v1/oauth2/token"): (401, {"error": "invalid_client", "error_description": "Client Authentication failed"})})
        a = SandboxPayments(CLIENT_ID, CLIENT_SECRET, transport=t)
        with self.assertRaises(PaymentError) as cm:
            a.get_order("ORD1")
        self.assertEqual(cm.exception.code, "invalid_client")

    def test_secrets_never_appear_in_logs_reprs_errors_or_model_visible_output(self):
        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("paypal_checkout_agent.sandbox")
        h, old = H(), logger.level
        logger.addHandler(h); logger.setLevel(logging.DEBUG)
        try:
            a, _t = adapter({("POST", "/v2/checkout/orders"): (201, ORDER),
                             ("POST", "/v2/checkout/orders/ORD1/capture"): (422, {"name": "X", "message": "m", "debug_id": "d"})})
            order = a.create_order(cart(), "req-1")
            with self.assertRaises(PaymentError) as cm:
                a.capture_order("ORD1", "k")
        finally:
            logger.removeHandler(h); logger.setLevel(old)
        blob = " ".join(records + [repr(a), str(cm.exception), json.dumps(summarize_order(order)),
                                   base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()[:0]])
        for secret in (CLIENT_ID, CLIENT_SECRET, TOKEN, "Bearer", "Basic"):
            self.assertNotIn(secret, blob)
        self.assertTrue(records)  # it did log something, just not secrets


if __name__ == "__main__":
    unittest.main()
