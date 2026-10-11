"""Search and product retrieval.

The only working full-text endpoint is ``/api/episearch/{chain}/autosuggest``.
Despite the name it is a complete search: ``page_size`` accepts up to 500
(verified) and ``page`` paginates. Each hit nests the product under
``contentData._source`` with 48 fields.

The plain ``/api/products/{chain}/{query}`` route returns ``total: 0`` for text
queries, and the ``/sok`` results page is server-rendered, so autosuggest is
genuinely the right primitive here rather than a fallback.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Sequence

from .client import MenyAPIError, MenyClient, client as default_client
from .config import ChainProfile, endpoints, settings
from .models import Product

log = logging.getLogger("meny_mcp.search")

SearchType = str  # "products" | "recipes" | "stores" | "articles" | "suggest"


class SearchService:
    def __init__(self, client: MenyClient | None = None,
                 profile: ChainProfile | None = None) -> None:
        self.client = client or default_client
        self.profile = profile or ChainProfile.current()

    # ------------------------------------------------------------------ search
    async def search_raw(
        self,
        query: str,
        *,
        page_size: int | None = None,
        page: int = 0,
        types: Sequence[SearchType] = ("products",),
        popularity: bool = True,
        store_id: str | None = None,
    ) -> dict[str, Any]:
        """Call autosuggest and return the raw payload."""
        size = min(page_size or settings.default_page_size, settings.max_page_size)
        url = endpoints.url(endpoints.autosuggest, chain_id=self.profile.chain_id)
        params: dict[str, Any] = {
            "types": ",".join(types),
            "search": query,
            "page_size": size,
            "store_id": store_id or self.profile.store_id,
            "popularity": "true" if popularity else "false",
        }
        if page:
            params["page"] = page
        return await self.client.get(
            url, params=params, cache_ttl=settings.cache_ttl_search
        ) or {}

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        page: int = 0,
        available_only: bool = True,
        store_id: str | None = None,
    ) -> list[Product]:
        """Search products and return normalised, optionally filtered results.

        When filtering for availability we over-fetch, because unavailable
        items are interleaved and would otherwise silently shrink the result
        set below ``limit``.

        The over-fetch is modest and capped. A hit is ~2.4 kB on the wire, so
        the old 3x factor pulled ~290 kB per ingredient while planning a week -
        several megabytes for one menu. Measured against live results, out-of-
        stock items run well under a third of any page, so 1.5x plus a small
        floor covers the shrinkage without paying for a third page of items
        that will be discarded unread.
        """
        if available_only:
            fetch = min(int(limit * settings.overfetch_factor) + 5,
                        settings.max_overfetch)
        else:
            fetch = limit
        fetch = min(max(fetch, limit), settings.max_page_size)
        payload = await self.search_raw(
            query, page_size=fetch, page=page, store_id=store_id
        )
        products = self._parse_products(payload)
        if available_only:
            products = [p for p in products if p.available]
        return products[:limit]

    async def search_many(
        self, queries: Iterable[str], *, limit: int = 10,
        available_only: bool = True, store_id: str | None = None,
    ) -> dict[str, list[Product]]:
        """Search several terms concurrently.

        A failing term yields an empty list rather than sinking the batch -
        important when resolving a whole week's ingredient list. Duplicate
        terms are collapsed: a week menu asks for "løk" several times, and the
        client's pacing makes a redundant round trip cost real wall time.
        """
        queries = list(dict.fromkeys(queries))
        results = await asyncio.gather(
            *(self.search(q, limit=limit, available_only=available_only,
                          store_id=store_id)
              for q in queries),
            return_exceptions=True,
        )
        out: dict[str, list[Product]] = {}
        for query, result in zip(queries, results):
            if isinstance(result, BaseException):
                log.warning("search failed for %r: %s", query, result)
                out[query] = []
            else:
                out[query] = result
        return out

    async def paginate(
        self, query: str, *, max_results: int = 200,
        available_only: bool = True,
    ) -> list[Product]:
        """Walk pages until exhausted or ``max_results`` reached."""
        collected: list[Product] = []
        seen: set[str] = set()
        page = 0
        while len(collected) < max_results:
            payload = await self.search_raw(
                query, page_size=settings.max_page_size, page=page
            )
            batch = self._parse_products(payload)
            if not batch:
                break
            fresh = [p for p in batch if p.ean not in seen]
            if not fresh:
                break
            for product in fresh:
                seen.add(product.ean)
                if available_only and not product.available:
                    continue
                collected.append(product)
            if len(batch) < settings.max_page_size:
                break
            page += 1
        return collected[:max_results]

    # ----------------------------------------------------------------- product
    async def get_product(self, ean: str, *,
                          store_id: str | None = None) -> tuple[Product, dict]:
        """Fetch the full product record.

        Returns ``(model, raw)``. The raw dict is required verbatim when
        building a cart line - the calculator rejects reconstructed objects.
        """
        url = endpoints.url(
            endpoints.product,
            chain_id=self.profile.chain_id,
            store_id=store_id or self.profile.store_id,
            ean=ean,
        )
        raw = await self.client.get(url, cache_ttl=settings.cache_ttl_product)
        if not isinstance(raw, dict) or not raw.get("ean"):
            raise MenyAPIError(404, f"no product for EAN {ean}", url)
        return Product.model_validate(raw), raw

    async def get_products(
        self, eans: Iterable[str], *, store_id: str | None = None,
        errors: dict[str, str] | None = None,
    ) -> dict[str, tuple[Product, dict]]:
        """Fetch many products concurrently, skipping failures.

        Pass ``errors`` to collect why each skipped EAN was skipped. Callers
        that do not are reporting a guess: a parse failure and a genuinely
        unknown EAN are not the same problem.
        """
        eans = list(dict.fromkeys(eans))  # dedupe, preserve order
        results = await asyncio.gather(
            *(self.get_product(e, store_id=store_id) for e in eans),
            return_exceptions=True,
        )
        out: dict[str, tuple[Product, dict]] = {}
        for ean, result in zip(eans, results):
            if isinstance(result, BaseException):
                log.warning("product fetch failed for %s: %s", ean, result)
                if errors is not None:
                    errors[ean] = str(result)[:200]
                continue
            out[ean] = result
        return out

    async def hydrate(
        self, products: Sequence[Product], *, limit: int = 6,
        store_id: str | None = None,
    ) -> list[Product]:
        """Re-fetch search hits as full product records.

        A search hit has 47 fields; the product endpoint returns ~91. Three of
        the extra ones decide matches that the hit cannot: ``synonyms`` (the
        shop's own cross-references, which is the only thing that connects
        "biff" to Entrecote and Mørbrad), ``shoppingListGroupNameLeaf``, and
        the declared ``ingredients``.

        Only worth paying for when the cheap path has already failed, so this
        is called on the rescue path and capped at ``limit`` records. The
        client caches them, and anything that survives will be fetched again
        for pack arithmetic and cart building anyway, so a rescue that succeeds
        costs nothing extra downstream.

        Products whose record cannot be fetched are returned as they arrived
        rather than dropped: a hit that fails to hydrate is still a candidate,
        just not a better-informed one.
        """
        wanted = list(products)[:limit]
        if not wanted:
            return []
        fetched = await self.get_products([p.ean for p in wanted],
                                          store_id=store_id)
        return [fetched[p.ean][0] if p.ean in fetched else p for p in wanted]

    # ------------------------------------------------------------------ stores
    async def list_stores(self) -> list[dict]:
        """Every store that can be chosen, with its GLN.

        Sourced from ``/api/handoveroptions/{chain}``. That endpoint is really
        about click-and-collect, but its ``BUTIKK`` locations carry the full
        store record - crucially including ``gln``, which nothing else exposes.
        The autosuggest ``types=stores`` search returns CMS content ids
        (``72498``) that no other endpoint accepts, and the public /butikker
        page is client-rendered and contains no ids at all.
        """
        url = endpoints.url(endpoints.handover_options,
                            chain_id=self.profile.chain_id)
        payload = await self.client.get(url, cache_ttl=settings.cache_ttl_store)
        entries = payload if isinstance(payload, list) else []

        out: dict[str, dict] = {}
        for entry in entries:
            for location in entry.get("locations") or []:
                store = location.get("store") or {}
                gln = store.get("gln") or store.get("pickupGln")
                if not gln or gln in out:
                    continue
                coords = store.get("location") or {}
                out[gln] = {k: v for k, v in {
                    "store_id": str(gln),
                    "name": store.get("name"),
                    "address": store.get("address"),
                    "postal_code": store.get("postalCode"),
                    "city": store.get("city"),
                    "municipality": store.get("municipality"),
                    "county": store.get("county"),
                    "lat": coords.get("lat"),
                    "lon": coords.get("lon"),
                }.items() if v is not None}
        return list(out.values())

    async def find_stores(self, query: str = "", *, limit: int = 10) -> list[dict]:
        """Stores matching a name or place. Empty query returns the first
        ``limit`` stores."""
        stores = await self.list_stores()
        needle = (query or "").strip().lower()
        if not needle:
            return stores[:limit]
        matched = [
            s for s in stores
            if any(needle in str(s.get(field, "")).lower()
                   for field in ("name", "city", "municipality", "county",
                                 "address", "postal_code", "store_id"))
        ]
        return matched[:limit]

    async def get_store(self, store_id: str) -> dict:
        url = endpoints.url(
            endpoints.store, chain_id=self.profile.chain_id, store_id=store_id
        )
        return await self.client.get(url, cache_ttl=settings.cache_ttl_store) or {}

    # ----------------------------------------------------------------- parsing
    @staticmethod
    def _parse_products(payload: dict[str, Any]) -> list[Product]:
        """Unwrap autosuggest's nested envelope.

        Shape: ``{"products": {"hits": [{"contentData": {"_source": {...}}}]}}``
        """
        hits = ((payload.get("products") or {}).get("hits")) or []
        products: list[Product] = []
        for hit in hits:
            source = ((hit.get("contentData") or {}).get("_source")) or {}
            if not source.get("ean"):
                continue
            try:
                products.append(Product.model_validate(source))
            except Exception as exc:  # noqa: BLE001 - never let one bad row win
                log.debug("skipping unparseable hit %s: %s",
                          source.get("ean"), exc)
        return products


search_service = SearchService()
