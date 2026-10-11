# Filling the basket

**The MCP server cannot write to your basket directly, and this is not a
limitation that can be engineered away.**

The anonymous meny.no basket lives in the browser, in `localStorage["CART"]`.
Every cart line carries `hasBeenSynced: false`, and `PUT /api/calculator` is a
stateless pricing service that stores nothing. There is no server-side
anonymous cart to POST to.

So `build_basket_script` returns JavaScript. Run it either way:

1. **Browser automation** (Claude in Chrome) - Claude runs it for you.
2. **Manually** - open meny.no, DevTools → Console, paste, Enter, reload.

The script is **async**: it returns a Promise, so `await` it (browser
automation should resolve the value rather than screenshotting `Promise
{<pending>}`).

It is also defensive: it refuses to run off `meny.no`, refuses to touch an
encrypted (logged-in) cart, and reads the value back to verify the write.

## Look before you fill

A basket is rarely empty by accident. Whatever is already in it is either a
deliberate mid-week top-up or last week's list that never got checked out, and
**nothing on this side can tell those apart** - so the tools do not try.

`inspect_basket_script` reports what is there, and the answer belongs to the
user:

```json
{"basket": {"verdict": "not empty", "item_count": 3, "estimated_total": 212.50,
            "lines": [{"ean": "7039610012854", "title": "Kyllingfilet 460g",
                       "quantity": 2, "line_price": 198.00}]}}
```

Keep them → fill with `mode: "merge"`. Discard them → `mode: "replace"`, which
clears and fills in one script rather than needing a separate
`clear_basket_script` run first.

It is also the cheapest place to discover an **encrypted cart**. A logged-in
session cannot be written to at all (see below), and finding that out here
costs one question - finding it out after pricing a week's shopping wastes the
whole run.

## Its return value is not proof - verify separately

The injector is async *and* it reloads the page, so its result is frequently
lost: automation bridges commonly hand back `{}` for an async IIFE, and the
reload discards the value regardless. A missing result is therefore not a
failure signal - and, more dangerously, a returned `ok: true` is not a success
signal either. A basket that took eight of twelve lines looks exactly like one
that took all twelve.

`verify_basket_script` is the answer, and it is not optional:

```
verify_basket_script(items=shopping_list)   → run on meny.no
```

It is **synchronous**, so bridges cannot swallow it, and it reads
`localStorage` directly rather than believing the writer. Pass the same
shopping list and it diffs per line:

```json
{"basket": {"verdict": "incomplete", "expected_lines": 12, "correct_lines": 10,
            "missing": [{"ean": "7090000000009", "expected": 3}],
            "wrong_quantity": [{"ean": "2000193100004",
                                "expected": 0.4, "actual": 1}]}}
```

`verdict` must read `complete`. Pass the **same mode you filled with**, because
it decides what `unexpected` means:

| Mode | A line in the basket that was not ordered |
|---|---|
| `merge`, `append` | The user's own. Reported, forgiven. |
| `replace` | A **failure** - the clear did not happen, and they are about to buy last week's shopping too. |

That asymmetry is the whole reason the mode is a parameter rather than an
assumption. A replace that silently half-worked looks exactly like a merge that
worked.

Two timing notes: the basket verdict is **final on the first call** (no
hydration involved), while the *store* half of the result needs 2-3 s of
polling until `settled` is true, because the pre-hydration paint still shows
the previous store.

Modes: `merge` (upsert, incoming quantity wins), `append` (add to existing
quantities), `replace` (discard current basket first).

## A logged-in session blocks this last step

Worth knowing before you rely on the workflow: **logging in to meny.no encrypts
the cart**, and the script will not write to one. It stops with

```
cart is encrypted (likely a logged-in session); refusing to write
```

rather than corrupting it. Log out, or use a private window, to fill the basket
this way. This affects only `build_basket_script` and `clear_basket_script` -
search, pricing and menu planning are unauthenticated and work either way.
There is no way around it from this side: the encryption key lives in the
logged-in session, and writing a plaintext cart over an encrypted one is how
you lose a basket rather than fill one.

## It also sets the store

A cart built for store A, dropped into a browser session set to store B, shows
the right items at the wrong prices - and checkout follows the session, not the
items. Each cart line embeds its own product snapshot, so the *contents* stay
correct either way, but the header and checkout read the session state.

So the script points the session at the same store the cart was priced for,
writing the three places meny.no keeps in sync:

| Where | Key | Value |
|---|---|---|
| `sessionStorage` | `WEBSHOP_PREFERRED_PICKUP_STORE_GLN` | JSON-quoted GLN - what the SPA reads on hydration |
| `sessionStorage` | `WEBSHOP_HAS_UPDATED_STORE_AND_HANDOVER_INFO` | `"true"` - suppresses the delivery-method modal |
| cookie | `anonymousUserGln` | raw GLN, readable before the SPA hydrates |

All three are read **at hydration only**, so a live page does not react -
**reload after running the script.** The result reports `store_was`,
`store_gln` and `store_changed`, and says nothing changed when the browser was
already on the right store. Pass `set_store_in_browser: false` to leave the
session alone.

## Why the script is small

A cart line must embed the **whole** product record - 96 fields, ~8.4 kB - or
the site cannot render it. Inlining twenty of those makes a 250 kB script,
which then has to travel back through the model's context to reach your
browser. That is the single most expensive thing this server could do.

It doesn't. The script ships `{ean, quantity}` pairs and re-fetches the product
records **in the page**, from the same endpoint the site itself calls. Verified:
`/api/products/{chain}/{store}/{ean}` answers a plain cross-origin GET from
`https://meny.no` with `access-control-allow-origin: https://meny.no`, and
sending no custom headers keeps it a CORS *simple request*, so there is no
preflight.

For a 20-item week, measured:

| | Script | Whole tool result |
|---|---|---|
| Embedded products | 249 kB | 251 kB |
| Fetched in-browser | 6 kB | **8 kB** |

The carts the two produce are byte-identical, `addedTime` aside. Pass
`embed_products: true` for the self-contained version if the browser cannot
reach the API.
