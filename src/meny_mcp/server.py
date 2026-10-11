"""MCP server for meny.no.

Tool surface, grouped by what it is for:

  discovery   search_products, compare_prices, find_best_value,
              check_availability, get_product_details, find_stores
  planning    plan_week_menu
  basket      price_cart, build_basket_script, clear_basket_script

Design notes
------------
* Tools return compact dicts, not raw API records. One product record is 96
  fields / ~8.4 kB, and a 40-result search would drown the context window.
  Keys whose value is null or false are dropped rather than sent.
* Tools that a menu drives are BATCHED (``find_best_value`` takes a list of
  queries). Twenty-five ingredients as twenty-five tool calls costs twenty-five
  round trips and twenty-five copies of the surrounding protocol chatter.
* Nothing here mutates the user's basket. ``build_basket_script`` returns
  JavaScript; something with browser access must run it. This is not caution
  for its own sake - the anonymous basket genuinely lives in the browser's
  localStorage and there is no server-side cart to write to.
* Read-only tools are annotated ``readOnlyHint``/``idempotentHint`` so clients
  can reason about which calls are safe to retry.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, create_model

if TYPE_CHECKING:
    from mcp.server.transport_security import TransportSecuritySettings

from . import auth
from .cart import CartItemRequest, cart_service
from .config import settings
from .client import MenyAPIError, client
from . import store as store_module
from .handoff import (
    build_bookmarklet,
    build_clear_script,
    build_injector,
    build_store_probe,
    build_verify_script,
    instructions,
)
from .matching import query_variants
from .menu import menu_service, parse_menu
from . import profile as profile_module
from .pricing import Preferences, compare_table, rank
from .search import search_service
from .ukemeny import tools as ukemeny_tools

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("meny_mcp.server")

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True,
                            openWorldHint=True)

server = MCPServer(
    name="meny",
    title="MENY grocery shopping",
    version="0.1.0",
    instructions=(
        "Search groceries at meny.no (Norway), compare price per measured "
        "unit, plan week menus and build a basket.\n\n"
        "- Prices are NOK; search in Norwegian ('kyllingfilet', not 'chicken "
        "breast').\n"
        "- Settle the store FIRST (find_stores, set_store). Every price "
        "depends on it, and every response names the store it used.\n"
        "- Batch: find_best_value and get_product_details take LISTS. Never "
        "loop one call per ingredient.\n"
        "- For a whole menu use plan_week_menu - it aggregates, picks and "
        "prices in one call. Its shopping_list feeds price_cart and "
        "build_basket_script directly.\n"
        "- Only compare unit prices within one unit_group.\n"
        "- Relay 'substitution' (right ingredient, other form - the quantity "
        "is NOT equivalent), 'matched_as' (found under another spelling), "
        "'matched_by' (found via the shop's synonyms, so the title will not "
        "name the ingredient) and 'rejected' (closer matches thrown out).\n"
        "- Standing household preferences go in set_household, not into every "
        "menu.\n"
        "- The basket lives in the browser. build_basket_script returns async "
        "JavaScript that something must run on meny.no and await.\n"
        "- Quote total_to_pay from price_cart, never your own sum: it includes "
        "fees and a weight buffer."
    ),
)


def _prefs(
    prefer_organic: bool = False,
    prefer_norwegian: bool = False,
    prefer_offers: bool = True,
    exclude_brands: list[str] | None = None,
    allow_alcohol: bool = False,
    exclude: set[str] | None = None,
    accept_forms: list[str] | None = None,
) -> Preferences:
    return Preferences(
        prefer_organic=prefer_organic,
        prefer_norwegian=prefer_norwegian,
        prefer_offers=prefer_offers,
        exclude_brands=set(exclude_brands or []),
        exclude_alcohol=not allow_alcohol,
        max_age_limit=18 if allow_alcohol else 0,
        exclude_words=set(exclude or ()),
        accept_forms={f.strip().lower() for f in (accept_forms or []) if f},
    )


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, MenyAPIError):
        return {"error": exc.message, "status": exc.status}
    return {"error": str(exc)}


def _store_summary(gln: str, info: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in {
        "store_id": gln,
        "name": info.get("name"),
        "address": info.get("address"),
        "postal_code": info.get("postalCode") or info.get("postnumber"),
        "city": info.get("city") or info.get("place"),
    }.items() if v is not None}


# Free-text fallback, used when the store list cannot be fetched. Docstrings
# are omitted from these models deliberately: the client renders them as the
# dialog's description, so implementation notes do not belong in them.
class StoreChoice(BaseModel):
    store_id: str = Field(
        description=(
            "Your MENY store's 13-digit id (GLN), e.g. 7080001271947. "
            "Find it by running the which_store_script tool's JavaScript on "
            "meny.no."
        )
    )


def _store_label(store: dict[str, Any]) -> str:
    place = store.get("city") or store.get("municipality") or ""
    address = store.get("address") or ""
    tail = ", ".join(x for x in (address, place) if x)
    return f"{store.get('name') or 'MENY'} - {tail} [{store['store_id']}]"


async def _pick_store(ctx: Context) -> str | None:
    """Ask the user which store to use, offering the real list as choices."""
    try:
        stores = await search_service.list_stores()
    except Exception as exc:  # noqa: BLE001
        log.warning("store list unavailable: %s", exc)
        stores = []

    message = ("Which MENY store should I use? Prices, stock and assortment "
               "differ per store.")

    if stores:
        stores = sorted(stores, key=lambda s: (s.get("county") or "",
                                               s.get("name") or ""))
        labels = [_store_label(s) for s in stores]
        by_label = dict(zip(labels, stores))
        # An enum field renders as a picker. Built per call because the list is
        # fetched, not compile-time known; the response is cached for a day.
        schema = create_model(
            "StorePick",
            store=(Literal[tuple(labels)],  # type: ignore[valid-type]
                   Field(description="Pick the store you shop at")),
        )
        result = await ctx.elicit(message, schema)
        if result.action == "accept" and result.data:
            chosen = by_label.get(getattr(result.data, "store", ""))
            if chosen:
                return str(chosen["store_id"])
        return None

    result = await ctx.elicit(message, StoreChoice)
    if result.action == "accept" and result.data:
        return store_module.normalise(result.data.store_id)
    return None


@dataclass
class StoreRef:
    """The shop a call was answered against, carried into its response.

    Every price, every stock flag and part of the assortment depends on this,
    and a wrong store produces answers that look completely fine. The failure
    mode worth designing against is not "the server guessed wrong" but "nobody
    ever checked": a whole week can be planned against a default without the
    subject ever coming up, and it only surfaces at the till. So the store's
    name travels with every result rather than being available on request.
    """

    store_id: str
    source: str = "fallback"
    name: str | None = None
    city: str | None = None
    warning: str | None = None

    @property
    def block(self) -> dict[str, Any]:
        return {k: v for k, v in {
            "store_id": self.store_id,
            "name": self.name,
            "city": self.city,
            "source": self.source,
        }.items() if v is not None}

    def apply(self, out: dict[str, Any]) -> dict[str, Any]:
        """Stamp the store onto a tool result."""
        if isinstance(out, dict) and "error" not in out:
            out["store"] = self.block
            if self.warning:
                out["warning"] = self.warning
        return out

    @property
    def refusal(self) -> dict[str, Any] | None:
        """The error to return instead of an answer, under MENY_REQUIRE_STORE."""
        if self.source != "fallback" or not _require_store():
            return None
        return {
            "error": "no store chosen",
            "hint": ("MENY_REQUIRE_STORE is set, so this server will not price "
                     "against a default. Call find_stores, then set_store."),
        }


def _require_store() -> bool:
    return os.getenv("MENY_REQUIRE_STORE", "").lower() in ("1", "true", "yes")


async def _store_name(store_id: str) -> tuple[str | None, str | None]:
    """``(name, city)`` for a GLN, from whichever cached list has it.

    Never raises and never blocks an answer: an unnamed store is worth less
    than a named one, but far more than a failed tool call.
    """
    try:
        for entry in await search_service.list_stores():
            if str(entry.get("store_id")) == store_id:
                return entry.get("name"), entry.get("city")
        info = await search_service.get_store(store_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("store name lookup failed for %s: %s", store_id, exc)
        return None, None
    if isinstance(info, dict):
        return (info.get("name"),
                info.get("city") or info.get("place"))
    return None, None


async def _store_for(ctx: Context, store_id: str | None) -> StoreRef:
    """Resolve the store for a call, asking the user when nothing is set.

    The prompt only fires when the server would otherwise answer from the
    hard-coded fallback GLN - a real shop, but not the caller's, which produces
    plausible and wrong prices. Clients that do not support elicitation just get
    the warning.
    """
    resolved, source = store_module.resolve(ctx, store_id)

    if source == "fallback":
        caps = getattr(ctx, "client_capabilities", None)
        if caps is not None and getattr(caps, "elicitation", None) is not None:
            try:
                picked = await _pick_store(ctx)
            except Exception as exc:  # noqa: BLE001 - never fail a tool over this
                log.warning("elicitation failed: %s", exc)
            else:
                if picked:
                    store_module.remember(ctx, picked)
                    resolved, source = picked, "session"

    name, city = await _store_name(resolved)
    warning = None
    if source == "fallback":
        warning = (
            f"No store chosen - using the fallback GLN {resolved} "
            f"({name or 'unknown shop'}), which is probably not yours. Prices "
            "and stock may be wrong. Call find_stores then set_store, or run "
            "which_store_script on meny.no."
        )
    return StoreRef(store_id=resolved, source=source, name=name, city=city,
                    warning=warning)


def _allergens(raw: Any) -> list[str] | None:
    """Only the allergens a product actually contains.

    The API lists every allergen it has been screened for, almost all with
    ``code: "FRI"`` (free of). Echoing 14 negatives to say "contains nothing"
    is the bulk of the payload and none of the information. Entries may also
    arrive as bare strings, which are always positives.
    """
    if not isinstance(raw, list):
        return None
    found = [
        entry if isinstance(entry, str)
        else (entry.get("displayName") or entry.get("name"))
        for entry in raw
        if isinstance(entry, str) or str(entry.get("code", "")).upper() != "FRI"
    ]
    return [f for f in found if f] or None


def _nutrition(raw: Any) -> dict[str, str] | None:
    """Flatten the nutrition array into ``{"Kalorier": "32 kcal"}``."""
    if not isinstance(raw, list):
        return raw or None
    out: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        label = entry.get("displayName") or entry.get("name")
        amount = entry.get("amount")
        if label is None or amount is None:
            continue
        out[str(label)] = f"{amount} {entry.get('unit') or ''}".strip()
    return out or None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@server.tool(
    description=(
        "Search grocery products at meny.no. Query in Norwegian. Returns "
        "compact summaries: ean, title, price, price per measured unit. "
        "To pick the cheapest option for an ingredient, use find_best_value "
        "instead - it ranks and batches. Set verbose for pack size, category "
        "and product URL."
    ),
    annotations=READ_ONLY,
)
async def search_products(
    query: str,
    ctx: Context,
    limit: int = 10,
    available_only: bool = True,
    verbose: bool = False,
    store_id: str | None = None,
) -> dict[str, Any]:
    ref = await _store_for(ctx, store_id)
    if ref.refusal:
        return ref.refusal
    try:
        products = await search_service.search(
            query, limit=limit, available_only=available_only,
            store_id=ref.store_id,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    return ref.apply({
        "query": query,
        "count": len(products),
        "results": [p.summary(verbose=verbose) for p in products],
    })


@server.tool(
    description=(
        "Compare candidate products for a query by price per measured unit "
        "(kr/kg, kr/l...). Rows are grouped by unit_group; only compare rows "
        "sharing a unit_group. Includes a vs_cheapest_pct premium column."
    ),
    annotations=READ_ONLY,
)
async def compare_prices(
    query: str,
    ctx: Context,
    limit: int = 12,
    store_id: str | None = None,
) -> dict[str, Any]:
    ref = await _store_for(ctx, store_id)
    if ref.refusal:
        return ref.refusal
    try:
        products = await search_service.search(
            query, limit=limit * 2, available_only=True, store_id=ref.store_id
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    rows = compare_table(products, limit=limit)
    groups = sorted({r["unit_group"] for r in rows})
    return ref.apply({
        "query": query,
        "unit_groups_present": groups,
        "note": (
            "Multiple unit groups present - rows are only comparable within "
            "the same unit_group." if len(groups) > 1 else None
        ),
        "rows": rows,
    })


@server.tool(
    description=(
        "Pick the best-value available product for each ingredient. Pass ALL "
        "ingredients in one call - they resolve concurrently. Filters "
        "out-of-stock, alcohol and age-restricted items; refuses products that "
        "merely share a word with the request (an oil, a ready meal, the wrong "
        "animal); retries other Norwegian spellings, then the shop's own "
        "synonyms, before giving up - which is what resolves a category word "
        "like 'biff' to the cuts it means. A 'note' "
        "means the right ingredient in another form - read it before accepting "
        "the quantity. needed_quantity, keyed by query string in the product's "
        "own unit (e.g. {'kyllingfilet': 0.6}), switches ranking to the kroner "
        "needed to cover it rather than price per unit. exclude blocks an "
        "ingredient outright, including inside a product's ingredient list; "
        "accept_forms allows an unasked-for form, e.g. ['frossen']."
    ),
    annotations=READ_ONLY,
)
async def find_best_value(
    queries: list[str],
    ctx: Context,
    needed_quantity: dict[str, float] | None = None,
    prefer_organic: bool = False,
    prefer_norwegian: bool = False,
    exclude_brands: list[str] | None = None,
    exclude: list[str] | None = None,
    accept_forms: list[str] | None = None,
    include_alternatives: bool = False,
    store_id: str | None = None,
) -> dict[str, Any]:
    queries = list(dict.fromkeys(q for q in queries if q and q.strip()))
    if not queries:
        return {"error": "no queries given"}
    ref = await _store_for(ctx, store_id)
    if ref.refusal:
        return ref.refusal
    banned = profile_module.exclusions_for(ctx, exclude)
    try:
        found = await search_service.search_many(
            queries, limit=12, available_only=True, store_id=ref.store_id
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    prefs = _prefs(prefer_organic, prefer_norwegian,
                   exclude_brands=exclude_brands, exclude=banned,
                   accept_forms=profile_module.accept_forms_for(
                       ctx, accept_forms))

    def best_for(query: str, term: str) -> list:
        return rank(found.get(term) or [], term, prefs,
                    needed_quantity=(needed_quantity or {}).get(query))

    ranked_by_query = {q: (best_for(q, q), q) for q in queries}

    # Retry the misses under other spellings. Only the failures pay for this,
    # and it is what turns "sjampinjong: no acceptable candidate" back into the
    # champignons that were on the shelf all along.
    retries = {q: query_variants(q) for q, (r, _) in ranked_by_query.items()
               if not r}
    retries = {q: v for q, v in retries.items() if v}
    if retries:
        terms = list(dict.fromkeys(t for v in retries.values() for t in v))
        try:
            found.update(await search_service.search_many(
                terms, limit=12, available_only=True, store_id=ref.store_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("spelling retry failed: %s", exc)
        for query, variants in retries.items():
            for term in variants:
                ranked = best_for(query, term)
                if ranked:
                    ranked_by_query[query] = (ranked, term)
                    break

    # Last resort: re-judge what the original search already returned, as full
    # records. That is where the shop's synonyms are, and they are the only
    # thing that connects a category word like "biff" to the eight cuts it
    # means - none of which is called a biff.
    still_stuck = [q for q, (ranked, _) in ranked_by_query.items() if not ranked]
    if still_stuck:
        hydrated = await asyncio.gather(*(
            search_service.hydrate(found.get(q) or [], store_id=ref.store_id)
            for q in still_stuck
        ), return_exceptions=True)
        for query, products in zip(still_stuck, hydrated):
            if isinstance(products, BaseException):
                log.warning("synonym retry failed for %r: %s", query, products)
                continue
            ranked = rank(products, query, prefs,
                          needed_quantity=(needed_quantity or {}).get(query))
            if ranked:
                ranked_by_query[query] = (ranked, query)

    picks: list[dict[str, Any]] = []
    for query in queries:
        ranked, term = ranked_by_query[query]
        if not ranked:
            picks.append({
                "query": query, "best": None,
                "reason": "no results" if not found.get(query)
                          else "no acceptable candidate",
                "tried": [query, *query_variants(query)],
            })
            continue
        entry: dict[str, Any] = {"query": query, "best": ranked[0].summary()}
        if term != query:
            entry["matched_as"] = term
        if ranked[0].via_synonym:
            entry["matched_by"] = "shop synonym"
        if include_alternatives:
            entry["runners_up"] = [c.summary() for c in ranked[1:4]]
        picks.append(entry)
    return ref.apply({"picks": picks})


@server.tool(
    description=(
        "Check whether specific EANs are available at a store. Reports the "
        "reason when not (out of stock, withdrawn, supplier order required)."
    ),
    annotations=READ_ONLY,
)
async def check_availability(
    eans: list[str],
    ctx: Context,
    store_id: str | None = None,
) -> dict[str, Any]:
    from .cart import _unavailable_reason
    ref = await _store_for(ctx, store_id)
    if ref.refusal:
        return ref.refusal
    try:
        fetched = await search_service.get_products(eans,
                                                    store_id=ref.store_id)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    out = []
    for ean in eans:
        entry = fetched.get(ean)
        if entry is None:
            out.append({"ean": ean, "available": False,
                        "reason": "not found at this store"})
            continue
        product, _ = entry
        row: dict[str, Any] = {
            "ean": ean,
            "title": product.title,
            "available": product.available,
            "price": product.effective_price,
        }
        if not product.available:
            row["reason"] = _unavailable_reason(product)
        out.append(row)
    return ref.apply({"products": out})


# Optional detail blocks, off unless asked for by name. The default set answers
# the question this tool is actually used for - "what is in this, and may we
# buy it" - and nothing else. Measured over a real week's 29 products, the full
# record costs ~6 000 tokens, of which nutrition is 34%, product URLs 12% and
# marketing copy 10%, while the ingredient list everyone came for is 4%.
_DETAIL_BLOCKS: frozenset[str] = frozenset({
    "nutrition", "description", "url", "origin", "storage", "promotions",
})


@server.tool(
    description=(
        "Full details for one or more products by EAN. Returns ingredient "
        "list, allergens actually present, pack size, price and quantity "
        "rules - the fields you need to vet what is in a product before "
        "buying it. Pass every EAN you care about in one call. Bulky extras "
        "are opt-in via fields: nutrition, description, url, origin, storage, "
        "promotions, or 'all'."
    ),
    annotations=READ_ONLY,
)
async def get_product_details(
    eans: list[str],
    ctx: Context,
    fields: list[str] | None = None,
    store_id: str | None = None,
) -> dict[str, Any]:
    eans = list(dict.fromkeys(str(e) for e in eans if e))
    if not eans:
        return {"error": "no eans given"}
    wanted = {f.strip().lower() for f in (fields or []) if f and f.strip()}
    if "all" in wanted:
        wanted = set(_DETAIL_BLOCKS)
    unknown = sorted(wanted - _DETAIL_BLOCKS)
    wanted &= _DETAIL_BLOCKS
    ref = await _store_for(ctx, store_id)
    if ref.refusal:
        return ref.refusal
    errors: dict[str, str] = {}
    try:
        fetched = await search_service.get_products(
            eans, store_id=ref.store_id, errors=errors
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    products = []
    for ean in eans:
        entry = fetched.get(ean)
        if entry is None:
            products.append({"ean": ean,
                             "error": errors.get(ean, "not found")})
            continue
        product, _raw = entry
        # The verbose summary adds unit_group, package_size, category and the
        # product URL. All four restate something already in the row - the
        # unit, the pack quantity, the title - or are for a human to click, so
        # the URL brings the rest of that block with it rather than the block
        # being the default.
        data = product.summary(verbose="url" in wanted)
        extra: dict[str, Any] = {
            "pack_quantity": product.pack_quantity,
            "ingredients": product.ingredients,
            "allergens_contains": _allergens(
                product.allergens_list or product.allergens
            ),
            "contains_alcohol": product.contains_alcohol or None,
        }
        if "nutrition" in wanted:
            extra["nutrition_per_100"] = _nutrition(product.nutritional_content)
        if "description" in wanted:
            extra["description"] = product.description
        if "origin" in wanted:
            extra["country_of_origin"] = product.country_of_origin
        if "storage" in wanted:
            extra["storage"] = product.storage_text
        if "promotions" in wanted:
            extra["promotions"] = [
                {k: v for k, v in
                 {"name": p.name or p.promo_name, "text": p.market_text,
                  "price": p.promo_price_per_unit, "to": p.valid_to}.items()
                 if v is not None}
                for p in product.promotions
            ] or None
        data.update({k: v for k, v in extra.items() if v is not None})
        products.append(data)
    out: dict[str, Any] = {"products": products}
    if unknown:
        out["ignored_fields"] = unknown
        out["known_fields"] = sorted(_DETAIL_BLOCKS)
    return ref.apply(out)


@server.tool(
    description=(
        "Find selectable MENY stores by name, town, municipality, county or "
        "GLN. Empty query lists them all. These are the stores the meny.no "
        "basket itself can switch between; pass a store_id to set_store."
    ),
    annotations=READ_ONLY,
)
async def find_stores(query: str = "", limit: int = 15) -> dict[str, Any]:
    try:
        stores = await search_service.find_stores(query, limit=limit)
        total = len(await search_service.list_stores())
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    out: dict[str, Any] = {"matches": len(stores), "stores": stores}
    if not stores:
        out["note"] = (
            f"No match in the {total} selectable stores. Only stores offering "
            "collect-in-store are listed; if yours is missing, run "
            "which_store_script on meny.no to read its id directly."
        )
    return out


@server.tool(
    description=(
        "Set the store for the rest of this session. Accepts a GLN or a store "
        "name/town that matches exactly one store. All later calls use it "
        "unless they pass store_id explicitly. Returns the shop's name and "
        "address so the user can confirm it is the right one."
    ),
)
async def set_store(store_id: str, ctx: Context) -> dict[str, Any]:
    gln = store_module.normalise(store_id)

    if not gln:
        # Accept a name or town too, but only when it identifies one store -
        # silently picking the first "MENY Storo" out of three would set the
        # wrong shop and every later price would be quietly wrong.
        try:
            matches = await search_service.find_stores(store_id, limit=25)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        if len(matches) == 1:
            gln = str(matches[0]["store_id"])
        elif matches:
            return {"error": f"{len(matches)} stores match {store_id!r}",
                    "candidates": matches,
                    "hint": "Call set_store again with one store_id."}
        else:
            return {"error": f"no store matches {store_id!r}",
                    "hint": "Try find_stores, or run which_store_script on meny.no."}

    try:
        info = await search_service.get_store(gln)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    if not info:
        return {"error": f"no store with GLN {gln}"}
    store_module.remember(ctx, gln)
    return {
        "store": _store_summary(gln, info),
        "applies_to": "this session; pass store_id to override for one call",
    }


@server.tool(
    description=(
        "JavaScript that reports which store the user's meny.no session is "
        "actually using. Run it on meny.no and pass the GLN it finds to "
        "set_store. Use this when the store is unknown, or when basket prices "
        "disagree with what this server reported."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def which_store_script() -> dict[str, Any]:
    return {
        "script": build_store_probe(),
        "how_to_run": instructions(lean=False),
        "note": (
            "Reads only - it scans localStorage, sessionStorage and cookies "
            "for 13-digit store ids and reports what it finds. Pass the result "
            "to set_store."
        ),
    }


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
@server.tool(
    description=(
        "Turn a whole week menu into one priced shopping list. Prefer this "
        "over per-ingredient lookups: it sums repeated ingredients across "
        "days, skips pantry staples, buys the amount actually needed and "
        "prices the basket in one call. Format: {'Mandag': {'name': 'Taco', "
        "'ingredients': ['400 g kjøttdeig', '2 stk løk']}} - amounts may use "
        "g, kg, dl, l, ss, ts, stk, pk. Every pick is re-checked against its "
        "full product record: 'substitution' means the right ingredient in "
        "another form, 'matched_by' means the shop's synonyms connected them "
        "so the title will not name it, 'rejected' means closer matches were "
        "thrown out. Read them before approving. exclude drops an ingredient "
        "AND refuses "
        "products containing it; accept_forms allows an unasked-for form, "
        "e.g. ['frossen']. The returned shopping_list feeds price_cart and "
        "build_basket_script directly."
    ),
    annotations=READ_ONLY,
)
async def plan_week_menu(
    menu: dict[str, Any],
    ctx: Context,
    servings_scale: float = 1.0,
    prefer_organic: bool = False,
    prefer_norwegian: bool = False,
    extra_pantry: list[str] | None = None,
    exclude: list[str] | None = None,
    accept_forms: list[str] | None = None,
    include_alternatives: bool = False,
    store_id: str | None = None,
) -> dict[str, Any]:
    pantry = profile_module.pantry_for(ctx, extra_pantry)
    banned = profile_module.exclusions_for(ctx, exclude)
    forms = profile_module.accept_forms_for(ctx, accept_forms)
    ref = await _store_for(ctx, store_id)
    if ref.refusal:
        return ref.refusal
    try:
        meals = parse_menu(menu)
        result = await menu_service.plan(
            meals,
            prefs=_prefs(prefer_organic, prefer_norwegian, exclude=banned,
                         accept_forms=forms),
            pantry=pantry,
            exclude=banned,
            scale=servings_scale,
            store_id=ref.store_id,
            include_alternatives=include_alternatives,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    return ref.apply(result)


@server.tool(
    description=(
        "Record this household's standing preferences for the rest of the "
        "session: pantry (staples already owned, left out of shopping lists), "
        "exclude (ingredients never to buy - allergies, dislikes) and "
        "accept_forms (forms that are fine unasked, e.g. ['frossen'] for a "
        "household happy with frozen fish and vegetables). All apply "
        "automatically to plan_week_menu and find_best_value. pantry extends a "
        "built-in list of ~70 staples unless replace_pantry is set. Call with "
        "no arguments to see what is currently in effect."
    ),
)
async def set_household(
    ctx: Context,
    pantry: list[str] | None = None,
    exclude: list[str] | None = None,
    accept_forms: list[str] | None = None,
    replace_pantry: bool = False,
) -> dict[str, Any]:
    if pantry is not None or exclude is not None or accept_forms is not None:
        profile_module.remember(ctx, pantry=pantry, exclude=exclude,
                                accept_forms=accept_forms,
                                replace_pantry=replace_pantry)
    return {
        "profile": {k: v for k, v in profile_module.describe(ctx).items()
                    if v is not None},
        "applies_to": "this session; MENY_PANTRY / MENY_EXCLUDE set the default",
    }


# ---------------------------------------------------------------------------
# Basket
# ---------------------------------------------------------------------------
@server.tool(
    description=(
        "Price a basket without modifying anything. Returns the authoritative "
        "total from MENY's own calculator, including fees and the weight "
        "buffer for fresh goods, plus any unavailable items and mix-and-match "
        "upsell hints. Show this to the user before filling their basket."
    ),
    annotations=READ_ONLY,
)
async def price_cart(
    items: list[dict[str, Any]],
    ctx: Context,
    store_id: str | None = None,
    handover_type: Literal["BUTIKK", "LEVERING"] = "BUTIKK",
) -> dict[str, Any]:
    requests = [CartItemRequest.from_dict(i) for i in items]
    ref = await _store_for(ctx, store_id)
    if ref.refusal:
        return ref.refusal
    try:
        result = await cart_service.build_and_price(
            requests, store_id=ref.store_id, handover_type=handover_type
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    return ref.apply(result)


@server.tool(
    description=(
        "Build JavaScript that puts items into the user's meny.no basket. "
        "items is [{ean, quantity}] - pass shopping_list from plan_week_menu "
        "straight through. This tool does NOT modify the basket: it returns an "
        "async script that must be run on meny.no (browser automation, or "
        "pasted into the DevTools console) and awaited. Modes: merge (upsert, "
        "incoming quantity wins), append (add to existing quantities), replace "
        "(discard current basket first). Set include_script=false to check "
        "what would be added without paying for the script text."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def build_basket_script(
    items: list[dict[str, Any]],
    ctx: Context,
    mode: Literal["merge", "append", "replace"] = "merge",
    store_id: str | None = None,
    as_bookmarklet: bool = False,
    embed_products: bool = False,
    set_store_in_browser: bool = True,
    include_script: bool = True,
) -> dict[str, Any]:
    requests = [CartItemRequest.from_dict(i) for i in items]
    ref = await _store_for(ctx, store_id)
    if ref.refusal:
        return ref.refusal
    store = ref.store_id
    try:
        resolved, problems = await cart_service.resolve(
            requests, store_id=store
        )
        lines = cart_service.build_lines(resolved)
        # Serialising the full lines is only worth it when they get embedded;
        # the lean script needs nothing but ean and quantity.
        payload = (
            cart_service.to_payload(lines) if embed_products
            else [{"ean": line.ean, "quantity": line.quantity} for line in lines]
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    if not payload:
        return {"error": "nothing to add", "problems": problems}

    out: dict[str, Any] = {
        "mode": mode,
        "store_id": store,
        "item_count": len(payload),
    }
    # The caller passed these items in and already has their titles; echoing
    # the accepted ones back is a second copy of its own request. Only the
    # quantities the cart actually changed are news.
    adjusted = [
        {"ean": r.product.ean, "title": r.product.title,
         "requested": r.requested_quantity, "ordered": r.quantity}
        for r in resolved if r.adjusted
    ]
    if adjusted:
        out["adjusted_quantities"] = adjusted
    if include_script:
        out["script"] = build_injector(
            payload, mode=mode, store_id=store,
            embed_products=embed_products, set_store=set_store_in_browser,
        )
        out["how_to_run"] = instructions(mode, lean=not embed_products)
        out["note"] = (
            "Run on meny.no and await the result; it refuses other origins "
            "and refuses an encrypted (logged-in) cart, so log out or use a "
            "private window. Reload afterwards - basket total and store are "
            "both applied at hydration."
        )
    if problems:
        out["problems"] = problems
    out = ref.apply(out)
    if as_bookmarklet:
        out["bookmarklet"] = build_bookmarklet(payload, mode=mode,
                                               store_id=store)
        out["bookmarklet_warning"] = (
            "Address bars cap URL length; large baskets may be truncated. "
            "Prefer the plain script."
        )
    return out


@server.tool(
    description=(
        "Return JavaScript that reports what is in the user's basket RIGHT "
        "NOW - every line with its title, quantity and price, plus a rough "
        "total. Run this on meny.no BEFORE filling the basket. If it comes "
        "back 'not empty', show the user the lines and ask whether they "
        "should stay; their answer chooses the mode for build_basket_script "
        "('merge' keeps them, 'replace' discards them). Never decide that for "
        "them: leftovers are as likely to be a deliberate mid-week top-up as "
        "last week's forgotten list, and only they know which. Read-only - it "
        "touches nothing."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def inspect_basket_script(
    ctx: Context,
    store_id: str | None = None,
) -> dict[str, Any]:
    ref = await _store_for(ctx, store_id)
    if ref.refusal:
        return ref.refusal
    return ref.apply({
        "script": build_verify_script(ref.store_id),
        "how_to_run": (
            "Run on meny.no before building the basket. Synchronous - the "
            "console prints the result immediately."
        ),
        "read_the_result": (
            "basket.verdict is 'empty' or 'not empty'. On 'empty', fill with "
            "mode 'merge' and do not ask. On 'not empty', list "
            "basket.lines for the user and ask whether to keep or discard "
            "before filling. basket.readable false means the cart is "
            "encrypted (logged in) - the basket script cannot write to it "
            "either, so have them log out or use a private window first."
        ),
    })


@server.tool(
    description=(
        "Return JavaScript that checks what the browser ACTUALLY ended up "
        "with, and is the only proof a handoff worked. Pass the same "
        "shopping_list you gave build_basket_script as items; the result "
        "carries a per-line diff - basket.verdict is 'complete' or "
        "'incomplete', with missing and wrong_quantity naming the lines that "
        "failed. Required after build_basket_script: that script is async and "
        "reloads the page, so its own 'ok: true' is routinely lost or "
        "meaningless. This script is synchronous, so automation bridges do not "
        "swallow it. The basket diff is final immediately; the store half "
        "needs 2-3s of polling until settled is true, because the "
        "pre-hydration paint still shows the previous store."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def verify_basket_script(
    ctx: Context,
    items: list[dict[str, Any]] | None = None,
    mode: Literal["merge", "append", "replace"] = "merge",
    store_id: str | None = None,
) -> dict[str, Any]:
    ref = await _store_for(ctx, store_id)
    if ref.refusal:
        return ref.refusal
    expected = [
        {"ean": str(i["ean"]), "quantity": i.get("packs", i.get("quantity", 1))}
        for i in (items or []) if i.get("ean")
    ]
    out: dict[str, Any] = {
        "script": build_verify_script(ref.store_id, expected_items=expected,
                                      mode=mode),
        "expected_lines": len(expected),
        "mode": mode,
        "how_to_run": (
            "Run on meny.no after the basket script and its reload. It is "
            "synchronous - the console prints the result immediately. Poll it "
            "for 2-3s until settled is true before judging the store; the "
            "basket verdict is already final on the first call."
        ),
        "read_the_result": (
            "basket.verdict must be 'complete'. Anything in basket.missing or "
            "basket.wrong_quantity means the handoff did not fully land: "
            "re-run build_basket_script for those lines and verify again. "
            "Pass the SAME mode you filled with: under merge/append, "
            "basket.unexpected is the user's own pre-existing lines and is "
            "forgiven; under replace a survivor is a failure, because the "
            "clear did not happen."
        ),
    }
    if not expected:
        out["warning"] = (
            "no items passed, so this only reports a line count - it cannot "
            "tell a complete basket from a wrong one. Pass the shopping list."
        )
    return ref.apply(out)


@server.tool(
    description=(
        "Return JavaScript that empties the meny.no basket. Does not modify "
        "anything itself - the script must be run on meny.no."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def clear_basket_script() -> dict[str, Any]:
    return {
        "script": build_clear_script(),
        "how_to_run": instructions(),
        "note": "Empties items but preserves the basket envelope.",
    }


# ---------------------------------------------------------------------------
# Optional toolsets
# ---------------------------------------------------------------------------
# The weekly-menu tools need a Mealie instance. Registering them unconditionally
# would advertise five tools that can only fail, so a server configured purely
# for groceries stays purely a grocery server.
ukemeny_tools.register(server)

# ...and are then gated per request. Gating here rather than at registration
# time is deliberate: whether a caller is authorised is a property of the
# request, not of the process, so one server can serve the open grocery tools
# and the credentialed ones from the same endpoint.
server.middleware.append(
    auth.gate(ukemeny_tools.TOOL_NAMES if settings.mealie_configured else (),
              auth.configured_token()
              if not auth.token_problem(auth.configured_token()) else "")
)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    return [part.strip() for part in raw.split(",") if part.strip()]


def build_transport_security(
    host: str, port: int
) -> "TransportSecuritySettings | None":
    """DNS-rebinding protection for the HTTP transport.

    The SDK enables this by default and rejects requests whose Host/Origin is
    not allow-listed (HTTP 421). That is the correct default: without it, a web
    page the user visits could POST to a locally bound MCP server and drive its
    tools.

    Host and port are passed in rather than re-read from the environment, so
    that ``--host``/``--port`` on the command line stay consistent with the
    allow-list. Getting this wrong yields a server that rejects every request.

    ``MENY_HTTP_ALLOWED_HOSTS`` / ``MENY_HTTP_ALLOWED_ORIGINS`` extend the list
    when the server sits behind a reverse proxy or is reached by hostname.
    Setting ``MENY_HTTP_DISABLE_DNS_REBINDING_PROTECTION=1`` turns the check
    off entirely - only sensible when a trusted proxy already enforces it.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    if os.getenv("MENY_HTTP_DISABLE_DNS_REBINDING_PROTECTION", "").lower() in (
        "1", "true", "yes",
    ):
        log.warning(
            "DNS rebinding protection DISABLED - only do this behind a trusted proxy"
        )
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
            allowed_hosts=["*"],
            allowed_origins=["*"],
        )

    names = {"127.0.0.1", "localhost", "[::1]"}
    if host not in ("0.0.0.0", "::"):
        names.add(host)

    hosts: set[str] = set()
    origins: set[str] = set()
    for name in names:
        hosts.add(name)
        hosts.add(f"{name}:{port}")
        origins.add(f"http://{name}:{port}")
        origins.add(f"http://{name}")

    hosts.update(_csv_env("MENY_HTTP_ALLOWED_HOSTS"))
    origins.update(_csv_env("MENY_HTTP_ALLOWED_ORIGINS"))

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )


