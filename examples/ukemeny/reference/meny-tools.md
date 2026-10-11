# Working with the Meny.no MCP

## What the tools already handle - do not re-litigate

- choosing the cheapest way to cover the stated amount, not the cheapest per
  kilo: four 65 g bags of spinach are not a better buy than one 200 g bag
- filtering out-of-stock, alcohol and age-restricted items
- rejecting packs far larger than the recipe needs
- ordering the weight you asked for from the counter, and whole packs otherwise
- refusing products that merely share a word with the ingredient - avocado oil
  for "avokado", a ready meal for "torsk", poultry bacon for "bacon"
- retrying alternative Norwegian spellings, then the shop's own synonyms,
  before reporting a miss - which is what resolves a category word like "biff"
  to the cuts it actually means
- skipping ~70 pantry staples: seasoning, oils, vinegars, baking goods, sauces
  in bottles and jars (pass `extra_pantry`, or `set_household`, for the rest)
- summing repeated ingredients across days, converting `2 dl fløte` into whole
  packs, and pricing the basket - all inside one `plan_week_menu` call

Do not reproduce any of that in prose. You will get it wrong and pay for it.

## What the tools flag for you to relay

These are not decoration:

- **`substitution`** on a shopping-list line means the right ingredient in a
  different state (dried, frozen, canned, smoked). One pack is ordered, *not*
  the recipe's weight, because 20 g of fresh ginger is not 20 g of ground
  ginger. Say so in the menu.
- **`matched_as`** - the ingredient was found under a different spelling.
- **`matched_by: shop synonym`** - the product's title does *not* name the
  ingredient; the shop's own cross-references connected them. Worth a glance:
  it is the one pick you cannot check from the title.
- **`rejected`** - closer-looking products that were thrown out, and why.
- **`pack_covers_count: unknown`** - the data does not say how many items are
  in the pack. Check `package_size` beside it before promising four peppers.

## Rules that prevent wrong answers

- Only compare unit prices **within the same `unit_group`**. 245 kr/kg and
  21 kr/l are not comparable numbers.
- Report only prices and stock the tools actually returned. Never estimate a
  price, and never claim something is in stock you did not look up.
- Nothing touches the basket until `build_basket_script`'s output is run in the
  browser. Every step before that is read-only.
- Search in Norwegian. The catalogue contains no English.

## Batching

`find_best_value` takes a **list** of ingredient names; `get_product_details`
takes a **list** of EANs. Send the whole week in one call. One call per
ingredient is slower and floods the context for no extra information.

## Household profile

`set_household(pantry=[...], exclude=[...], accept_forms=[...])` holds standing
preferences for the session. Use `exclude` for allergies and dislikes rather
than editing them out of the menu by hand - it drops the ingredient *and*
refuses products that contain it, which hand-editing does not.

Set `accept_forms=["frossen"]`: the clean-eating rule welcomes plain frozen
fish and vegetables, and without this the server treats an unrequested frozen
product as a substitution and marks it down. Do **not** write "frossen" into
the ingredient lines instead - that penalises every fresh product for not being
frozen, which is the opposite of what is meant.

## Why `verify_basket_script` is not optional

`build_basket_script` returns an **async** script that reloads the page. Its
return value is routinely lost to the reload, and automation bridges commonly
return `{}` for an async IIFE. So its `ok: true` proves nothing - a partial
handoff and a complete one look identical from there.

`verify_basket_script` is synchronous, reads `localStorage` directly, and takes
the shopping list, so it answers the actual question per line: `missing`,
`wrong_quantity`, `unexpected`. `basket.verdict` must read `complete`.

Two details:
- The basket verdict is **final on the first call** - no hydration needed.
- The *store* half needs 2-3 s of polling until `settled` is true, because the
  pre-hydration paint still shows the previous store.
