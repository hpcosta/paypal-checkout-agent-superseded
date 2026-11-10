"""Decimal money helpers. Amounts are strings on the wire, Decimals in code."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")


def to_money(value) -> Decimal:
    """Parse a money value (str/int/Decimal) into a 2-decimal Decimal. Floats are refused."""
    if isinstance(value, float):
        raise ValueError("floats are not accepted for money")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not a money amount: {value!r}") from exc
    if not d.is_finite():
        raise ValueError("money amount must be finite")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(value: Decimal) -> str:
    return f"{value.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"
