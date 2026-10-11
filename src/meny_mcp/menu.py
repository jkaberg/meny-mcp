"""Week-menu resolution.

Turns a meal plan into a concrete, priced basket. The hard parts are not the
HTTP calls but the reconciliation:

  * The same ingredient appears across several days ("løk" on Monday, Wednesday
    and Friday) and must be aggregated into one basket line, not three.
  * Recipe quantities are expressed in cooking units (2 dl fløte, 400 g kjøtt)
    while the shop sells packs. Needs are converted to the product's comparable
    unit, then rounded UP to whole packs.
  * Pantry staples (salt, pepper, oil) should not be bought every week.
  * The product the search ranks first is not always the product the recipe
    means, so every pick is re-checked against its full record before it
    reaches the basket.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .cart import CartItemRequest, CartService, cart_service as default_cart
from .matching import dried_equivalent, parse_ask, query_variants
from .models import Product
from .pricing import Candidate, Preferences, Verdict, _acceptable, rank, verdict_for
from .profile import BASE_PANTRY, matches as pantry_match
from .search import SearchService, search_service as default_search

log = logging.getLogger("meny_mcp.menu")


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
# Recipe unit -> (canonical group, factor to the base unit of that group).
# Base units: mass -> kg, volume -> l, count -> stk.
RECIPE_UNITS: dict[str, tuple[str, float]] = {
    # mass
    "g": ("mass", 0.001), "gram": ("mass", 0.001),
    "hg": ("mass", 0.1), "kg": ("mass", 1.0),
    # volume
    "ml": ("volume", 0.001), "cl": ("volume", 0.01), "dl": ("volume", 0.1),
    "l": ("volume", 1.0), "liter": ("volume", 1.0),
    "ts": ("volume", 0.005),     # teskje  ~5 ml
    "tsp": ("volume", 0.005),
    "ss": ("volume", 0.015),     # spiseskje ~15 ml
    "tbsp": ("volume", 0.015),
    "kopp": ("volume", 0.25),
    # count
    "stk": ("count", 1.0), "pk": ("count", 1.0), "pakke": ("count", 1.0),
    "boks": ("count", 1.0), "glass": ("count", 1.0), "fedd": ("count", 1.0),
    "neve": ("count", 1.0), "bunt": ("count", 1.0),
}

# Things most kitchens already have. Excluded by default; override per call or
# per household - see :mod:`.profile`, which owns the list and the env/session
# overrides. Kept here as a name because it is part of this module's API.
DEFAULT_PANTRY: set[str] = set(BASE_PANTRY)

_QTY_RE = re.compile(
    r"^\s*(?P<qty>\d+(?:[.,]\d+)?(?:\s*[-/]\s*\d+(?:[.,]\d+)?)?)\s*"
    r"(?P<unit>[a-zA-ZæøåÆØÅ]+)?\s*(?P<name>.*)$"
)


@dataclass
class Ingredient:
    """One ingredient line from a recipe."""

    name: str
    quantity: float = 1.0
    unit: str = "stk"
    day: str = ""
    meal: str = ""

    @classmethod
    def parse(cls, text: str, *, day: str = "", meal: str = "") -> "Ingredient":
        """Parse free text such as '400 g kjøttdeig' or '2 stk løk'."""
        match = _QTY_RE.match(text.strip())
        if not match:
            return cls(name=text.strip(), day=day, meal=meal)
        raw_qty = match.group("qty") or "1"
        # ranges ("2-3 stk") -> take the upper bound
        parts = re.split(r"[-/]", raw_qty)
        try:
            qty = max(float(p.replace(",", ".")) for p in parts if p.strip())
        except ValueError:
            qty = 1.0
        unit = (match.group("unit") or "").lower().strip()
        name = (match.group("name") or "").strip()
        if unit and unit not in RECIPE_UNITS:
            # The token was part of the name, not a unit ("2 store løk")
            name = f"{unit} {name}".strip()
            unit = "stk"
        if not name:
            name, unit, qty = text.strip(), "stk", 1.0
        return cls(name=name, quantity=qty, unit=unit or "stk",
                   day=day, meal=meal)

    @property
    def normalised(self) -> tuple[str, float] | None:
        """(group, quantity in that group's base unit)."""
        entry = RECIPE_UNITS.get(self.unit.lower())
        if not entry:
            return None
        group, factor = entry
        return group, self.quantity * factor

    @property
    def key(self) -> str:
        return self.name.strip().lower()


@dataclass
class Meal:
    name: str
    day: str = ""
    ingredients: list[Ingredient] = field(default_factory=list)
    servings: int = 4


@dataclass
class AggregatedNeed:
    """One ingredient, summed across the whole week."""

    name: str
    group: str | None
    quantity: float
    unit: str
    sources: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str | None]:
        """Identity for lookups. The group is part of it, because "2 stk løk"
        and "300 g løk" aggregate into separate needs."""
        return self.name.strip().lower(), self.group

    def summary(self) -> dict[str, Any]:
        return {
            "ingredient": self.name,
            "needed": round(self.quantity, 3),
            "unit": self.unit,
            "used_in": self.sources,
        }


