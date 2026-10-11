# Design notes

## Five things that make this non-trivial

### 1. Unit prices are not comparable across unit groups

A single search for "melk" returns products priced per **kg**, per **l** and
per **porsjon**. Nine distinct units appear in live data - including both
`meter` and `100 meter`, which the API does **not** normalise between.

Sorting a search by `comparePricePerUnit` therefore produces confidently wrong
answers on most real queries. This server buckets candidates by unit group and
only ranks within a bucket. Unknown units rank never, rather than wrongly.

### 2. The displayed total is not the sum of item prices

A real 3-line basket:

| | NOK |
|---|---|
| Items subtotal | 212.50 |
| Fees | 6.75 |
| Weight buffer (reserve for fresh goods) | 26.50 |
| **Total to pay** | **245.75** |

A naive sum understates by **33.25 kr (16%)**. `price_cart` always reports the
calculator's figure and breaks out the components.

### 3. Cheapest per kilo is not the same question as cheapest

Price per measured unit answers *which is better value*; a recipe asks *which
costs least to cover 200 g*. For small needs those come apart, in both
directions:

| Need | Cheapest per unit | Actually cheapest |
|---|---|---|
| 200 g spinat | Babyspinat 65 g @ 153.85 kr/kg → 4 bags, **40.00** | Spinat 200 g @ 184.50 kr/kg → **36.90** |
| 450 g kyllingfilet | 1 kg @ 185 kr/kg → **185.00** | 460 g @ 215 kr/kg → **99.00** |

So whenever the need is known, candidates are ranked on the kroner to cover it -
whole packs included - which is the same figure `_packs_required` will order.
`max_pack_multiple` only ever caught the gross version of this (a 5 kg sack of
rice bought for 200 g) and stays as a backstop.

With no stated need there is nothing to cover, and price per unit is again the
only sensible question.

### 4. A nominal pack size is not a pack

The fresh counter's cod fillet is labelled `packageSize: "2KG"` with
`measurementValue: 2000` - and it is `productSoldByWeight` with
`amountInterval: 0.2`, so 200 g is a legal order. Reading the label as a pack
did two things wrong at once: it rounded a 400 g need up to one "pack", and it
made the product look 5× oversized so the pack guard threw it out of the ranking
before the price was read.

Verified against the live calculator:

| Ordered | Charged |
|---|---|
| `quantity: 0.4` | **171.60** |
| `quantity: 1.0` (what the old code sent) | 429.00 |

Goods sold by weight therefore have no pack constraint at all; the order step is
`min_quantity` and anything above it is buyable.

### 5. "2 stk løk" and "300 g løk" are different questions

Loose produce is priced per kg but bought by the piece, and the fields that
describe this are actively misleading:

- `amountInterval` on "Poteter / pr stk" is `0.13` - the *weight of one potato*,
  not an order step. Reading it as a step orders **1.04 potatoes**. Only
  `productSoldByWeight` goods genuinely step in fractions; everything else steps
  in whole items.
- Search hits omit `measurementValue` and `packageSize` entirely, so pack size
  silently falls back to `weight`, a placeholder `1.0` on loose produce. Pack
  arithmetic must use the full product record, never a search hit.
- A count need cannot be ranked on kr/kg. For "2 stk rødløk" a 1 kg bag wins on
  kr/kg and costs 4x more than a 2-pack. This server ranks count needs on *total
  kroner to cover N items*, which is the only figure comparable across a loose
  item, a 2-pack and a bag.
- The unit-group bucketing in point 1 must be **off** for count needs. "1 stk
  sitron" returns six juices and iced teas against two actual lemons, so
  majority rule would discard every lemon before ranking.

Where a pack's piece count is genuinely absent from the data (a net, a bag),
one pack is assumed to cover the need and the line is flagged
`pack_covers_count: unknown` rather than guessed at - with the `package_size`
beside it, so the reader can judge instead of guessing too.

That flag used to fire on loose lemons, avocados and garlic, which the shop
plainly sells one at a time. It no longer does: a product whose *comparable*
unit is `stk` is priced per piece, so unless its pack size is stated as a mass
or a volume, one order unit is one item. What is left flagged is the genuinely
unknowable - a 400 g bag of carrots, where nothing in the record says how many
carrots that is.

## API notes

Base URL: `https://platform-rest-prod.ngdata.no` · chain `1300` = Meny.
Only header that matters: `fwc-chain-id`. No cookie, bearer or CSRF token.

| Endpoint | Method | Notes |
|---|---|---|
| `/api/episearch/{chain}/autosuggest` | GET | Full search. `page_size` ≤ 500, `page` paginates |
| `/api/products/{chain}/{store}/{ean}` | GET | ~90-field product record |
| `/api/stores/{chain}[/{store}]` | GET | Store list / detail |
| `/api/stores/{chain}/{store}/pickupfees` | GET | Delivery and pickup fees |
| `/api/calculator/{chain}/{store}` | PUT | Prices a whole cart; stateless |

The `/sok` results page is server-rendered, so `autosuggest` is the only
client-reachable full-text search - it is a complete search engine despite the
name.

The calculator requires the **full product object** embedded in each cart line;
omitting it returns `400 Invalid payload`.

Availability comes from `isOutOfStock`, `isForSale`, `isRevoked` and
`mustOrderFromSupplier`. `availableInventory` exists in the schema but was
`null` for every product sampled - treat it as advisory only.

Field types are not stable across product kinds. `storage` is free text for
ambient goods but a `{"minTemperature": 0, "maxTemperature": 4}` dict for
chilled ones; typing it strictly rejected the record, and since a fetch failure
was reported as "not found", fresh items silently vanished from carts. Anything
read off this API wants a permissive type and a `model_config` that keeps
unknown keys.

The product endpoint is CORS-open to `https://meny.no`, which is what lets the
basket script fetch records in the browser instead of carrying them.
