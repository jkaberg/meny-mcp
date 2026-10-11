# The weekly menu toolset

An optional second toolset on the same server, for the weekly meal-planning
run: it takes inventory of a [Mealie](https://mealie.io) library, holds the
approved week as a manifest, writes the recipes and the meal plan, and verifies
what actually landed.

It registers **only** when `MEALIE_URL` and `MEALIE_TOKEN` are set, so a server
configured purely for groceries advertises purely grocery tools.

| Variable | Meaning |
|---|---|
| `MEALIE_URL` | Base URL of the Mealie instance |
| `MEALIE_TOKEN` | API token (Mealie → user profile → API tokens) |
| `MENY_STATE_DIR` | Where the manifest and id maps live. `/data` in the image |
| `MENY_AUTH_TOKEN` | Shared secret that unlocks these tools over HTTP |

These are the only tools here that hold a credential and write with it, so over
HTTP they are **hidden until authenticated** - invisible in `tools/list` and
refused on `tools/call`. Setting `MEALIE_URL` without `MENY_AUTH_TOKEN` leaves
them unreachable rather than public. Full model in
[Hosting over HTTP](hosting.md#security--two-tiers-on-one-endpoint).

## Turning it on

Set them where the server actually runs.

**Container (the usual case - this is how a hosted connector reaches it).**
Put them in `.env` beside `docker-compose.yml`:

```bash
MEALIE_URL=https://mealie.example.com
MEALIE_TOKEN=...
MENY_AUTH_TOKEN=...   # python -c "import secrets;print(secrets.token_urlsafe(32))"
```

then `docker compose up -d --build`. The compose file passes all three through
and mounts a named volume on `/data`, which is where the manifest lives -
without it a redeploy loses a run half way through.

Point the connector at the **authenticated** URL:

```
https://meny-mcp.example.com/mcp/<MENY_AUTH_TOKEN>
```

Plain `/mcp` keeps working and keeps serving the grocery tools, so anything
already pointed there is unaffected.

**stdio, in a desktop client.** No `MENY_AUTH_TOKEN` needed - there is no
request to authenticate. Put the Mealie variables in the `env` block of the
server's entry in the client's MCP config:

```json
"meny": {
  "command": "meny-mcp",
  "env": {
    "MENY_STORE_ID": "7080001271947",
    "MEALIE_URL": "https://mealie.example.com",
    "MEALIE_TOKEN": "..."
  }
}
```

Either way the tools appear only after a restart, and only if both variables
are set. If the toolset is missing, that is the first thing to check.

## One manifest at a time

The server holds a single active manifest, which is right for a household
server and wrong for a shared one: two people planning different weeks against
the same endpoint would overwrite each other. Give them separate instances, or
separate `MENY_STATE_DIR` values.

## Why this is server-side rather than a skill's own scripts

The obvious place for this logic is a few Python scripts bundled with the
skill. It does not work: a skill's scripts execute in a code-execution sandbox
that is **not the user's machine**, so they cannot reach a self-hosted Mealie,
and the files they write are not the ones anything else sees.

An MCP server has neither problem. It already runs on the user's machine with
their network - that is the whole reason the grocery tools work - so that is
where the mechanical half of the run belongs.

| Tool | Does |
|---|---|
| `mealie_inventory` | Resolves every unit, food and tag id, saves them server-side, returns the reuse shortlist |
| `save_week_manifest` | Saves the week as the plan of record and validates it against the ingredient contract |
| `week_menu_for_planning` | Returns the manifest as a `plan_week_menu` argument |
| `apply_week_manifest` | Writes recipes and meal plan entries, idempotently |
| `verify_week_run` | Fetches everything back and returns one PASS/FAIL report |

## The three ideas

**1. Lookup data does not belong in the conversation.** Step 0 used to pull
several hundred food records, every unit and every tag through the model's
context, before the first menu idea and still there when the run reached the
steps that go wrong. None of it needs to be *read* - the id maps need to be
*looked up*, once each, by name. So `mealie_inventory` resolves them
server-side and returns only what needs judgement: the reuse shortlist, with
each recipe's cooldown status already computed.

**2. One manifest, two projections.** The Mealie recipe and the Meny basket
used to be built separately from the model's in-context memory of the menu,
which let them drift - the recipe saying 450 g chicken while the basket bought
400 g, with nothing able to notice. They are now both derived from the saved
manifest: `apply_week_manifest` builds the PUT body from it, and
`week_menu_for_planning` builds the shopping input from it.

Because the manifest is on disk rather than in the conversation, the run also
survives a compaction or a restart: `apply_week_manifest` is idempotent, so
calling it again after an interruption finishes the job and creates nothing
twice.

**3. Mechanical rules belong in code.** A number is a number, a unit is in the
table or it is not, a recipe was served 9 days ago or it was not. `save_week_manifest` checks them and reports precisely:

```
quantity '2' must be a number, not a string, a range or null
unit 'boks' is banned - use a weight or a volume and put the rest in note
food 'Tomater' is not in Mealie  did you mean: tomat
note '1 stk hakket' contains a number - amounts belong in quantity
served 7 days ago - inside the 14-day cooldown
instructions mention 'vannet' but 'vann' is not an ingredient row
```

Nothing is written while any of those stand. Fixing one day means re-sending
that day; days merge by date.

## The gate

`verify_week_run` is the only thing that may declare a week done. It fetches
every recipe and meal plan entry back from Mealie, folds in the browser's
basket diff from [`verify_basket_script`](basket.md), and returns `PASS` or
`FAIL` with a report to show the user.

It deliberately shares no code path with the writer - a verifier built on the
writer's assumptions verifies nothing - and an unchecked basket is a `FAIL`,
not a pass with a caveat.

## Mealie's update is a PATCH, and why that matters

`apply_week_manifest` writes with `PATCH /recipes/{slug}`. `PUT` looks like the
right verb and is a trap - verified against Mealie 3.22 on 2026-08-20:

| Request | Result |
|---|---|
| `PUT` without `id` | **400 "Recipe already exists"** |
| `PUT` with `id`, `userId`, `householdId`, `groupId` | 200 |
| `PATCH` without any of them | 200 |

Mealie's update applies the submitted object field by field, so a `PUT` that
omits `id` writes NULL over a NOT NULL column. The recipe controller maps
*every* SQL integrity error to the same message - **"Recipe already exists"** -
which names neither the cause nor the field, and fires even when updating a
recipe that plainly exists and is not being renamed. If you see that message,
it almost never means what it says.

`PATCH` also leaves unsent fields alone, so `dateAdded`, `createdAt`, `image`
and `comments` survive a write. The `PUT` path nulled those by omission even
when it succeeded.

The guarantee is therefore: **every field the contract owns is written on every
update; every field the server owns is left alone.** The original failure this
was built to prevent - tags vanishing because a second write omitted them -
still cannot happen, because the body always carries the tags.

## Tag names are Mealie's, not yours

Mealie title-cases tag names on creation and transliterates diacritics into the
slug: `"Høyt proteininnhold"` is stored as name `"Høyt Proteininnhold"` under
slug `"hoyt-proteininnhold"`. Two consequences, both handled:

- Lookups fold case, so the manifest's `"Under 40 min"` finds the stored
  `"Under 40 Min"` instead of creating a second tag.
- A genuinely new tag is slugified the way Mealie would slugify it, so it does
  not arrive as `"høyt-proteininnhold"` beside an existing
  `"hoyt-proteininnhold"`.

## Creating foods is the dangerous step

Libraries that have been running a while contain `tomat`, `tomat ` and
`Tomater`, and each arrived the same way: a lookup missed on case or plural,
and something created what it could not find.

So lookups fold case, whitespace, plurals and aliases, and creating is opt-in.
`apply_week_manifest` stops and lists what it would create until called with
`create_missing=true`.
