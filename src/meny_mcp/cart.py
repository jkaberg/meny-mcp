"""Cart assembly and pricing.

Two distinct things live here, and the distinction matters:

  * ``PUT /api/calculator/{chain}/{store}`` is a STATELESS pricing and
    validation service. It takes an entire cart, returns totals, applies
    discounts and flags per-store availability. It does NOT store anything.

  * The authoritative anonymous cart lives in the browser, in
    ``localStorage["CART"]``. Nothing server-side holds it - every intercepted
    line carried ``hasBeenSynced: false``.

So this module builds and prices a cart, then emits a payload that
``handoff.py`` injects into the browser. Verified end-to-end on 2026-08-15:
an injected line survived a hard reload and was re-priced by the site itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .client import MenyClient, client as default_client
from .config import (
    DEFAULT_BAG_TYPE,
    DEFAULT_EDIT_MODE,
    DEFAULT_HANDOVER_TYPE,
    ChainProfile,
    endpoints,
)
from .models import CalculatedCart, CartLine, Product
from .search import SearchService, search_service as default_search

log = logging.getLogger("meny_mcp.cart")


@dataclass
class CartItemRequest:
    """What the caller wants, before resolution to a concrete product."""

    ean: str
    quantity: float = 1
    note: str = ""

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "CartItemRequest":
        """Build from a loose dict, accepting a shopping-list row as-is.

        ``plan_week_menu`` names the quantity ``packs``, because that is what
        it means there. Accepting both spellings is what lets its
        ``shopping_list`` be handed straight to ``price_cart`` and
        ``build_basket_script`` without the response having to carry a
        stripped-down second copy of itself for the purpose.
        """
        quantity = item.get("quantity")
        if quantity is None:
            quantity = item.get("packs")
        return cls(
            ean=str(item["ean"]),
            quantity=float(1 if quantity is None else quantity),
            note=str(item.get("note", "")),
        )


@dataclass
class ResolvedItem:
    product: Product
    raw: dict[str, Any]
    quantity: float
    requested_quantity: float
    note: str = ""

    @property
    def adjusted(self) -> bool:
        return abs(self.quantity - self.requested_quantity) > 1e-6

    def summary(self) -> dict[str, Any]:
        data = {
            "ean": self.product.ean,
            "title": " ".join(
                x for x in (self.product.title, self.product.subtitle) if x
            ),
            "quantity": self.quantity,
            "price_each": self.product.effective_price,
            "line_total": round((self.product.effective_price or 0) * self.quantity, 2),
        }
        if self.product.on_sale:
            data["on_sale"] = True
        if self.adjusted:
            data["quantity_adjusted_from"] = self.requested_quantity
            data["adjustment_reason"] = (
                f"minimum/step is {self.product.min_quantity}"
            )
        if self.note:
            data["note"] = self.note
        return data


class CartService:
    def __init__(
        self,
        client: MenyClient | None = None,
        search: SearchService | None = None,
        profile: ChainProfile | None = None,
    ) -> None:
        self.client = client or default_client
        self.search = search or default_search
        self.profile = profile or ChainProfile.current()

    # --------------------------------------------------------------- resolving
    async def resolve(
        self, requests: Sequence[CartItemRequest], *, store_id: str | None = None
    ) -> tuple[list[ResolvedItem], list[dict[str, Any]]]:
        """Fetch full product records and legalise quantities.

        Returns ``(resolved, problems)``. A missing or unavailable EAN becomes
        a problem entry rather than an exception, so one dead item cannot sink
        a whole week's menu.
        """
        requests = list(requests)
        errors: dict[str, str] = {}
        fetched = await self.search.get_products(
            [r.ean for r in requests], store_id=store_id, errors=errors
        )

        resolved: list[ResolvedItem] = []
        problems: list[dict[str, Any]] = []

        for req in requests:
            entry = fetched.get(req.ean)
            if entry is None:
                problems.append({
                    "ean": req.ean,
                    "problem": "not_found",
                    "detail": errors.get(req.ean, "no product record at this store"),
                })
                continue
            product, raw = entry
            if not product.available:
                problems.append({
                    "ean": req.ean,
                    "title": product.title,
                    "problem": "unavailable",
                    "detail": _unavailable_reason(product),
                })
                continue
            resolved.append(ResolvedItem(
                product=product,
                raw=raw,
                quantity=product.quantise(req.quantity),
                requested_quantity=req.quantity,
                note=req.note,
            ))
        return resolved, problems

    # ---------------------------------------------------------------- building
    @staticmethod
    def build_lines(items: Iterable[ResolvedItem]) -> list[CartLine]:
        return [
            CartLine.build(item.product, item.raw, item.quantity)
            for item in items
        ]

    @staticmethod
    def to_payload(lines: Iterable[CartLine]) -> list[dict[str, Any]]:
        return [line.to_payload() for line in lines]

    # ----------------------------------------------------------------- pricing
    async def calculate(
        self,
        lines: Sequence[CartLine],
        *,
        store_id: str | None = None,
        bag_type: str = DEFAULT_BAG_TYPE,
        handover_type: str = DEFAULT_HANDOVER_TYPE,
    ) -> CalculatedCart:
        """Price a cart through the platform's own calculator.

        This is the authoritative total: it applies mix-and-match offers and
        member discounts that per-product prices do not reflect.
        """
        if not lines:
            return CalculatedCart()
        url = endpoints.url(
            endpoints.calculator,
            chain_id=self.profile.chain_id,
            store_id=store_id or self.profile.store_id,
        )
        params = {
            "bagtype": bag_type,
            "editmode": DEFAULT_EDIT_MODE,
            "handoverType": handover_type,
        }
        payload = self.to_payload(lines)
        data = await self.client.put(url, params=params, json_body=payload)
        return CalculatedCart.model_validate(data or {})

    # ------------------------------------------------------------ orchestration
    async def build_and_price(
        self,
        requests: Sequence[CartItemRequest],
        *,
        store_id: str | None = None,
        handover_type: str = DEFAULT_HANDOVER_TYPE,
        include_items: bool = True,
        include_payload: bool = False,
    ) -> dict[str, Any]:
        """Full pipeline: resolve -> build -> price -> report.

        The returned dict is the thing to show the user BEFORE touching their
        real basket.

        ``include_payload`` is off by default: the serialised lines embed one
        ~8.4 kB product record each and no caller outside this module needs
        them. ``include_items`` lets a caller that already lists the products
        (``plan_week_menu``) suppress a second copy.
        """
        resolved, problems = await self.resolve(requests, store_id=store_id)
        lines = self.build_lines(resolved)

        calculated = CalculatedCart()
        if lines:
            try:
                calculated = await self.calculate(
                    lines, store_id=store_id, handover_type=handover_type
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("calculator failed, falling back to local sum: %s", exc)
                problems.append({
                    "problem": "calculator_unavailable",
                    "detail": str(exc),
                })

        # The calculator is the only source that knows per-store availability.
        for ean in calculated.unavailable_eans:
            problems.append({
                "ean": ean,
                "problem": "not_in_this_store",
                "detail": "product exists but is not stocked at the chosen store",
            })

        local_total = round(
            sum((i.product.effective_price or 0) * i.quantity for i in resolved), 2
        )
        totals = calculated.totals

        summary: dict[str, Any] = {
            "totals": _compact({
                "total_to_pay": (totals.total_to_pay
                                 if totals.total_to_pay is not None else local_total),
                "items_subtotal": totals.calculator_total_from_engine or local_total,
                "list_price_before_offers": totals.calculator_total,
                "offer_discount": totals.total_offer_discount,
                "fees": totals.total_fees,
                # Reserve for weight-variable goods; part of total_to_pay but
                # partially refunded once the real picked weight is known.
                "weight_buffer": totals.buffer,
                "total_weight_kg": totals.total_weight,
                "deposit_pant": totals.total_recycle_value,
                "item_count": totals.number_of_items or len(resolved),
            }),
        }
        if include_items:
            summary["items"] = [item.summary() for item in resolved]
        else:
            summary["adjusted_quantities"] = [
                i.summary() for i in resolved if i.adjusted
            ]
        if problems:
            summary["problems"] = problems
        hints = _mix_match_hints(calculated)
        if hints:
            summary["upsell_hints"] = hints
        if include_payload:
            summary["lines_payload"] = self.to_payload(lines)
        return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _unavailable_reason(product: Product) -> str:
    reasons = []
    if product.is_out_of_stock:
        reasons.append("out of stock")
    if not product.is_for_sale:
        reasons.append("not for sale")
    if product.is_revoked:
        reasons.append("withdrawn")
    if product.must_order_from_supplier:
        reasons.append("must be ordered from supplier")
    return ", ".join(reasons) or "unavailable"


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is ``None`` - a null tells the reader nothing."""
    return {k: v for k, v in data.items() if v is not None}


def _mix_match_hints(cart: CalculatedCart) -> list[dict[str, Any]]:
    """Turn ``missingMixMatch`` into actionable 'add one more' suggestions.

    The raw entry is deliberately not echoed: it is a large nested promotion
    object and the three fields below are the whole actionable content.
    """
    return [
        _compact({
            "offer": entry.get("name") or entry.get("promoName")
                     or entry.get("marketTextLong"),
            "missing_count": entry.get("missing") or entry.get("missingCount"),
            "detail": entry.get("description") or entry.get("promoMarketTextLong"),
        })
        for entry in cart.missing_mix_match
    ]


cart_service = CartService()
