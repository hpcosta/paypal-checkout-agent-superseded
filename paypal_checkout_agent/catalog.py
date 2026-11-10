"""Synthetic catalogue. Prices live here and only here: the agent never supplies a price."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .money import to_money


@dataclass(frozen=True)
class Item:
    item_id: str
    name: str
    tags: tuple
    prices: dict  # currency code -> Decimal

    def price(self, currency: str) -> Decimal:
        try:
            return self.prices[currency]
        except KeyError:
            raise KeyError(f"{self.item_id} has no {currency} price") from None


def _item(item_id, name, tags, usd, eur):
    return Item(item_id, name, tuple(tags), {"USD": to_money(usd), "EUR": to_money(eur)})


ITEMS = (
    _item("TEE-001", "Sample Cotton Tee", ("tee", "shirt", "tshirt", "t-shirt", "clothing"), "19.00", "17.50"),
    _item("MUG-001", "Sample Ceramic Mug", ("mug", "cup", "coffee", "kitchen"), "12.50", "11.50"),
    _item("STK-001", "Sample Sticker Pack", ("sticker", "stickers", "pack"), "3.25", "3.00"),
    _item("NTB-001", "Sample Dotted Notebook", ("notebook", "journal", "paper"), "8.75", "8.00"),
    _item("BTL-001", "Sample Steel Bottle", ("bottle", "flask", "water"), "18.40", "17.00"),
    _item("HDY-001", "Sample Zip Hoodie", ("hoodie", "sweatshirt", "jacket", "clothing"), "42.00", "39.00"),
)

_BY_ID = {i.item_id: i for i in ITEMS}


def get(item_id: str) -> Item:
    try:
        return _BY_ID[item_id]
    except KeyError:
        raise KeyError(f"unknown item_id {item_id!r}") from None


def stem(word: str) -> str:
    """Tiny plural stemmer: mugs -> mug, hoodies -> hoodie."""
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-1]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def search(query: str, limit: int = 5) -> list:
    """Keyword search over name and tags. Returns Items best-first, ties by catalogue order."""
    words = {stem(w) for w in "".join(c if c.isalnum() or c == "-" else " " for c in query).split()}
    scored = []
    for pos, item in enumerate(ITEMS):
        keys = {stem(t) for t in item.tags} | {stem(w) for w in item.name.split()}
        score = len(words & keys)
        if score:
            scored.append((-score, pos, item))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in scored[:limit]]
