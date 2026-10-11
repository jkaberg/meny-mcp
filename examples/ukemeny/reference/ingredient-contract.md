# The ingredient contract

Mealie stores an ingredient as three fields - `quantity`, `unit`, `food` - plus
a free-text `note`. Prose, or a null unit, means the amount is not usable: the
shopping list cannot aggregate it and scaling breaks.

**You do not have to remember these rules.** `save_week_manifest` enforces
every one of them and `apply_week_manifest` refuses to write when one fails.
This page is here for when a tool tells you *what* failed. Read it then.

## Allowed units

| Unit | Use for |
|---|---|
| `gram` | anything weighable: meat, fish, vegetables sold by weight, cheese, pasta, rice, linser, drained contents of a can |
| `kilogram` | only when 1 kg or more is the natural amount |
| `milliliter`, `desiliter`, `liter` | liquids: fløte, buljong, melk, olje in volume |
| `teskje`, `spiseskje` | spices, small amounts of fat, condiments |
| `stk` | genuinely countable whole items: egg, løk, gulrot, paprika, tortillalefse |
| `fedd` | hvitløk |
| `klype` | salt/pepper to taste, nothing else |

Banned outright: `boks`, `porsjon`, `hode`, `skvett`, `kopp`, `pakke`, `neve`,
and every US unit. A can of beans is `400 gram sorte bønner` with the note
`"hermetiske, skylt"` - not `1 boks` with the weight hidden in the note.

`stk` may not exist in a fresh Mealie instance.
`apply_week_manifest(create_missing=true)` creates it once.

## Row rules

* `quantity` - a number greater than 0. Never a string, never a range
  (`2-3`), never null, never "etter smak".
* `unit` - a name from the table above, lowercase.
* `food` - singular, lowercase, no brand, no descriptor, no trailing
  whitespace.
* `note` - preparation and specification only: `"i biter"`, `"finhakket"`,
  `"18 %"`, `"fersk"`. **Never an amount, a pack size or a unit.** A digit in
  a note is a validation error.
* Garnishes get real amounts too: `10 gram persille`, not `1 persille`.
* Every ingredient named in an instruction must exist as a row.
  `save_week_manifest` warns when it sees one that does not - it matches
  instruction text against Mealie's own food vocabulary, so `"tilsett vannet"`
  with no water on the list is caught.

## Why creating a food is the dangerous step

`tomat`, `tomat ` and `Tomater` all exist in libraries that have been run for a
while, and each one arrived the same way: a lookup missed on case or plural, and
something created what it could not find.

So the lookup is case-insensitive, whitespace-trimmed, and checks `name`,
`pluralName` and every alias - that is the index `mealie_inventory` builds. And
creating is opt-in: `apply_week_manifest` stops and lists what it would create
until you pass `create_missing=true`, having read the near-matches
`save_week_manifest` reported.

## Why the PUT body is generated, not written by hand

`update_recipes_by_slug` is a PUT: any field left out is wiped. That is how
tags disappeared on a previous run - a second write omitted them.

`apply_week_manifest` builds the complete body from the manifest, every field,
every time. There is no code path that writes a partial recipe, so the failure
is not "discouraged"; it is unreachable.
