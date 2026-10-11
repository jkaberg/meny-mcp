# Driving the server from a Claude skill

These tools are the hands, not the brain. They will search, price and rank
whatever they are asked about, but nothing in here knows that you eat two
portions, that Friday is taco night, or that you would rather not buy
ultra-processed food. That belongs in a **skill** - a standing instruction file
Claude loads when the task matches, so you get the same result from "plan next
week" as from a paragraph of re-explained preferences.

Put the durable things in the skill:

| Belongs in the skill | Belongs in the request |
|---|---|
| Household size, budget stance, foods you avoid | "Skip fish this week" |
| Time limit per meal, calorie/protein targets | "Friday we have guests" |
| Which days to plan, and the approval checkpoint | "Swap Wednesday" |
| House rules for choosing products | - |

Two of those the *server* can hold for you rather than the skill re-stating
them: `set_household(pantry=[…], exclude=[…])` pins the cupboard contents and
the never-buy list for the session. See [Household profile](household.md).

## What a good skill does

**1. Propose before it buys.** Menu first, in plain text, and stop for a yes.
Every tool here is read-only, so nothing has been touched at this point and the
user can rewrite freely. This is the one step worth being strict about.

**2. Batch its lookups.** `find_best_value` takes a *list* of ingredients and
resolves them concurrently; `get_product_details` takes a *list* of EANs. One
call for a week's shopping list, not twenty-five calls in a loop. Instruct the
skill to do this explicitly - the natural failure mode is one call per
ingredient, which is slower and costs far more context.

**3. Let `plan_week_menu` do the arithmetic.** Hand it the whole menu and it
sums repeated ingredients across days ("løk" on three nights becomes one line),
drops pantry staples, converts `2 dl fløte` into whole packs, and prices the
lot in a single call. A skill that re-derives that in prose will get it wrong
and pay for the privilege. Its `shopping_list` output feeds straight into
`price_cart` and `build_basket_script` - they read the `packs` field, so there
is no second stripped-down copy of the list to carry around.

**4. Check ingredients where it matters, not everywhere.** `find_best_value`
already ranks on price per measured unit and filters out-of-stock items. If the
skill has a rule about *what is in* a product, spend one batched
`get_product_details` call on the shortlist - by default it returns the
ingredient list and the allergens actually present, which is what such a rule
needs. Nutrition, marketing copy, origin, storage and the product URL are opt-in
via `fields`, because over a week's 29 products they were 60% of the response
and none of the question.

**5. Verify with a tool, not with attention.** A skill instruction is a
request; a tool result is evidence. Anything a script can check - a total, a
line count, whether the write landed - should be checked by one, and the skill
should require the *output* rather than the model's word for it. This is the
difference between a workflow that is usually right and one that can be
trusted, and it does not depend on the model behaving well.

The recurring failure it prevents: `build_basket_script` returns an async
script that reloads the page, so its `ok: true` is routinely lost or
meaningless. A skill that reads success off it will report a complete basket
for a partial one. `verify_basket_script` takes the shopping list and diffs
the real basket per line - see [Filling the basket](basket.md).

## The resulting shape

All read-only until the last line:

```
set_store("Moholt")                 → pinned for the session (or let it ask)
set_household(pantry=…, exclude=…)  → optional; standing preferences
plan_week_menu(whole menu)          → shopping_list + totals
  └ get_product_details([eans])     → vet the shortlist, swap what fails
price_cart(shopping_list)           → the real total, including fees
        ↓ show the user, get a yes
build_basket_script(shopping_list)  → JavaScript, run on meny.no, then reload
verify_basket_script(shopping_list) → per-line diff; verdict must be "complete"
```

Three things worth telling the skill outright, because it cannot infer them:

- **Settle the store first, explicitly.** Every price depends on it, and a menu
  priced against the wrong store has to be redone. Silence is not confirmation:
  a whole week can be planned against a default without the subject coming up.
  Every response names the store it used, so the skill can check rather than
  assume. See [Choosing a store](stores.md).
- **Search in Norwegian.** `kyllingfilet`, not `chicken breast`. The catalogue
  has no English in it.
- **The last step needs a browser.** `build_basket_script` returns JavaScript;
  it does not touch the basket. See [Filling the basket](basket.md).

## What `plan_week_menu` takes

> "Plan a week of dinners for 4, prefer Norwegian ingredients, then show me
> the total before adding anything."

The menu goes in as a day → meal mapping:

```json
{
  "Mandag":  {"name": "Taco",       "ingredients": ["400 g kjøttdeig", "2 stk løk"]},
  "Tirsdag": {"name": "Laks",       "ingredients": ["600 g laks", "1 kg poteter"]},
  "Onsdag":  {"name": "Pastagryte", "ingredients": ["500 g pasta", "2 dl fløte"]}
}
```

and it returns a `shopping_list` (what was bought for which meal, at what price
per unit) and `totals`. Pass the `shopping_list` straight to `price_cart` or
`build_basket_script`; both accept its `packs` field as the quantity.

Outputs are trimmed on purpose. A key whose value is `null` or `false` is
omitted rather than sent; `get_product_details` reports only the allergens a
product actually contains instead of the fourteen it was screened for; and
nothing is stated twice, because a fact restated in a second shape is a fact the
reader has to reconcile as well as pay for.

## The worked example

Skills live in `~/.claude/skills/<name>/SKILL.md` (or `.claude/skills/` in a
project). There is a worked example in
[`examples/ukemeny/SKILL.md`](../examples/ukemeny/SKILL.md) - a 7-day dinner
planner with a fixed household size, a quality rule and an approval checkpoint.
It also drives Mealie: it takes inventory before it plans, so the week is built
from recipes the user already has (with a cooldown on anything served recently)
instead of a pile of near-duplicates, and it writes ingredients as structured
`quantity` / `unit` / `food` rows rather than prose.

Copy the folder, edit the preferences to yours. The point is that next week you
type three words.

It is also worth reading as an answer to the question *what belongs in prose
and what belongs in a tool*. The mechanical parts of that run - pulling an
inventory, checking an ingredient contract, building a PUT body, reading back
what landed - are handled by the optional
[weekly menu toolset](ukemeny.md), and the skill file is the ~150 lines of
judgement that are left: the quality rule, the reuse policy, the approval gate.
That split exists because the parts that kept failing were the mechanical ones,
and no amount of additional prose fixed them.

Note where that logic lives. Bundling it as scripts alongside `SKILL.md` is the
obvious move and it does not work: a skill's scripts run in a code-execution
sandbox that is not the user's machine, so they cannot reach a self-hosted
Mealie, and the files they write are invisible to everything else. An MCP
server is already running locally with the user's network - so anything a skill
needs *executed* rather than *decided* belongs on the server.
