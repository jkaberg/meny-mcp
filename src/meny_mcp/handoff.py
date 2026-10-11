"""Handoff of a built cart into the browser's basket.

Why this module exists
----------------------
The anonymous meny.no basket is NOT stored server-side. Every intercepted cart
line carried ``hasBeenSynced: false``, and ``PUT /api/calculator`` is a
stateless pricing service that persists nothing. The authoritative basket lives
in ``localStorage["CART"]`` on meny.no.

Consequently an MCP server - which runs on your machine, not in your browser -
cannot push items into the basket over HTTP. It must emit a small script that
runs in the page. Verified end to end on 2026-08-15: an injected line survived
a hard reload, was re-priced by the site, and appeared in the header total.

Envelope shape (captured live)::

    localStorage["CART"] = {
      "encrypted": false,
      "data": {
        "__id__":       "<string>",
        "__rev__":      3,
        "__removed__":  false,
        "__type__":     "",
        "__clientId__": "<uuid-36>",
        "title":        "cart",
        "items":        [ ...cart lines... ],
        "persistedId":  "<uuid-36>"
      }
    }

The injector preserves an existing envelope when present (keeping ``__id__``,
``__clientId__`` and ``persistedId`` untouched) and only creates a fresh one
when the key is absent. It bumps ``__rev__`` so the app treats the cart as
changed.

Two injector flavours
---------------------
A cart line must embed the FULL product record, and one such record is ~8.4 kB
of JSON. Embedding 25 of them makes a ~210 kB script, which then has to travel
back through the model's context - tens of thousands of tokens to add a week's
groceries.

So the default injector ships only ``{ean, quantity}`` pairs and fetches the
product records **in the browser**, from the same API the site itself uses.
Verified on 2026-08-15: ``/api/products/{chain}/{store}/{ean}`` answers a
plain cross-origin GET from ``https://meny.no`` with
``access-control-allow-origin: https://meny.no``, and sending no custom
headers keeps it a CORS "simple request" (no preflight). That drops the script
from ~210 kB to ~1 kB.

``embed_products=True`` restores the old self-contained behaviour for the case
where the browser cannot reach the API (offline demo, blocked host).

Store selection (corrected 2026-08-16)
--------------------------------------
An earlier version of this module believed the selected store lived in
``WEBSHOP_PREFERRED_PICKUP_STORE_GLN`` + ``anonymousUserGln``, and that a
reload was enough to make the app notice. **That was wrong**, and it failed
silently: the cart was injected against one store while the app kept pricing
against another. What was actually established by experiment:

* The authoritative value is ``sessionStorage["extended_user_offline_key"]``,
  a JSON blob whose ``store`` object (``gln``, ``pickupGln``, ``name``, ...)
  is what the app reads on hydration. Setting the cookie and the
  ``..._PICKUP_STORE_GLN`` key to store B while this blob still said store A
  left the app on store A. Writing this blob flips the app; verified in both
  directions.
* The two older values are **secondary**. They are kept in sync here only
  because the site itself keeps them in sync - they do not drive anything.
* There is **no SSR and no server-side session** involved. ``fetch("/")``
  returns ~281 kB of HTML containing zero occurrences of any GLN or store
  name, so there is no HttpOnly store cookie to worry about.
* The full store record can be fetched mechanically from
  ``https://platform-rest-prod.ngdata.no/api/stores/{chain}/{gln}`` - same
  host as the products API, so the same CORS-simple-request property holds.
  Field names differ from the app's shape and must be mapped:
  ``storeId`` -> ``gln``/``pickupGln`` (**number** in the API, **string** in
  app state), ``phonenumber`` -> ``phoneNumber``, ``postnumber`` /
  ``postalCode`` -> ``postalCode``.
* The app reads all of this at hydration only, so a **reload is required** and
  is therefore performed by the injector itself rather than left to the user.

Two traps worth knowing about
-----------------------------
1. **The reload race.** For roughly 2-3 s after the reload the pre-hydration
   paint still shows the *old* store. Checking once, too early, looks exactly
   like a failed write. Callers must poll (see :func:`build_verify_script`)
   until the value settles rather than taking a single screenshot.
2. **Async return values get lost.** Browser-automation bridges commonly
   return ``{}`` for an async IIFE instead of resolving the promise - and the
   reload destroys the return value anyway. Never treat "no result" as
   failure, and never treat a returned ``ok: true`` as proof. Verify by
   re-reading storage in a *separate* call afterwards.

Also note ``sessionStorage`` is **per-tab**: the handoff only affects the tab
it runs in, and a freshly opened tab starts with no store selected.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Sequence

MergeMode = Literal["replace", "merge", "append"]

CART_STORAGE_KEY = "CART"
MENY_ORIGIN = "https://meny.no"
PRODUCT_API = "https://platform-rest-prod.ngdata.no/api/products"
STORE_API = "https://platform-rest-prod.ngdata.no/api/stores"

# THE authoritative store/handover blob. Everything else below is secondary.
STORE_STATE_KEY = "extended_user_offline_key"

# Secondary copies. The site keeps these in sync with the blob above, so we do
# too - but writing only these does NOT change the store the app uses.
STORE_GLN_KEY = "WEBSHOP_PREFERRED_PICKUP_STORE_GLN"
STORE_HANDOVER_KEY = "WEBSHOP_HAS_UPDATED_STORE_AND_HANDOVER_INFO"
STORE_COOKIE = "anonymousUserGln"

# Default handover block written alongside a store switch when none exists yet.
# Mirrors what the site writes for in-store pickup with no slot reserved.
_DEFAULT_HANDOVER = {
    "handoverType": "BUTIKK",
    "windowId": None,
    "supplierOrderDeadline": None,
    "windowTags": [],
    "windowReservationId": None,
    "from": None,
    "to": None,
    "customerPickupFrom": None,
    "customerPickupTo": None,
    "deadline": None,
}

# Points the browser session at a given store. Requires await (it fetches the
# store record), so it only fits an async injector - both templates below are
# async for that reason. STORE_GLN is null when the caller wants the session
# left alone.
_SET_STORE = r"""
  // ---- point the session at a store ------------------------------------
  // extended_user_offline_key is authoritative; the GLN key and the cookie are
  // secondary copies kept in sync only because the site keeps them in sync.
  let store_before = null;
  let store_changed = false;
  try {
    const prev = JSON.parse(sessionStorage.getItem(%(state_key)s));
    store_before = (prev && prev.store && prev.store.gln) ? String(prev.store.gln) : null;
  } catch (e) {}

  if (STORE_GLN && String(store_before) !== String(STORE_GLN)) {
    try {
      const resp = await fetch(STORE_API + "/" + STORE_GLN, {credentials: "omit"});
      if (!resp.ok) {
        return JSON.stringify({ok: false,
          error: "store lookup failed: HTTP " + resp.status + " for " + STORE_GLN});
      }
      const b = await resp.json();
      if (!b || (b.storeId === undefined && b.id === undefined)) {
        return JSON.stringify({ok: false,
          error: "store lookup returned no usable record for " + STORE_GLN});
      }
      // Map the API's field names onto the app's own store shape. storeId is a
      // number here but a string in app state - stringify it.
      const sid = String(b.storeId !== undefined ? b.storeId : b.id);
      const store = {
        address: b.address,
        chainId: CHAIN_ID,
        city: b.city,
        county: b.county,
        deviationInfo: b.deviationInfo,
        deviationInfoDisplayFrom: b.deviationInfoDisplayFrom,
        deviationInfoDisplayTo: b.deviationInfoDisplayTo,
        gln: sid,
        location: b.location || {lat: b.latitude, lon: b.longitude},
        municipality: b.municipality,
        name: b.name,
        phoneNumber: b.phonenumber,
        pickupGln: sid,
        pickupPointDescription: b.pickupPointDescription,
        postalCode: b.postalCode || b.postnumber
      };

      let ext = {};
      try { ext = JSON.parse(sessionStorage.getItem(%(state_key)s)) || {}; } catch (e) {}
      ext.store = store;
      ext.chainId = CHAIN_ID;
      if (!ext.handoverInfo) ext.handoverInfo = %(handover)s;
      sessionStorage.setItem(%(state_key)s, JSON.stringify(ext));

      // secondary copies
      sessionStorage.setItem(%(gln_key)s, JSON.stringify(String(STORE_GLN)));
      sessionStorage.setItem(%(handover_key)s, "true");
      document.cookie = %(cookie)s + "=" + STORE_GLN + "; path=/; max-age=31536000";
      store_changed = true;
    } catch (e) {
      return JSON.stringify({ok: false, error: "store switch failed: " + e});
    }
  }
