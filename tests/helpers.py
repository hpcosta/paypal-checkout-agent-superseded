from paypal_checkout_agent.guardrails import Policy
from paypal_checkout_agent.llm import ScriptedLLM
from paypal_checkout_agent.payments import MockPayments
from paypal_checkout_agent.tools import CheckoutTools


def make(confirm=lambda _a: True, policy=None):
    pay = MockPayments()
    tools = CheckoutTools(pay, policy or Policy(), confirm=confirm, session_id="test-session")
    return ScriptedLLM(), pay, tools


def approved_order(tools, pay, items=None, currency="USD"):
    """Build a cart, create an order and simulate buyer approval. Returns the order id."""
    items = items or [{"item_id": "MUG-001", "quantity": 2}]
    cart = tools.dispatch("build_cart", {"items": items, "currency": currency})["cart"]
    order = tools.dispatch("create_order", {"cart_id": cart["cart_id"]})["order"]
    pay.simulate_buyer_approval(order["order_id"])
    return order["order_id"]
