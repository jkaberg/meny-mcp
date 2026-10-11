# The week manifest

One record, written by `save_week_manifest` at the approval gate and held
server-side by meny-mcp, read by everything after it.

It exists because the Mealie recipe and the Meny basket used to be built
*separately* from the model's in-context memory of the menu, which let them
drift - the recipe said 450 g chicken and the basket bought 400 g, and nothing
in the system could notice. They are now two projections of this file:

```
manifest ──apply_week_manifest───────► Mealie PUT body
         └─week_menu_for_planning────► plan_week_menu argument
```

It also makes the run **resumable**. It is on disk, not in the conversation, so
if the session dies after four recipes - or the context is compacted mid-run -
calling `apply_week_manifest` again finishes the other three and creates
nothing twice.

## Shape

```json
{
  "week_start": "2026-08-25",
  "store": {"gln": "7080001271947", "name": "MENY Moholt"},
  "days": [
    {
      "date": "2026-08-25",
      "day": "Tirsdag",
      "dish": "Kremet kyllinggryte",
      "source": "new",
      "slug": null,
      "recipe_id": null,
      "servings": 2,
      "prep_time": "10 minutter",
      "perform_time": "25 minutter",
      "total_time": "35 minutter",
      "description": "Kremet gryte med kylling og sopp. 650 kcal, 48 g protein.",
      "tags": ["Ukemeny", "Under 40 min", "Høyt proteininnhold"],
      "nutrition": {
        "calories": "650", "proteinContent": "48",
        "carbohydrateContent": "40", "fatContent": "22"
      },
      "instructions": ["Brun kyllingen i olje.", "Tilsett fløten og la det koke."],
      "ingredients": [
        {"quantity": 450, "unit": "gram", "food": "kyllingfilet", "note": "i biter"},
        {"quantity": 2, "unit": "stk", "food": "løk", "note": "finhakket"}
      ]
    }
  ],
  "steps_done": []
}
```

Seven days, passed as the `days` argument. `slug` and `recipe_id` start null
for new dishes and are filled in by `apply_week_manifest`; for a reused dish,
set `source: "mealie"` and the existing `slug` up front.

Days **merge by date**, so fixing one rejected day means sending that one day,
not all seven.

## Ingredients are written by name, not by id

```json
{"quantity": 450, "unit": "gram", "food": "kyllingfilet", "note": "i biter"}
```

Not a uuid in sight. `save_week_manifest` resolves the names against the ids
`mealie_inventory` cached and reports a near-match when one misses
(`food 'Tomater' is not in Mealie  did you mean: tomat`), which is the moment
the old flow instead created a duplicate food. Ids are attached by the script
when it builds the PUT body.

The rules those rows must satisfy are in
[ingredient-contract.md](ingredient-contract.md), and every one of them is
enforced by `save_week_manifest` before anything is written.
