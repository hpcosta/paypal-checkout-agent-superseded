"""Command line: chat, ask, catalog, doctor, smoke."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import catalog
from .agent import run_turn
from .cart import build as build_cart
from .guardrails import Policy
from .llm import LLMError, make_llm
from .money import fmt
from .payments import MockPayments, PaymentError, make_adapter, summarize_order, summarize_refund
from .tools import CheckoutTools


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--llm", choices=["scripted", "anthropic"], default=os.environ.get("CHECKOUT_AGENT_LLM", "scripted"),
                   help="language-model backend (default: scripted test double)")
    p.add_argument("--payments", choices=["mock", "sandbox"], default=os.environ.get("CHECKOUT_AGENT_PAYMENTS", "mock"),
                   help="payment adapter (default: mock, offline)")
    p.add_argument("--yes", action="store_true", help="auto-confirm capture and refund prompts (demo/testing only)")
    p.add_argument("--quiet", action="store_true", help="hide the tool-call trace")


def _confirmer(auto_yes: bool, readline):
    def confirm(action: str) -> bool:
        if auto_yes:
            print(f"  [confirm] {action} -> yes (--yes)")
            return True
        print(f"  [confirm] {action} [y/N] ", end="", flush=True)
        answer = readline()
        return answer.strip().lower() in ("y", "yes")
    return confirm


def _tracer(quiet: bool):
    if quiet:
        return None

    def trace(name, args, outcome):
        shown = json.dumps(args, separators=(",", ":"))
        status = "ok" if outcome["ok"] else "REFUSED: " + outcome["error"].get("message", "")
        print(f"  [tool] {name} {shown} -> {status}")
    return trace


def _build_session(args, readline):
    llm = make_llm(args.llm)
    payments = make_adapter(args.payments)
    tools = CheckoutTools(payments, Policy(), confirm=_confirmer(args.yes, readline))
    return llm, payments, tools


def _readline() -> str:
    return sys.stdin.readline()


def cmd_chat(args) -> int:
    llm, payments, tools = _build_session(args, _readline)
    banner = f"PayPal Checkout Agent | llm={llm.name} | payments={payments.name}"
    if llm.is_test_double:
        banner += " (scripted LLM is a test double)"
    print(banner)
    print("Type what you want to buy. Commands: /approve (mock only), /quit")
    messages: list = []
    interactive = sys.stdin.isatty()
    while True:
        if interactive:
            print("you> ", end="", flush=True)
        line = sys.stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        if not interactive:
            print(f"you> {line}")
        if line in ("/quit", "/exit"):
            return 0
        if line == "/approve":
            if isinstance(payments, MockPayments) and tools.orders:
                payments.simulate_buyer_approval(list(tools.orders)[-1])
                print("  [mock] buyer approval simulated; now tell the assistant you approved")
            else:
                print("  /approve only exists for the mock adapter; in sandbox, open the approval URL in a browser "
                      "signed in to a sandbox buyer account")
            continue
        try:
            print("agent> " + run_turn(llm, tools, messages, line, _tracer(args.quiet)))
        except LLMError as exc:
            print(f"agent> language-model error: {exc}")
            return 2


def cmd_ask(args) -> int:
    llm, _payments, tools = _build_session(args, _readline)
    try:
        print(run_turn(llm, tools, [], " ".join(args.text), _tracer(args.quiet)))
    except LLMError as exc:
        print(f"language-model error: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_catalog(_args) -> int:
    for item in catalog.ITEMS:
        prices = ", ".join(f"{fmt(p)} {c}" for c, p in item.prices.items())
        print(f"{item.item_id}  {item.name:<26} {prices}")
    return 0


def cmd_doctor(args) -> int:
    print(f"llm backend       : {args.llm}" + (" (test double)" if args.llm == "scripted" else ""))
    if args.llm == "anthropic":
        print(f"ANTHROPIC_API_KEY : {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'MISSING'}")
        print(f"model             : {os.environ.get('CHECKOUT_AGENT_MODEL') or 'claude-sonnet-5-5 (default)'}")
    print(f"payments adapter  : {args.payments}")
    if args.payments == "sandbox":
        for var in ("PAYPAL_SANDBOX_CLIENT_ID", "PAYPAL_SANDBOX_CLIENT_SECRET"):
            print(f"{var:<30}: {'set' if os.environ.get(var) else 'MISSING'}")
        try:
            adapter = make_adapter("sandbox")
        except PaymentError as exc:
            print(f"sandbox adapter   : not usable ({exc.message})")
            return 1
        print("sandbox base URL  : ok (api-m.sandbox.paypal.com only)")
        if args.live:
            try:
                adapter._access_token()
            except PaymentError as exc:
                print(f"live token check  : FAILED ({exc.code}: {exc.message})")
                return 1
            print("live token check  : OK (OAuth2 client-credentials accepted by the sandbox)")
    return 0


def cmd_smoke(args) -> int:
    """Payment-adapter smoke run, no LLM. Prints ids/statuses only; never secrets."""
    try:
        adapter = make_adapter(args.payments)
        if args.capture:
            order = summarize_order(adapter.get_order(args.capture))
            print(json.dumps({"step": "get_order", **order}))
            if order["status"] != "APPROVED":
                print(f"order is {order['status']}, not APPROVED; approve it in the sandbox buyer session first")
                return 3
            done = summarize_order(adapter.capture_order(args.capture, f"smoke-capture-{args.capture}"))
            print(json.dumps({"step": "capture_order", **done}))
            if args.refund:
                ref = summarize_refund(adapter.refund_capture(
                    done["capture_id"], None, done["currency"], "Sandbox smoke refund", f"smoke-refund-{done['capture_id']}"))
                print(json.dumps({"step": "refund_capture", **ref}))
            return 0
        cart = build_cart([{"item_id": "MUG-001", "quantity": 1}], "USD", Policy())
        created = summarize_order(adapter.create_order(cart, "smoke-create-" + cart.cart_id))
        print(json.dumps({"step": "create_order", **created}))
        fetched = summarize_order(adapter.get_order(created["order_id"]))
        print(json.dumps({"step": "get_order", **fetched}))
        print("next: open approval_url in a browser signed in to a SANDBOX buyer account, approve, then run "
              f"smoke --capture {created['order_id']} [--refund]")
        return 0
    except PaymentError as exc:
        print(f"payment error: {exc.code}: {exc.message}" + (f" (debug_id {exc.debug_id})" if exc.debug_id else ""),
              file=sys.stderr)
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="paypal_checkout_agent",
                                     description="LLM checkout agent on the PayPal sandbox (mock adapter by default).")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("chat", help="interactive (or piped) conversation"); _common(p); p.set_defaults(fn=cmd_chat)
    p = sub.add_parser("ask", help="one-shot request"); _common(p)
    p.add_argument("text", nargs="+"); p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("catalog", help="list the synthetic catalogue"); p.set_defaults(fn=cmd_catalog)
    p = sub.add_parser("doctor", help="show configuration status (no secrets printed)"); _common(p)
    p.add_argument("--live", action="store_true", help="also request an OAuth token from the sandbox"); p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("smoke", help="adapter smoke run without an LLM")
    p.add_argument("--payments", choices=["mock", "sandbox"], default="mock")
    p.add_argument("--capture", metavar="ORDER_ID", help="capture this already-approved order")
    p.add_argument("--refund", action="store_true", help="with --capture: then refund the capture in full")
    p.set_defaults(fn=cmd_smoke)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (PaymentError, LLMError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
