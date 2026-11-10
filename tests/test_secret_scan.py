import importlib.util
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("secret_scan", os.path.join(ROOT, "scripts", "secret_scan.py"))
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


class SecretScanTests(unittest.TestCase):
    def test_repository_tree_is_clean(self):
        self.assertEqual(scan.scan_tree(ROOT), [])

    def test_detects_key_shapes(self):
        samples = {
            "anthropic-api-key": "k = '" + "sk-" + "ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2" + "'",
            "private-key-block": "-----BEGIN " + "RSA PRIVATE KEY-----",
            "aws-access-key-id": "AKIA" + "ABCDEFGHIJKLMNOP",
            "github-token": "ghp_" + "a" * 36,
            "bearer-token": "Authorization: Bearer " + "abcDEF0123456789." * 3,
            "quoted-credential-assignment": "client_secret = '" + "Zx9Qw8Er7Ty6Ui5Op4As3" + "'",
            "paypal-credential-shape": "A" + "bC1" * 25,
        }
        for rule, line in samples.items():
            self.assertIn(rule, scan.scan_line(line), rule)

    def test_allow_marker_and_innocent_lines(self):
        self.assertEqual(scan.scan_line("client_secret = '" + "Zx9Qw8Er7Ty6Ui5Op4As3" + "'  # secret-scan: allow"), [])
        for line in ("PAYPAL_SANDBOX_CLIENT_SECRET", "token = self._access_token()", "sku = 'MUG-001'",
                     "def validate_sandbox_base_url(url):"):
            self.assertEqual(scan.scan_line(line), [])

    def test_scan_tree_finds_planted_secret_without_printing_it(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "x.txt"), "w") as fh:
                fh.write("ok\n" + "ghp_" + "b" * 36 + "\n")
            found = scan.scan_tree(d)
        self.assertEqual(found, [("x.txt", 2, "github-token")])


if __name__ == "__main__":
    unittest.main()