"""

# Shared prologue: origin guard, uuid helper, envelope load/create.
_PRELUDE = r"""
  if (!location.hostname.endsWith("meny.no")) {
    return JSON.stringify({ok: false, error:
      "wrong origin: run this on meny.no (currently " + location.hostname + ")"});
  }

  const uuid = () => (crypto.randomUUID
    ? crypto.randomUUID()
    : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
        const r = Math.random()*16|0;
        return (c === "x" ? r : (r&0x3|0x8)).toString(16);
      }));

  let envelope;
  try {
    envelope = JSON.parse(localStorage.getItem(KEY));
  } catch (e) { envelope = null; }

  if (!envelope || typeof envelope !== "object" || !envelope.data) {
    envelope = {
      encrypted: false,
      data: {
        __id__: uuid(), __rev__: 1, __removed__: false, __type__: "",
        __clientId__: uuid(), title: "cart", items: [], persistedId: uuid()
      }
    };
  }
  if (envelope.encrypted) {
    return JSON.stringify({ok: false, error:
      "cart is encrypted (likely a logged-in session); refusing to write"});
  }
"""

# Shared epilogue: merge INCOMING into the envelope, persist, verify, reload.
#
# Kept identical between the two flavours so a lean and an embedded run produce
# byte-identical carts. Only the way INCOMING is obtained differs.
#
# The reload is part of the handoff: the app reads the store blob at hydration
# only, so without it a store switch is invisible. The cart lives in
# localStorage and survives it. The reload is deferred slightly so the caller
# has a chance to receive this function's return value first - but callers must
# NOT depend on that, and should verify afterwards with build_verify_script().
_MERGE_AND_WRITE = r"""
  const data = envelope.data;
  const before = Array.isArray(data.items) ? data.items.slice() : [];
  const beforeCount = before.length;

  let items;
  if (MODE === "replace") {
    items = INCOMING.slice();
  } else {
    items = before.slice();
    const indexOf = (ean) => items.findIndex(i => String(i.ean) === String(ean));
    for (const line of INCOMING) {
      const at = indexOf(line.ean);
      if (at === -1) {
        items.push(line);
      } else if (MODE === "append") {
        // add to whatever is already there
        const existing = items[at];
        const qty = (Number(existing.quantity) || 0) + (Number(line.quantity) || 0);
        items[at] = Object.assign({}, existing, {
          quantity: qty,
          linePrice: Math.round((Number(line.pricePerUnit) || 0) * qty * 100) / 100,
          __rev__: (Number(existing.__rev__) || 1) + 1,
          hasBeenSynced: false
        });
      } else {
        // "merge": incoming quantity wins
        items[at] = Object.assign({}, items[at], line, {
          __rev__: (Number(items[at].__rev__) || 1) + 1,
          hasBeenSynced: false
        });
      }
    }
  }

  data.items = items;
  data.__rev__ = (Number(data.__rev__) || 1) + 1;
  data.__removed__ = false;
  if (!data.title) data.title = "cart";
  if (!data.__clientId__) data.__clientId__ = uuid();
  if (!data.persistedId) data.persistedId = uuid();

  try {
    localStorage.setItem(KEY, JSON.stringify(envelope));
  } catch (e) {
    return JSON.stringify({ok: false, error: "localStorage write failed: " + e});
  }

  // Verify the round trip rather than trusting the write.
  let verified = [];
  try {
    verified = (JSON.parse(localStorage.getItem(KEY)).data.items) || [];
  } catch (e) {}

  const willReload = RELOAD && (store_changed || FORCE_RELOAD);
  if (willReload) setTimeout(() => location.reload(), 250);

  return JSON.stringify({
    ok: true,
    mode: MODE,
    items_before: beforeCount,
    items_after: verified.length,
    rev: data.__rev__,
    failed: FAILED,
    store_gln: STORE_GLN,
    store_was: store_before,
    store_changed: store_changed,
    reloading: willReload,
    cart: verified.map(i => ({
      ean: i.ean,
      title: (i.product && i.product.title) || null,
      quantity: i.quantity
    })),
    note: willReload
      ? "store set to " + STORE_GLN + "; page is reloading. Do NOT trust this "
        + "result - re-read state afterwards (build_verify_script) and poll for "
        + "2-3s, because the pre-hydration paint still shows the old store."
      : "reload meny.no for the header total to refresh"
  });