def serve_http(*, host: str, port: int, path: str, token: str,
               stateless: bool, json_response: bool) -> None:
    """Run the Streamable HTTP transport, with the secret path unwrapped.

    The SDK's own ``run(transport="streamable-http")`` builds this app and
    hands it to uvicorn; the only reason to do it here is to slip one ASGI
    wrapper in between, so ``/mcp/<token>`` is served by the same single mount
    as ``/mcp``.
    """
    import uvicorn

    app = server.streamable_http_app(
        streamable_http_path=path,
        json_response=json_response,
        stateless_http=stateless,
        transport_security=build_transport_security(host, port),
        host=host,
    )
    if token:
        app = auth.path_unwrapping_app(app, path, token)

    uvicorn.Server(uvicorn.Config(app, host=host, port=port,
                                  log_level="warning")).run()


def main(argv: list[str] | None = None) -> None:
    """Run the server.

    Transports:
      * ``stdio`` (default) - what Claude Desktop launches directly
      * ``http``            - Streamable HTTP, for remote or shared hosting
    """
    parser = argparse.ArgumentParser(
        prog="meny-mcp", description="MCP server for meny.no groceries"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "streamable-http", "sse"],
        default=os.getenv("MENY_TRANSPORT", "stdio"),
        help="stdio (default) or http",
    )
    parser.add_argument("--host", default=os.getenv("MENY_HTTP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("MENY_HTTP_PORT", "8000")))
    parser.add_argument("--path", default=os.getenv("MENY_HTTP_PATH", "/mcp"),
                        help="HTTP path the MCP endpoint is mounted at")
    parser.add_argument(
        "--stateless", action="store_true",
        default=os.getenv("MENY_HTTP_STATELESS", "").lower() in ("1", "true", "yes"),
        help="No per-session state; each request is independent. Use behind "
             "a load balancer or for serverless deployment.",
    )
    parser.add_argument(
        "--json-response", action="store_true",
        default=os.getenv("MENY_HTTP_JSON_RESPONSE", "").lower() in ("1", "true", "yes"),
        help="Return plain JSON instead of an SSE stream.",
    )
    args = parser.parse_args(argv)

    transport = "streamable-http" if args.transport == "http" else args.transport

    try:
        if transport == "stdio":
            server.run(transport="stdio")
        elif transport == "streamable-http":
            log.warning(
                "meny-mcp listening on http://%s:%s%s (stateless=%s)",
                args.host, args.port, args.path, args.stateless,
            )
            token = auth.configured_token()
            for line in auth.describe(token, args.path,
                                      settings.mealie_configured):
                log.warning("%s", line)
            if args.host not in ("127.0.0.1", "localhost", "::1"):
                log.warning(
                    "Binding to %s exposes this server beyond localhost. The "
                    "grocery tools are unauthenticated by design; put a proxy "
                    "in front if even those should not be public.",
                    args.host,
                )
            serve_http(
                host=args.host,
                port=args.port,
                path=args.path,
                token="" if auth.token_problem(token) else token,
                stateless=args.stateless,
                json_response=args.json_response,
            )
        else:  # sse - deprecated, kept for older clients
            server.run(transport="sse", host=args.host, port=args.port)
    finally:
        try:
            asyncio.run(client.aclose())
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
