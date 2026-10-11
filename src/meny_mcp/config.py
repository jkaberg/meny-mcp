"""Static configuration for the NorgesGruppen "FWC" commerce platform.

Every value here was verified against a live meny.no session on 2026-08-15 by
reading ``window.ENV`` and intercepting the site's own ``fetch`` calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Hosts
# --------------------------------------------------------------------------
# From window.ENV.NEXT_PUBLIC_PLATFORM_REST_API_BASE_URL
PLATFORM_BASE = "https://platform-rest-prod.ngdata.no"

# From window.ENV.NEXT_PUBLIC_STORE_API_URL (store finder / "Sylinder")
STORE_API_BASE = "https://api.ngdata.no"

# Product imagery. imagePath on a product is "<gtin>/<chain>".
IMAGE_BASE = "https://res.cloudinary.com/norgesgruppen/image/upload"


# --------------------------------------------------------------------------
# Chains
# --------------------------------------------------------------------------
# All NorgesGruppen chains share this backend; only the chain id changes.
CHAINS: dict[str, str] = {
    "meny": "1300",
    "spar": "1210",
    "joker": "1220",
}

DEFAULT_CHAIN = "meny"

# A store id is a GLN. 7080001150488 is the store bound to the session that was
# reverse-engineered; it is only a fallback. Resolve the user's real store with
# the `find_stores` tool and set MENY_STORE_ID.
FALLBACK_STORE_ID = "7080001150488"


# --------------------------------------------------------------------------
# Cart / calculator query parameters
# --------------------------------------------------------------------------
# Observed on the live PUT /api/calculator/{chain}/{store} request.
# handoverType: BUTIKK = collect in store. Home delivery uses a different value
# which is not yet confirmed - treat BUTIKK as the safe default, since the
# handover choice is made by the user at checkout anyway.
DEFAULT_BAG_TYPE = "PLAST"
DEFAULT_HANDOVER_TYPE = "BUTIKK"
DEFAULT_EDIT_MODE = "false"


@dataclass(frozen=True)
class Settings:
    """Runtime settings, overridable by environment variables."""

    chain: str = os.getenv("MENY_CHAIN") or DEFAULT_CHAIN
    store_id: str = os.getenv("MENY_STORE_ID") or FALLBACK_STORE_ID
    # Whether a store was actually configured, as opposed to falling back. The
    # difference matters: an unconfigured server answers with a stranger's shop
    # rather than admitting it does not know which one to use.
    store_configured: bool = bool(os.getenv("MENY_STORE_ID", "").strip())

    # Networking
    timeout: float = float(os.getenv("MENY_TIMEOUT", "20"))
    # Four rather than three: the platform drops the occasional connection
    # under a menu's burst of lookups, and a whole plan being abandoned over
    # one transient failure costs far more than one more retry.
    max_retries: int = int(os.getenv("MENY_MAX_RETRIES", "4"))
    # Be a good citizen: this is an undocumented API belonging to someone else.
    max_concurrency: int = int(os.getenv("MENY_MAX_CONCURRENCY", "4"))
    min_request_interval: float = float(os.getenv("MENY_MIN_INTERVAL", "0.12"))

    # Caching (seconds). Product data changes at most daily; prices, less often
    # than you would think. Keep the cache short enough to stay honest.
    cache_ttl_product: int = int(os.getenv("MENY_CACHE_TTL_PRODUCT", "900"))
    cache_ttl_search: int = int(os.getenv("MENY_CACHE_TTL_SEARCH", "300"))
    cache_ttl_store: int = int(os.getenv("MENY_CACHE_TTL_STORE", "86400"))

    # Search
    default_page_size: int = int(os.getenv("MENY_PAGE_SIZE", "40"))
    max_page_size: int = 500  # verified server-side ceiling
    # How far to over-fetch when filtering out unavailable products. Each hit
    # is ~2.4 kB, so this factor is the main lever on network traffic.
    overfetch_factor: float = float(os.getenv("MENY_OVERFETCH", "1.5"))
    max_overfetch: int = int(os.getenv("MENY_MAX_OVERFETCH", "60"))

    # Mealie, for the weekly-menu tools. Optional: when these are unset the
    # ukemeny toolset does not register at all, so a server configured only
    # for groceries advertises only grocery tools.
    mealie_url: str = os.getenv("MEALIE_URL", "").rstrip("/")
    mealie_token: str = (os.getenv("MEALIE_TOKEN")
                         or os.getenv("MEALIE_API_TOKEN", ""))
    # Where the week manifest and the resolved id maps live. One run spans
    # many tool calls and may span a restart, so this is on disk rather than
    # in session memory.
    state_dir: str = os.getenv(
        "MENY_STATE_DIR",
        os.path.join(os.path.expanduser("~"), ".local", "share", "meny-mcp"))

    @property
    def mealie_configured(self) -> bool:
        return bool(self.mealie_url and self.mealie_token)

    @property
    def chain_id(self) -> str:
        return CHAINS.get(self.chain.lower(), CHAINS[DEFAULT_CHAIN])

    @property
    def headers(self) -> dict[str, str]:
        """Headers the site itself sends.

        No cookie, no bearer token and no CSRF token are required - this was
        verified by replaying every endpoint from a clean context. The `fwc-*`
        headers are what the frontend sends; `fwc-chain-id` is the meaningful
        one. A descriptive User-Agent is included deliberately so this traffic
        is identifiable rather than pretending to be a browser.
        """
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "fwc-chain-id": self.chain_id,
            "fwc-browser-platform": "Win32",
            "fwc-using-api-key": "false",
            "fwc-using-bearer-token": "false",
            "user-agent": os.getenv(
                "MENY_USER_AGENT",
                "meny-mcp/0.1 (personal meal-planning assistant)",
            ),
        }


settings = Settings()


# --------------------------------------------------------------------------
# Endpoint templates
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Endpoints:
    """Verified endpoint templates. Format with chain_id / store_id / ean."""

    autosuggest: str = "/api/episearch/{chain_id}/autosuggest"
    product: str = "/api/products/{chain_id}/{store_id}/{ean}"
    # The only endpoint that lists stores WITH their GLNs. Nominally the
    # click-and-collect options, but its BUTIKK locations are exactly the
    # stores the basket's own store picker offers, which is what makes it the
    # right list to choose from. `/api/stores/{chain_id}` is a 404; the store
    # search index and the /butikker page both return CMS ids, not GLNs.
    handover_options: str = "/api/handoveroptions/{chain_id}"
    store: str = "/api/stores/{chain_id}/{store_id}"
    pickup_fees: str = "/api/stores/{chain_id}/{store_id}/pickupfees"
    calculator: str = "/api/calculator/{chain_id}/{store_id}"

    def url(self, template: str, **kw: str) -> str:
        return PLATFORM_BASE + template.format(**kw)


endpoints = Endpoints()


# --------------------------------------------------------------------------
# Availability semantics
# --------------------------------------------------------------------------
# A product is considered buyable only if ALL of these hold. Derived from the
# product schema returned by /api/products/{chain}/{store}/{ean}.
UNAVAILABLE_FLAGS: tuple[str, ...] = (
    "isOutOfStock",      # True  -> out of stock
    "isRevoked",         # True  -> withdrawn from sale
)
REQUIRED_TRUE_FLAGS: tuple[str, ...] = (
    "isForSale",         # False -> not sellable at all
)

# Comparable-unit normalisation.
#
# Two distinct fields carry this information and they are NOT interchangeable:
#   * `comparableUnitType` - a Norwegian label ("Kilo", "Liter"). Present only
#     on the full product endpoint; ALWAYS ABSENT from search hits.
#   * `compareUnit`        - a short code ("kg", "l"). Present on both.
# So `compareUnit` is the reliable one; the label map is a refinement.
COMPARABLE_UNIT_MAP: dict[str, str] = {
    "Kilo": "kg",
    "Kilogram": "kg",
    "Liter": "l",
    "Stykk": "stk",
    "Porsjon": "porsjon",
    "Vask": "vask",
    "Behandling": "behandling",
    "Meter": "meter",
    "Tablett": "tablett",
}

# Units that describe fundamentally different things must never be compared.
# Every value below was observed in live search results across 18 queries;
# counts in that sample: kg 945, l 617, stk 152, vask 67, porsjon 39,
# "100 meter" 31, tablett 12, meter 4, behandling 3.
#
# "100 meter" deliberately gets its own group. It is the same dimension as
# "meter" but a different scale, and the API does not normalise between them -
# ranking 12 kr/100 meter against 3 kr/meter would invert the true answer.
# Isolating it means such products are simply never cross-compared.
COMPARABLE_UNIT_GROUPS: dict[str, str] = {
    "kg": "mass",
    "l": "volume",
    "stk": "count",
    "porsjon": "portion",
    "vask": "wash",
    "behandling": "treatment",
    "tablett": "tablet",
    "meter": "length_m",
    "100 meter": "length_100m",
}


@dataclass
class ChainProfile:
    """Convenience bundle passed around the codebase."""

    chain: str
    chain_id: str
    store_id: str
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def current(cls) -> "ChainProfile":
        return cls(
            chain=settings.chain,
            chain_id=settings.chain_id,
            store_id=settings.store_id,
            headers=settings.headers,
        )