"""

# Lean injector. WANTED is [[ean, quantity], ...]; the product records are
# fetched in the page. Async, so it returns a Promise - `await` it in the
# console. Note that some automation bridges return {} instead of resolving it;
# that is not a failure signal.
_LEAN_TEMPLATE = r"""
(async () => {
  const KEY = %(key)s;
  const MODE = %(mode)s;            // "replace" | "merge" | "append"
  const WANTED = %(wanted)s;        // [[ean, quantity], ...]
  const API = %(api)s;              // ".../api/products/{chain}/{store}"
  const STORE_API = %(store_api)s;  // ".../api/stores/{chain}"
  const CHAIN_ID = %(chain_id)s;
  const STORE_GLN = %(store_gln)s;  // null = leave the session's store alone
  const RELOAD = %(reload)s;
  const FORCE_RELOAD = %(force_reload)s;
%(prelude)s%(set_store)s
  // ---- fetch product records from the site's own API --------------------
  // No custom headers: that keeps it a CORS simple request (no preflight).
  const FAILED = [];
  const INCOMING = [];
  const CONCURRENCY = 6;
  const queue = WANTED.slice();

  const num = (...v) => {
    for (const x of v) if (x !== null && x !== undefined && x !== "") return Number(x);
    return 0;
  };

  const worker = async () => {
    while (queue.length) {
      const [ean, wantQty] = queue.shift();
      try {
        const resp = await fetch(API + "/" + ean, {credentials: "omit"});
        if (!resp.ok) { FAILED.push({ean, error: "HTTP " + resp.status}); continue; }
        const p = await resp.json();
        if (!p || !p.ean) { FAILED.push({ean, error: "no product record"}); continue; }

        // Legalise the quantity against this product's own order step, then
        // build the line exactly as models.CartLine.build does server-side.
        // Mirrors Product.min_quantity: only true weight goods step in
        // fractions. For "pr stk" produce, amountInterval is the weight of one
        // item (0.13 = a 130 g potato), not an order step - stepping by it
        // would order 1.04 potatoes.
        const step = p.productSoldByWeight
          ? (num(p.amountInterval, p.unitRules, p.unitRule) || 1)
          : Math.max(1, Math.round(num(p.unitRule, p.unitRules) || 1));
        const qty = Math.round(
          Math.max(step, Math.ceil((Number(wantQty) || 1) / step - 1e-9) * step) * 1000
        ) / 1000;
        const ppu = num(p.pricePerUnit, p.calcPricePerUnit, p.price);

        INCOMING.push({
          id: p.ean, ean: p.ean, quantity: qty,
          addedTime: new Date().toISOString(),
          hasBeenSynced: false,
          __rev__: 1, __removed__: false, __type__: "CART_PRODUCT",
          pricePerUnit: ppu,
          linePrice: Math.round(ppu * qty * 100) / 100,
          comparePricePerUnit: num(p.comparePricePerUnit) || ppu,
          totalDiscount: 0,
          product: p,
          doesNotExistInCurrentStore: false,
          substitute: null,
          shouldSlice: false
        });
      } catch (e) {
        FAILED.push({ean, error: String(e)});
      }
    }
  };

  await Promise.all(Array.from({length: Math.min(CONCURRENCY, WANTED.length)}, worker));

  if (!INCOMING.length) {
    return JSON.stringify({ok: false,
      error: "could not fetch any product record from " + API, failed: FAILED});
  }
  // Keep the caller's order; workers finish out of order.
  const order = new Map(WANTED.map(([e], i) => [String(e), i]));
  INCOMING.sort((a, b) => order.get(String(a.ean)) - order.get(String(b.ean)));
%(merge)s
})()
"""

# Self-contained injector: every line, product record and all, is inlined.
# Still async, because the store switch has to fetch the store record. Pass
# set_store=False if the browser genuinely cannot reach the store API.
_EMBEDDED_TEMPLATE = r"""
(async () => {
  const KEY = %(key)s;
  const MODE = %(mode)s;            // "replace" | "merge" | "append"
  const INCOMING = %(lines)s;       // array of complete cart lines
  const FAILED = [];
  const STORE_API = %(store_api)s;  // ".../api/stores/{chain}"
  const CHAIN_ID = %(chain_id)s;
  const STORE_GLN = %(store_gln)s;  // null = leave the session's store alone
  const RELOAD = %(reload)s;
  const FORCE_RELOAD = %(force_reload)s;
%(prelude)s%(set_store)s
%(merge)s
})()
"""


_COMMENT_RE = re.compile(r"(?<![:\"'/])//(?!\S*[\"']).*$")


def strip_for_transport(script: str) -> str:
    """Drop comments and indentation from a generated script.

    The templates here are written to be read by a person auditing what is
    about to run against their basket, and that is worth keeping in the source.
    It is not worth keeping in the *response*: this script is a payload that
    travels through a model's context to a browser, where nothing reads the
    prose. Measured on a 29-item basket, comments and indentation are 2 484 of
    10 287 characters - about 700 tokens per call, paid again on every rebuild
    after a product swap.

    Deliberately conservative: line structure, spacing inside lines and string
    literals are all left alone, so the result stays valid and diffable rather
    than being run through a real minifier for a few hundred more bytes.
    """
    out: list[str] = []
    for line in script.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        stripped = _COMMENT_RE.sub("", stripped).strip()
        if stripped:
            out.append(stripped)
    return "\n".join(out)


def build_injector(
    lines: Sequence[dict[str, Any]],
    *,
    mode: MergeMode = "merge",
    storage_key: str = CART_STORAGE_KEY,
    embed_products: bool = False,
    chain_id: str | None = None,
    store_id: str | None = None,
    set_store: bool = True,
    reload_after: bool = True,
    force_reload: bool = False,
    readable: bool = False,
) -> str:
    """Return JavaScript that writes ``lines`` into the meny.no basket.

    ``lines`` are full cart lines as produced by :meth:`CartService.to_payload`.

    Modes:
      * ``replace`` - discard whatever is in the basket first
      * ``merge``   - upsert by EAN; incoming quantity wins (default)
      * ``append``  - upsert by EAN; quantities are added together

    By default only the ``(ean, quantity)`` pairs are inlined and the browser
    re-fetches each product record, which keeps the script ~200x smaller. Pass
    ``embed_products=True`` to inline the records instead - self-contained, but
    ~8.4 kB per item.

    ``set_store`` (default on) repoints the session at ``store_id`` when it
    differs from the store the session is currently using, writing the
    authoritative ``extended_user_offline_key`` blob. Because the app only
    reads that at hydration, ``reload_after`` (default on) reloads the page
    when the store actually changed; pass ``force_reload=True`` to reload even
    when it did not.

    ``readable`` keeps the comments and indentation. Off by default - see
    :func:`strip_for_transport` - because the script's audience is a browser,
    not a reader, and the prose is a quarter of what it costs to deliver.

    The returned script is async. Two consequences, both important:

    * Some automation bridges return ``{}`` rather than resolving the promise,
      and the reload discards the value anyway. **A missing result is not a
      failure**, and a returned ``ok: true`` is not proof of anything.
    * Always follow up with :func:`build_verify_script` in a separate call, and
      poll for 2-3 s: the pre-hydration paint after the reload still shows the
      previous store.

    The script is defensive on purpose: it refuses to run off-origin, refuses
    to touch an encrypted (logged-in) cart, aborts the whole run if the store
    switch fails rather than silently loading the cart against the wrong store,
    and verifies the cart write by reading the value back.
    """
    if mode not in ("replace", "merge", "append"):
        raise ValueError(f"unknown merge mode: {mode}")

    from .config import settings

    resolved_store = store_id or settings.store_id
    resolved_chain = chain_id or settings.chain_id

    common = {
        "key": json.dumps(storage_key),
        "mode": json.dumps(mode),
        "prelude": _PRELUDE,
        "merge": _MERGE_AND_WRITE,
        "chain_id": json.dumps(str(resolved_chain)),
        "store_api": json.dumps(f"{STORE_API}/{resolved_chain}"),
        "store_gln": json.dumps(resolved_store if set_store else None),
        "reload": json.dumps(bool(reload_after)),
        "force_reload": json.dumps(bool(force_reload)),
        "set_store": _SET_STORE % {
            "state_key": json.dumps(STORE_STATE_KEY),
            "gln_key": json.dumps(STORE_GLN_KEY),
            "handover_key": json.dumps(STORE_HANDOVER_KEY),
            "cookie": json.dumps(STORE_COOKIE),
            "handover": json.dumps(_DEFAULT_HANDOVER),
        },
    }

    if embed_products:
        script = _EMBEDDED_TEMPLATE % {
            **common,
            "lines": json.dumps(list(lines), ensure_ascii=False,
                                separators=(",", ":")),
        }
    else:
        wanted = [[str(line["ean"]), line.get("quantity", 1)]
                  for line in lines]
        api = f"{PRODUCT_API}/{resolved_chain}/{resolved_store}"
        script = _LEAN_TEMPLATE % {
            **common,
            "wanted": json.dumps(wanted, separators=(",", ":")),
            "api": json.dumps(api),
        }
    return script if readable else strip_for_transport(script)


def build_store_setter(store_id: str | None = None,
                       *, chain_id: str | None = None,
                       reload_after: bool = True) -> str:
    """JavaScript that only switches the session's store, touching no cart.

    Useful before pricing anything, so the basket is never built against one
    store and priced against another. Same caveats as :func:`build_injector`:
    async, reloads on success, verify afterwards.
    """
    from .config import settings

    resolved_store = store_id or settings.store_id
    resolved_chain = chain_id or settings.chain_id

    body = _SET_STORE % {
        "state_key": json.dumps(STORE_STATE_KEY),
        "gln_key": json.dumps(STORE_GLN_KEY),
        "handover_key": json.dumps(STORE_HANDOVER_KEY),
        "cookie": json.dumps(STORE_COOKIE),
        "handover": json.dumps(_DEFAULT_HANDOVER),
    }

    return r"""
