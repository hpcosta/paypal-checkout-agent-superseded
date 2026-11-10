"""Guardrails: policy limits and strict schema validation of every tool argument.

The model never supplies a price, a total, a payee or a URL. Tool schemas forbid extra
properties, so a `price` or `amount` smuggled into create_order/build_cart is rejected
before any payment call is made.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal


class GuardrailError(Exception):
    """A tool call violated policy or its schema. Safe to show to the model."""


@dataclass(frozen=True)
class Policy:
    currencies: tuple = ("USD", "EUR")
    max_order_total: Decimal = Decimal("200.00")
    max_line_quantity: int = 10
    max_lines: int = 8
    max_refunds_per_capture: int = 3


_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate(schema: dict, value, path: str = "arguments"):
    """Validate `value` against a small, strict JSON-Schema subset. Raises GuardrailError."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise GuardrailError(f"{path}: expected object")
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                raise GuardrailError(f"{path}: missing required property '{req}'")
        if schema.get("additionalProperties", True) is False:
            for key in value:
                if key not in props:
                    raise GuardrailError(f"{path}: unexpected property '{key}' (not allowed)")
        for key, sub in props.items():
            if key in value:
                validate(sub, value[key], f"{path}.{key}")
    elif t == "array":
        if not isinstance(value, list):
            raise GuardrailError(f"{path}: expected array")
        if len(value) < schema.get("minItems", 0):
            raise GuardrailError(f"{path}: too few items")
        if len(value) > schema.get("maxItems", 10**9):
            raise GuardrailError(f"{path}: too many items")
        for n, el in enumerate(value):
            validate(schema.get("items", {}), el, f"{path}[{n}]")
    elif t == "string":
        if not isinstance(value, str):
            raise GuardrailError(f"{path}: expected string")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10**6):
            raise GuardrailError(f"{path}: string length out of range")
        if "enum" in schema and value not in schema["enum"]:
            raise GuardrailError(f"{path}: must be one of {schema['enum']}")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise GuardrailError(f"{path}: does not match required pattern")
    elif t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise GuardrailError(f"{path}: expected integer")
        if value < schema.get("minimum", -(10**12)) or value > schema.get("maximum", 10**12):
            raise GuardrailError(f"{path}: integer out of range")
    elif t == "boolean":
        if not isinstance(value, bool):
            raise GuardrailError(f"{path}: expected boolean")
    else:
        raise GuardrailError(f"{path}: unsupported schema type {t!r}")


def check_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise GuardrailError(f"{what} has an invalid format")
    return value
