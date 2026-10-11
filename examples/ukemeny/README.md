# ukemeny - the worked skill

A 7-day dinner planner that reuses the Mealie library before writing anything
new, schedules the week, and fills the Meny.no basket with the cheapest clean
ingredients.

```
SKILL.md      the judgement: quality rule, reuse policy, approval gate
reference/    loaded when a step needs it, not on every run
```

The mechanics are **not** in this folder. They are MCP tools on the meny-mcp
server - see [docs/ukemeny.md](../../docs/ukemeny.md) for what they do and how
to turn them on.

## Install

```bash
cp -r examples/ukemeny ~/.claude/skills/ukemeny
```

Then enable the toolset on the meny-mcp server - `MEALIE_URL` and
`MEALIE_TOKEN`, set wherever that server runs. If it is the container, that is
`.env` beside `docker-compose.yml` and a rebuild; if it is stdio in a desktop
client, the `env` block of its entry. [docs/ukemeny.md](../../docs/ukemeny.md)
has both, and the security note that goes with them.

The five `ukemeny` tools appear only after a restart and only when both
variables are set. Edit the constraints in `SKILL.md` to yours - servings,
calorie target, time limit, the store name in Step 0.

## Why it is shaped like this

A skill written as instructions alone kept making the same three mistakes:
recipes with ingredients Mealie could not parse, a week plan reported as
scheduled while it was empty, and a basket a few lines short of complete. More
instructions did not help, so the mechanical parts are tools:

| Tool | Replaces |
|---|---|
| `mealie_inventory` | four inventory calls whose output was 90% lookup tables - now resolved server-side, with a shortlist coming back |
| `save_week_manifest` | "check these five things after each write" - now a hard gate before any write |
| `apply_week_manifest` | hand-built PUT bodies - now generated complete from one manifest, idempotently |
| `verify_week_run` | "confirm the entries are there" - now a PASS/FAIL report |

What is left in `SKILL.md` is the part no tool can do: deciding whether a dish
is good, whether a week is varied, and whether a product is real food.

## The gate

`verify_week_run` is the only thing that may declare the week done. It fetches
from Mealie and folds in the browser's own basket diff, shares no code path
with the writer, and returns FAIL if anything is missing:

```
recipes in Mealie
  [PASS] kremet-kyllinggryte    12 ingredients
  [FAIL] torsk-med-potet        row 3 (persille) has no unit

week plan
  [PASS] 2026-08-25 kremet-kyllinggryte
  [FAIL] 2026-08-26 torsk-med-potet    nothing scheduled on this date

meny.no basket
  [FAIL] basket contents               10/12 lines - incomplete
  [FAIL]   missing 7090000000009       expected 3

FAIL - 8/12 checks. The week is NOT done; fix these and re-run
apply_week_manifest, then this.
```

That report is the deliverable. "I checked and it looks right" is not.