(async () => {
  const STORE_API = %(store_api)s;
  const CHAIN_ID = %(chain_id)s;
  const STORE_GLN = %(store_gln)s;
  const RELOAD = %(reload)s;

  if (!location.hostname.endsWith("meny.no")) {
    return JSON.stringify({ok: false, error:
      "wrong origin: run this on meny.no (currently " + location.hostname + ")"});
  }
%(set_store)s
  if (RELOAD && store_changed) setTimeout(() => location.reload(), 250);
  return JSON.stringify({
    ok: true,
    store_gln: STORE_GLN,
    store_was: store_before,
    store_changed: store_changed,
    reloading: RELOAD && store_changed,
    note: "verify afterwards; poll for 2-3s because the pre-hydration paint "
        + "still shows the previous store"
  });
})()
""" % {
        "store_api": json.dumps(f"{STORE_API}/{resolved_chain}"),
        "chain_id": json.dumps(str(resolved_chain)),
        "store_gln": json.dumps(resolved_store),
        "reload": json.dumps(bool(reload_after)),
        "set_store": body,
    }


def build_verify_script(
    expected_gln: str | None = None,
    *,
    expected_items: Sequence[dict[str, Any]] | None = None,
    mode: MergeMode = "merge",
    storage_key: str = CART_STORAGE_KEY,
) -> str:
    """JavaScript that reports what the browser actually ended up with.

    Two questions, answered in one call because they are only ever asked
    together after a handoff:

    * **Which store** has the app settled on?
    * **Is the basket what was ordered?** Pass the shopping list as
      ``expected_items`` (``[{"ean": ..., "quantity": ...}]``, or the
      ``packs`` field straight off ``plan_week_menu``) and the result carries
      a per-line diff: ``missing``, ``wrong_quantity``, ``unexpected``. Without
      it the basket half only reports a count, which cannot distinguish a
      complete basket from a coincidentally equal-sized wrong one.

    Synchronous on purpose: sync scripts survive the automation bridges that
    swallow async return values. :func:`build_injector` returns a promise whose
    value is routinely lost to the reload it performs, so its ``ok: true``
    proves nothing about the basket. This is the call that proves something.

    Callers should poll for 2-3 s after a reload. ``settled`` is false while
    the app is still hydrating (the visible store button has not yet caught up
    with stored state), which is exactly the window that makes a single check
    look like a failure. The **basket half needs no hydration** - it is read
    straight out of localStorage - so ``basket.verdict`` is already final on
    the first call even while ``settled`` is still false.

    Whether ``unexpected`` lines are a failure depends on ``mode``, which must
    be the mode the basket was filled with. Under ``merge`` and ``append`` the
    user's own pre-existing lines are supposed to survive, so they are
    reported and forgiven. Under ``replace`` they are a **failure**: the point
    of replace is that nothing else remains, so a survivor means the clear did
    not happen and the user is about to buy last week's shopping too.

    With no ``expected_items`` the script switches to **inspection**: it lists
    what the basket currently holds, which is the question asked *before*
    filling rather than after. That is a different question from "is this what
    I ordered", and answering it needs no expectation to compare against.
    """
    wanted = [
        [str(i["ean"]), float(i.get("quantity", 1) or 0)]
        for i in (expected_items or [])
    ]
    return r"""
