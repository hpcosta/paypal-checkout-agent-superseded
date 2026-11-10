from .base import PaymentAdapter, PaymentError, summarize_order, summarize_refund
from .mock import MockPayments
from .sandbox import SandboxPayments, validate_sandbox_base_url

__all__ = ["PaymentAdapter", "PaymentError", "MockPayments", "SandboxPayments",
           "summarize_order", "summarize_refund", "validate_sandbox_base_url"]


def make_adapter(kind: str):
    if kind == "mock":
        return MockPayments()
    if kind == "sandbox":
        return SandboxPayments.from_env()
    raise ValueError(f"unknown payments adapter {kind!r}")
