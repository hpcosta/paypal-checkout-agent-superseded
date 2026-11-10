#!/usr/bin/env python3
"""Live sandbox proof, no LLM: check configuration and the OAuth token, then create and read one order.

    python3 scripts/live_smoke.py                  # PayPal sandbox (needs the two sandbox env vars)
    python3 scripts/live_smoke.py --payments mock  # offline rehearsal

It prints ids and statuses only, never credentials. It stops before capture, because capturing
needs a human buyer to approve the order in the sandbox (then use `smoke --capture <ORDER_ID> --refund`).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paypal_checkout_agent import cli  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--payments", choices=["mock", "sandbox"], default="sandbox")
    a = ap.parse_args(argv)
    rc = cli.main(["doctor", "--payments", a.payments, "--live"])
    if rc:
        return rc
    return cli.main(["smoke", "--payments", a.payments])


if __name__ == "__main__":
    sys.exit(main())