(() => {
  if (!location.hostname.endsWith("meny.no")) {
    return JSON.stringify({ok: false, error:
      "wrong origin: run this on meny.no (currently " + location.hostname + ")"});
  }
  const EXPECTED = %(expected)s;
  const KEY = %(key)s;
  const WANTED = %(wanted)s;
  const MODE = %(mode)s;

  let stateGln = null, stateName = null;
  try {
    const ext = JSON.parse(sessionStorage.getItem(%(state_key)s)) || {};
    if (ext.store) { stateGln = String(ext.store.gln); stateName = ext.store.name; }
  } catch (e) {}

  // What the user can actually see - the store button in the page chrome.
  let label = null;
  const nodes = document.querySelectorAll("button span, button strong, a span");
  for (const el of nodes) {
    if (el.children.length) continue;
    const t = (el.textContent || "").trim();
    if (/^MENY\s+\S/.test(t) && t.length < 40) { label = t; break; }
  }

  // The basket, read from storage rather than from the injector's return value.
  let envelope = null, encrypted = false, items = null;
  try { envelope = JSON.parse(localStorage.getItem(KEY)); } catch (e) {}
  if (envelope && envelope.encrypted) encrypted = true;
  if (envelope && envelope.data && Array.isArray(envelope.data.items)) {
    items = envelope.data.items;
  }

  const round3 = (n) => Math.round((Number(n) || 0) * 1000) / 1000;
  const actual = new Map();
  const duplicates = [];
  for (const it of (items || [])) {
    const ean = String(it.ean);
    if (actual.has(ean)) { duplicates.push(ean); continue; }
    actual.set(ean, {
      quantity: round3(it.quantity),
      title: (it.product && it.product.title) || null,
      price: (typeof it.linePrice === "number") ? it.linePrice : null
    });
  }

  let basket;
  if (items === null) {
    basket = {readable: false, verdict: "unknown",
      error: encrypted
        ? "basket is encrypted (logged in) - log out or use a private window"
        : "no readable CART in localStorage"};
  } else if (!WANTED.length) {
    // Inspection: what is in there now, which is what someone deciding
    // whether to keep it needs to see.
    const lines = [];
    for (const entry of actual) {
      lines.push({ean: entry[0], title: entry[1].title,
                  quantity: entry[1].quantity, line_price: entry[1].price});
    }
    basket = {
      readable: true,
      verdict: actual.size ? "not empty" : "empty",
      item_count: actual.size,
      lines: lines,
      estimated_total: Math.round(
        lines.reduce((n, l) => n + (Number(l.line_price) || 0), 0) * 100) / 100
    };
    if (!actual.size) {
      basket.note = "nothing in the basket - fill it with mode 'merge'";
    } else {
      basket.note = "ask the user whether these should stay. Keep them with "
        + "mode 'merge'; discard them with mode 'replace'. Do not decide "
        + "this for them.";
    }
  } else {
    const missing = [], wrong = [], unexpected = [];
    let correct = 0;
    const wantedSet = new Set();
    for (const pair of WANTED) {
      const ean = String(pair[0]), qty = round3(pair[1]);
      wantedSet.add(ean);
      const got = actual.get(ean);
      if (!got) { missing.push({ean: ean, expected: qty}); continue; }
      if (Math.abs(got.quantity - qty) > 0.0005) {
        wrong.push({ean: ean, title: got.title,
                    expected: qty, actual: got.quantity});
      } else {
        correct += 1;
      }
    }
    for (const entry of actual) {
      if (!wantedSet.has(entry[0])) {
        unexpected.push({ean: entry[0], title: entry[1].title,
                         quantity: entry[1].quantity});
      }
    }
    // Under "replace" a survivor is a failure: the clear did not happen, and
    // the user is about to buy last week's shopping alongside this week's.
    const strays = (MODE === "replace") && unexpected.length;
    basket = {
      readable: true,
      verdict: (missing.length || wrong.length || strays)
        ? "incomplete" : "complete",
      mode: MODE,
      expected_lines: WANTED.length,
      correct_lines: correct,
      item_count: actual.size
    };
    if (missing.length) basket.missing = missing;
    if (wrong.length) basket.wrong_quantity = wrong;
    if (unexpected.length) {
      basket.unexpected = unexpected;
      basket.unexpected_note = strays
        ? "these were NOT ordered and should have been cleared by mode "
          + "'replace'. The basket still holds them: re-run the basket script."
        : "lines in the basket that were not ordered; under " + MODE + " these "
          + "are the user's own and are not a failure";
    }
    if (duplicates.length) basket.duplicate_eans = duplicates;
  }

  const settled = !!(stateName && label && label === stateName);
  return JSON.stringify({
    ok: true,
    store_gln: stateGln,
    store_name: stateName,
    visible_label: label,
    settled: settled,
    matches_expected: EXPECTED ? (String(stateGln) === String(EXPECTED)) : null,
    basket: basket,
    note: settled
      ? "stored state and visible label agree"
      : "store still hydrating - poll again (up to ~3s) before concluding "
        + "anything about it; the basket diff above is already final"
  });
})()
""" % {
        "expected": json.dumps(expected_gln),
        "key": json.dumps(storage_key),
        "wanted": json.dumps(wanted),
        "mode": json.dumps(mode),
        "state_key": json.dumps(STORE_STATE_KEY),
    }


def build_store_probe() -> str:
    """JavaScript that reports which store the browser session is using.

    Reads the authoritative blob first. The storage-wide scan is kept as a
    fallback for the day the site renames that key - it walks localStorage,
    sessionStorage and cookies and pulls out every 13-digit id, reporting where
    each was found, so the caller can resolve them against the store register.
    """
    return r"""
