import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from paypal_checkout_agent import cli


def run(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_catalog(self):
        rc, out, _ = run(["catalog"])
        self.assertEqual(rc, 0)
        self.assertIn("MUG-001", out)
        self.assertIn("12.50 USD", out)

    def test_one_shot_ask(self):
        rc, out, _ = run(["ask", "--quiet", "two", "mugs"])
        self.assertEqual(rc, 0)
        self.assertIn("25.00 USD", out)
        self.assertIn("scripted test double", out)

    def test_piped_chat_full_journey(self):
        script = "2 mugs and a tee\n/approve\nI approved it, capture\nhow do refunds work\nrefund $5.00\n/quit\n"
        rc, out, _ = run(["chat", "--yes"], script)
        self.assertEqual(rc, 0)
        for needle in ("44.00 USD", "Payment captured", "refund policy", "Refund MOCKREF-0003 COMPLETED: 5.00 USD",
                       "[tool] create_order", "[confirm] Capture 44.00 USD"):
            self.assertIn(needle, out)

    def test_confirmation_prompt_reads_the_shopper_answer(self):
        script = "1 mug\n/approve\nI approved it, capture\nn\n/quit\n"
        rc, out, _ = run(["chat"], script)
        self.assertIn("declined", out)
        self.assertNotIn("Payment captured", out)

    def test_doctor_sandbox_without_keys(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            rc, out, _ = run(["doctor", "--payments", "sandbox"])
        self.assertEqual(rc, 1)
        self.assertIn("MISSING", out)

    def test_doctor_never_prints_secret_values(self):
        env = {"PAYPAL_SANDBOX_CLIENT_ID": "id-value-xyz", "PAYPAL_SANDBOX_CLIENT_SECRET": "secret-value-xyz"}  # secret-scan: allow
        with mock.patch.dict(os.environ, env, clear=True):
            rc, out, err = run(["doctor", "--payments", "sandbox"])
        self.assertEqual(rc, 0)
        self.assertNotIn("value-xyz", out + err)

    def test_smoke_mock(self):
        rc, out, _ = run(["smoke"])
        self.assertEqual(rc, 0)
        self.assertIn('"step": "create_order"', out)
        self.assertIn("PAYER_ACTION_REQUIRED", out)

    def test_live_smoke_script_rehearses_on_mock(self):
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location("live_smoke", os.path.join(root, "scripts", "live_smoke.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mod.main(["--payments", "mock"])
        self.assertEqual(rc, 0)
        self.assertIn('"step": "get_order"', out.getvalue())

    def test_sandbox_with_live_base_url_refuses_to_start(self):
        env = {"PAYPAL_SANDBOX_CLIENT_ID": "a", "PAYPAL_SANDBOX_CLIENT_SECRET": "b", "PAYPAL_BASE_URL": "https://api-m." + "paypal.com"}  # secret-scan: allow
        with mock.patch.dict(os.environ, env, clear=True):
            rc, out, err = run(["ask", "--payments", "sandbox", "a", "mug"])
        self.assertEqual(rc, 2)
        self.assertIn("sandbox", err)


if __name__ == "__main__":
    unittest.main()