class MenuService:
    def __init__(
        self,
        search: SearchService | None = None,
        cart: CartService | None = None,
    ) -> None:
        self.search = search or default_search
        self.cart = cart or default_cart

    # ------------------------------------------------------------- aggregation
    @staticmethod
    def aggregate(
        meals: Sequence[Meal], *, pantry: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        scale: float = 1.0,
    ) -> tuple[list[AggregatedNeed], list[str], list[str]]:
        """Sum ingredients across meals.

        Returns ``(needs, skipped_pantry, excluded)``.

        Both skip lists match on compound edges rather than string equality: a
        pantry holding "karri" covers "karripulver", and excluding "reker"
        covers "reker i lake". See :func:`.profile.matches`.
        """
        pantry_set = {p.lower() for p in (
            DEFAULT_PANTRY if pantry is None else pantry)}
        exclude_set = {e.lower() for e in (exclude or [])}

        buckets: dict[tuple[str, str | None], AggregatedNeed] = {}
        skipped: list[str] = []
        excluded: list[str] = []

        for meal in meals:
            for ing in meal.ingredients:
                banned = pantry_match(ing.name, exclude_set) if exclude_set else None
                if banned:
                    label = f"{ing.name} (excluded: {banned})"
                    if label not in excluded:
                        excluded.append(label)
                    continue
                if pantry_match(ing.name, pantry_set):
                    if ing.name not in skipped:
                        skipped.append(ing.name)
                    continue
                norm = ing.normalised
                group = norm[0] if norm else None
                amount = (norm[1] if norm else ing.quantity) * scale
                bucket_key = (ing.key, group)
                label = meal.name or meal.day or "meal"
                if bucket_key in buckets:
                    existing = buckets[bucket_key]
                    existing.quantity += amount
                    if label not in existing.sources:
                        existing.sources.append(label)
                else:
                    base_unit = {"mass": "kg", "volume": "l",
                                 "count": "stk"}.get(group or "", ing.unit)
                    buckets[bucket_key] = AggregatedNeed(
                        name=ing.name, group=group, quantity=amount,
                        unit=base_unit, sources=[label],
                    )
        return list(buckets.values()), skipped, excluded

    # --------------------------------------------------------------- resolving
    async def resolve_needs(
        self,
        needs: Sequence[AggregatedNeed],
        *,
        prefs: Preferences | None = None,
        candidates_per_item: int = 12,
        store_id: str | None = None,
        include_alternatives: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Match each need to the best available product.

        Returns ``(matches, unmatched)``.

        ``candidates_per_item`` is the ranking pool per ingredient. Twelve is
        enough for the cheapest-per-unit answer to be stable - beyond that the
        tail is padding, and it is paid for once per ingredient per menu.

        Alternatives are off by default: three runner-up products for each of
        ~25 ingredients is 75 extra product summaries, larger than the answer
        itself, and they are only wanted when the user is second-guessing a
        specific pick.
        """
        prefs = prefs or Preferences()
        results = await self.search.search_many(
            [n.name for n in needs],
            limit=candidates_per_item,
            available_only=True,
            store_id=store_id,
        )

        matches: list[dict[str, Any]] = []
        unmatched: list[dict[str, Any]] = []

        # (need, ranked candidates, the query they were ranked against)
        shortlists: list[tuple[AggregatedNeed, list[Candidate], str]] = []
        stuck: list[AggregatedNeed] = []
        for need in needs:
            ranked = self._shortlist(need, results.get(need.name) or [],
                                     need.name, prefs)
            if ranked:
                shortlists.append((need, ranked, need.name))
            else:
                stuck.append(need)

        rescued = await self._rescue(stuck, prefs, candidates_per_item,
                                     store_id)
        # Anything still stuck gets one last look at the products the ORIGINAL
        # search already returned, this time as full records. That is where the
        # shop's synonyms live, and they are the only thing connecting a
        # category word like "biff" to the eight cuts it actually means.
        still_stuck = [n for n in stuck if n.key not in rescued]
        rescued.update(await self._rescue_by_synonym(
            still_stuck, results, prefs, store_id))
        for need in stuck:
            found = rescued.get(need.key)
            if found:
                shortlists.append((need, found[0], found[1]))
            else:
                seen = len(results.get(need.name) or [])
                unmatched.append({
                    "ingredient": need.name,
                    "reason": "no search results" if not seen
                              else "no acceptable candidate",
                    **({"candidates_seen": seen} if seen else {}),
                    "tried": [need.name, *query_variants(need.name)],
                })

        # Verify every pick against its full product record before it reaches
        # the basket, walking down the shortlist until one passes. The full
        # record carries the shop's own category label and the declared
        # ingredient list, which is where "hermetisk" and an excluded allergen
        # actually become visible - a search hit shows neither. It also carries
        # measurementValue/packageSize, without which pack arithmetic falls
        # back to a placeholder weight and turns "800 g poteter" into one
        # potato. The client caches these fetches and build_and_price needs
        # them next anyway, so the checked path costs no extra HTTP.
        verified = await asyncio.gather(*(
            self._verify(need, ranked, query, prefs, store_id)
            for need, ranked, query in shortlists
        ))

        for (need, ranked, query), outcome in zip(shortlists, verified):
            candidate, product, verdict, rejected = outcome
            matches.append(self._match_row(
                need, candidate, product, verdict, query,
                alternatives=ranked if include_alternatives else None,
                rejected=rejected,
            ))
        return matches, unmatched

    # ------------------------------------------------------------ selection
    @staticmethod
    def _shortlist(need: AggregatedNeed, products: Sequence[Product],
                   query: str, prefs: Preferences) -> list[Candidate]:
        """Rank the candidates for one need, best first."""
        if not products:
            return []
        # For mass/volume this is an amount that constrains pack size; for a
        # count need it is the number of items to cover. An unparseable unit
        # gives neither, so pass nothing rather than a number whose meaning is
        # unknown.
        is_count = need.group == "count"
        needed_qty = (need.quantity
                      if need.group in ("mass", "volume") or is_count
                      else None)
        return rank(products, query, prefs, needed_quantity=needed_qty,
                    count_need=is_count)

    async def _rescue(
        self, stuck: Sequence[AggregatedNeed], prefs: Preferences,
        candidates_per_item: int, store_id: str | None,
    ) -> dict[str, tuple[list[Candidate], str]]:
        """Retry ingredients the shop spells differently.

        Five of twenty-five ingredients on a real week came back as "no
        acceptable candidate" while sitting on the shelf: ``sjampinjong`` is
        stocked as *Champignon*, ``dijonsennep`` as *Sennep Dijon*,
        ``svinefilet`` as *Svin Ytrefilet*. These are the most annoying misses
        precisely because the item exists, and they cost one extra search each
        - paid only on the failure path, never for a menu that resolves
        cleanly.
        """
        if not stuck:
            return {}
        plan: dict[str, list[str]] = {
            need.key: query_variants(need.name) for need in stuck
        }
        every = list(dict.fromkeys(v for vs in plan.values() for v in vs))
        if not every:
            return {}
        found = await self.search.search_many(
            every, limit=candidates_per_item, available_only=True,
            store_id=store_id,
        )
        out: dict[str, tuple[list[Candidate], str]] = {}
        for need in stuck:
            for variant in plan.get(need.key, []):
                ranked = self._shortlist(need, found.get(variant) or [],
                                         variant, prefs)
                if ranked:
                    log.info("rescued %r as %r", need.name, variant)
                    out[need.key] = (ranked, variant)
                    break
        return out

    async def _rescue_by_synonym(
        self, stuck: Sequence[AggregatedNeed],
        results: dict[str, list[Product]], prefs: Preferences,
        store_id: str | None,
    ) -> dict[str, tuple[list[Candidate], str]]:
        """Retry ingredients the shop names something else entirely.

        "biff" is not a spelling problem and no query rewrite reaches it: the
        search returns Mørbrad, Entrecote, Indrefilet and Ytrefilet, none of
        which is called a biff, and all of which list ``biff`` in the synonyms
        on their full record. Same for "sopp", which is five differently-named
        mushrooms, and for the shop's *Aubergin*.

        Paid for only here, on an ingredient that has already failed both the
        plain search and every spelling variant - and the records fetched are
        cached, so a successful rescue costs nothing later.
        """
        if not stuck:
            return {}
        hydrated = await asyncio.gather(*(
            self.search.hydrate(results.get(need.name) or [], store_id=store_id)
            for need in stuck
        ), return_exceptions=True)

        out: dict[str, tuple[list[Candidate], str]] = {}
        for need, products in zip(stuck, hydrated):
            if isinstance(products, BaseException):
                log.warning("synonym rescue failed for %r: %s",
                            need.name, products)
                continue
            ranked = self._shortlist(need, products, need.name, prefs)
            if ranked:
                log.info("rescued %r via the shop's synonyms", need.name)
                out[need.key] = (ranked, need.name)
        return out

    async def _verify(
        self, need: AggregatedNeed, ranked: Sequence[Candidate], query: str,
        prefs: Preferences, store_id: str | None, tries: int = 3,
    ) -> tuple[Candidate, Product, Verdict, list[str]]:
        """Re-judge the shortlist against full records, best first.

        Returns the first candidate that still passes, plus the ones that did
        not and why. If none passes, the top pick is returned unchanged with
        its objections attached rather than dropping the ingredient - the user
        can then swap it, which they cannot do for something that never
        appeared.
        """
        ask = parse_ask(query, prefs.accept_forms)
        rejected: list[str] = []
        fallback: tuple[Candidate, Product, Verdict] | None = None
        for candidate in list(ranked)[:tries]:
            fetched = await self.search.get_products([candidate.product.ean],
                                                     store_id=store_id)
            entry = fetched.get(candidate.product.ean)
            product = entry[0] if entry else candidate.product
            if fallback is None:
                fallback = (candidate, product, candidate.verdict)
            allowed, why = _acceptable(product, prefs)
            verdict = verdict_for(product, ask)
            if allowed and verdict.ok:
                return candidate, product, verdict, rejected
            reason = why or verdict.note or "rejected on full record"
            rejected.append(f"{product.title}: {reason}")
        assert fallback is not None  # ranked is never empty here
        candidate, product, verdict = fallback
        return candidate, product, verdict, rejected

    # -------------------------------------------------------------- reporting
    def _match_row(
        self, need: AggregatedNeed, candidate: Candidate, product: Product,
        verdict: Verdict, query: str, *,
        alternatives: Sequence[Candidate] | None = None,
        rejected: Sequence[str] = (),
    ) -> dict[str, Any]:
        packs = self._packs_required(product, need,
                                     form_mismatch=verdict.form_mismatch)
        # `needed` states its own unit rather than carrying a second key for
        # it: "0.4 kg" costs a third of `"needed": 0.4, "needed_unit": "kg"`
        # repeated once per line, and reads better.
        row: dict[str, Any] = {
            "ingredient": need.name,
            "needed": f"{round(need.quantity, 3):g} {need.unit}",
            "ean": product.ean,
            "title": " ".join(
                x for x in (product.title, product.subtitle) if x
            ),
            "packs": packs,
            "price_each": product.effective_price,
            "line_total": round((product.effective_price or 0) * packs, 2),
            "unit_price": product.unit_price,
            "unit": product.canonical_compare_unit,
            "used_in": need.sources,
        }
        # Pack size is only worth stating when it is doing work: it explains a
        # pack count above one, and it is the number a reader needs when the
        # piece count is unknown (added with that flag below). On a single pack
        # of a known size it is noise.
        if packs > 1 and product.pack_constraint:
            row["pack_quantity"] = product.pack_constraint
        if query != need.name:
            row["matched_as"] = query
        if candidate.via_synonym:
            # The product is not named after the ingredient; the shop's own
            # cross-references are what connect them. Worth saying, because it
            # is the one pick a reader cannot check from the title alone.
            row["matched_by"] = "shop synonym"
        if need.group == "count":
            if product.pieces_per_pack:
                row["pieces_per_pack"] = product.pieces_per_pack
            else:
                # A bag or net: the data does not say how many items are in it,
                # so one pack is assumed to cover the need. Surfaced rather
                # than hidden - it is the one quantity here that might be
                # short - and with the pack size beside it, so the reader can
                # judge instead of guessing.
                row["pack_covers_count"] = "unknown - verify one pack is enough"
                if product.package_size:
                    row["package_size"] = product.package_size
        note = self._form_note(need, product, verdict)
        if note:
            row["substitution"] = note
        if rejected:
            row["rejected"] = list(rejected)
        if product.on_sale:
            row["on_sale"] = True
        if alternatives is not None and len(alternatives) > 1:
            row["alternatives"] = [c.summary() for c in alternatives[1:4]]
        return row

    @staticmethod
    def _form_note(need: AggregatedNeed, product: Product,
                   verdict: Verdict) -> str | None:
        """Say so when the product is the right thing in the wrong state.

        This is the failure that hides: a menu asking for 20 g of fresh grated
        ginger bought two 10 g jars of ground ginger, which is roughly six
        times the ginger and the wrong ingredient. Silently correct-looking
        arithmetic on the wrong good is worse than a visible substitution.
        """
        if not verdict.form_mismatch:
            return None
        forms = ", ".join(sorted(verdict.unwanted)) or "a different form"
        note = f"{forms} - the request named no form"
        if "tørket" in verdict.unwanted and need.group == "mass":
            grams = need.quantity * 1000
            dried = dried_equivalent(need.name, grams)
            if dried:
                note += (
                    f"; {grams:.0f} g fresh is roughly {dried:.0f} g dried, so "
                    "one pack is ordered rather than the fresh weight"
                )
            else:
                note += "; one pack ordered, not the fresh weight"
        return note

    @staticmethod
    def _packs_required(product: Product, need: AggregatedNeed, *,
                        form_mismatch: bool = False) -> float:
        """How many packs to cover the need, rounded up.

        Must be given a product built from the FULL record. Search hits carry
        neither ``measurementValue`` nor ``packageSize``, so ``pack_quantity``
        falls back to ``weight`` - a placeholder 1.0 on loose produce, which
        silently turns "800 g poteter" into one potato.

        ``form_mismatch`` short-circuits the arithmetic, because when the form
        differs the quantity is no longer the same question. A menu asking for
        20 g of fresh ginger and 10 g of fresh parsley was served ground ginger
        and dried parsley, and rounding up to the *fresh* weight bought two
        jars of one and three of the other - 90 kr of dried parsley for a
        garnish. One pack, flagged, is the only defensible answer: the
        conversion factor is ingredient-specific and the substitution is
        something the user should see rather than have quietly costed.

        Goods sold by weight skip pack arithmetic entirely. You order the
        amount you want, stepped by ``min_quantity``, and the calculator
        accepts it: 0.4 of the counter's cod fillet prices at 171.60 kr,
        verified live. Rounding that need up to one nominal "2KG pack" - which
        ``quantise`` then clamps to 1 kg - charged 429 kr for 400 g of fish.
        """
        if form_mismatch:
            return product.quantise(1.0)
        if (product.product_sold_by_weight
                and need.group in ("mass", "volume")
                and product.unit_group == need.group):
            return product.quantise(need.quantity)
        if need.group not in ("mass", "volume"):
            # A count need. Buying N of a 2-pack is 2N peppers, and N of a 1 kg
            # bag is N kilos, so the pack's piece count decides this.
            pieces = product.pieces_per_pack
            if pieces and pieces > 0:
                packs = math.ceil((need.quantity / pieces) - 1e-9)
            else:
                # A bag or net of unknown count. One covers a handful of items;
                # buying `need` of them would be absurd.
                packs = 1
            return product.quantise(max(1.0, float(packs)))
        pack = product.pack_constraint
        if not pack or pack <= 0:
            return product.quantise(1.0)
        packs = math.ceil((need.quantity / pack) - 1e-9)
        return product.quantise(max(1.0, float(packs)))

    # ------------------------------------------------------------- full flow
    async def plan(
        self,
        meals: Sequence[Meal],
        *,
        prefs: Preferences | None = None,
        pantry: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        scale: float = 1.0,
        store_id: str | None = None,
        include_alternatives: bool = False,
    ) -> dict[str, Any]:
        """Menu -> aggregated needs -> products -> priced cart.

        The priced cart is returned WITHOUT its item list: ``shopping_list``
        already names every product, with more context, so a second copy is
        pure duplication.

        For the same reason there is no separate ``basket_items``. It used to
        restate ``ean`` and ``packs`` from every row - a third copy of the same
        facts, and the largest single block of redundancy in the response.
        ``price_cart`` and ``build_basket_script`` read ``packs`` directly, so
        ``shopping_list`` can be handed to either of them as it stands.
        """
        exclude = list(exclude or [])
        if exclude:
            prefs = prefs or Preferences()
            prefs.exclude_words = set(prefs.exclude_words) | {
                e.lower() for e in exclude}
        needs, skipped, excluded = self.aggregate(
            meals, pantry=pantry, exclude=exclude, scale=scale)
        matches, unmatched = await self.resolve_needs(
            needs, prefs=prefs, store_id=store_id,
            include_alternatives=include_alternatives,
        )

        requests = [CartItemRequest.from_dict(m) for m in matches]
        priced = await self.cart.build_and_price(
            requests, store_id=store_id, include_items=False
        )

        result: dict[str, Any] = {
            "shopping_list": matches,
            "totals": priced.get("totals", {}),
        }
        for key, value in (
            ("unmatched", unmatched),
            ("pantry_skipped", skipped),
            ("excluded", excluded),
            ("problems", priced.get("problems")),
            ("upsell_hints", priced.get("upsell_hints")),
            ("adjusted_quantities", priced.get("adjusted_quantities")),
        ):
            if value:
                result[key] = value
        return result


def parse_menu(raw: dict[str, Any]) -> list[Meal]:
    """Build meals from a plain dict.

    Accepts::

        {"Mandag": {"name": "Taco", "ingredients": ["400 g kjøttdeig", ...]},
         "Tirsdag": ["2 stk laksefilet", "500 g poteter"]}
    """
    meals: list[Meal] = []
    for day, value in raw.items():
        if isinstance(value, dict):
            name = value.get("name") or day
            items = value.get("ingredients") or []
            servings = int(value.get("servings") or 4)
        else:
            name, items, servings = day, list(value), 4
        meals.append(Meal(
            name=name, day=day, servings=servings,
            ingredients=[
                Ingredient.parse(i, day=day, meal=name) if isinstance(i, str)
                else Ingredient(**i)
                for i in items
            ],
        ))
    return meals


menu_service = MenuService()