(() => {
  if (!location.hostname.endsWith("meny.no")) {
    return JSON.stringify({ok: false, error:
      "wrong origin: run this on meny.no (currently " + location.hostname + ")"});
  }
  const GLN = /\b\d{13}\b/g;
  const found = new Map();          // id -> Set of places it appeared
  const note = (id, where) => {
    if (!found.has(id)) found.set(id, new Set());
    found.get(id).add(where);
  };

  // Authoritative first.
  let selected = null, selectedName = null;
  try {
    const ext = JSON.parse(sessionStorage.getItem(%(state_key)s)) || {};
    if (ext.store && ext.store.gln) {
      selected = String(ext.store.gln);
      selectedName = ext.store.name || null;
      note(selected, "selected store");
    }
  } catch (e) {}

  const scan = (text, where) => {
    if (typeof text !== "string") return;
    const hits = text.match(GLN);
    if (hits) for (const h of hits) note(h, where);
  };

  for (const [label, store] of [["localStorage", localStorage],
                                ["sessionStorage", sessionStorage]]) {
    let keys = [];
    try { keys = Object.keys(store); } catch (e) { continue; }
    for (const k of keys) {
      // The cart is full of product EANs, which are also 13 digits - scanning
      // it would bury the store id in a hundred false positives.
      if (k === "CART") continue;
      try { scan(store.getItem(k), label + "[" + k + "]"); } catch (e) {}
    }
  }
  scan(document.cookie, "cookie");

  const candidates = [...found.entries()].map(([id, where]) => ({
    store_id: id, seen_in: [...where]
  }));

  return JSON.stringify({
    ok: true,
    selected_store: selected,
    selected_store_name: selectedName,
    candidates,
    note: selected
      ? "selected_store is the store this browser session is using."
      : (candidates.length
          ? "No store selected yet; candidates are other ids found in storage."
          : "No store selected. Pick one on meny.no, or call set_store here.")
  });
})()
""" % {"state_key": json.dumps(STORE_STATE_KEY)}


def build_clear_script(storage_key: str = CART_STORAGE_KEY) -> str:
    """JavaScript that empties the basket without destroying the envelope."""
    return r"""
(() => {
  const KEY = %(key)s;
  let env;
  try { env = JSON.parse(localStorage.getItem(KEY)); } catch (e) { env = null; }
  if (!env || !env.data) return JSON.stringify({ok: true, items_after: 0,
    note: "cart was already absent"});
  const before = (env.data.items || []).length;
  env.data.items = [];
  env.data.__rev__ = (Number(env.data.__rev__) || 1) + 1;
  localStorage.setItem(KEY, JSON.stringify(env));
  return JSON.stringify({ok: true, items_before: before, items_after: 0,
    note: "reload meny.no to refresh the header total"});
})()
""" % {"key": json.dumps(storage_key)}


def build_bookmarklet(lines: Sequence[dict[str, Any]],
                      *, mode: MergeMode = "merge",
                      chain_id: str | None = None,
                      store_id: str | None = None) -> str:
    """Same injector packaged as a ``javascript:`` URL.

    Useful when Claude has no browser-automation tool available: the user
    pastes this into the address bar on meny.no. Always uses the lean flavour -
    an embedded one would blow past every browser's URL length cap.
    """
    from urllib.parse import quote
    script = build_injector(lines, mode=mode, chain_id=chain_id,
                            store_id=store_id)
    return "javascript:" + quote(script, safe="")


def summarise_handoff(result_json: str) -> dict[str, Any]:
    """Parse the injector's return value into a plain dict.

    An empty or unparseable result is NOT evidence of failure: the injector is
    async and reloads the page, so the value is frequently lost. Callers get
    ``inconclusive: True`` in that case and must confirm with
    :func:`build_verify_script`.
    """
    if result_json in (None, "", "{}"):
        return {"ok": None, "inconclusive": True,
                "note": "no result returned (async value lost, or page "
                        "reloaded). Confirm with build_verify_script()."}
    try:
        data = json.loads(result_json)
    except (TypeError, ValueError):
        return {"ok": None, "inconclusive": True,
                "error": "unparseable injector result",
                "raw": str(result_json)[:400],
                "note": "Confirm with build_verify_script()."}
    if isinstance(data, dict) and data.get("reloading"):
        data["inconclusive"] = True
        data.setdefault(
            "note",
            "page is reloading; confirm with build_verify_script().")
    return data


def instructions(mode: MergeMode = "merge", *, lean: bool = True,
                 store_changed: bool = True) -> str:
    """Human-readable fallback instructions."""
    step3 = (
        "3. Paste the script and press Enter. It is async - the console shows a "
        "Promise, and the result appears a moment later."
        if lean else
        "3. Paste the script and press Enter."
    )
    step4 = (
        "4. The page reloads by itself once the store is set; the basket total "
        "in the header updates after that.\n"
        "   Give it 2-3 seconds - the store name shown immediately after the "
        "reload is the old one until the app finishes hydrating."
        if store_changed else
        "4. Reload the page; the basket total in the header will update."
    )
    return (
        "1. Open https://meny.no in your browser (any page on the site).\n"
        "2. Open DevTools -> Console (F12).\n"
        f"{step3}\n"
        f"{step4}\n"
        f"   Mode '{mode}' was used. The script refuses to run off meny.no "
        "and will not touch an encrypted (logged-in) cart."
    )