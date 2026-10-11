# meny-mcp

An MCP server for **meny.no** (NorgesGruppen, Norway). Search groceries,
compare price per measured unit, plan week menus, and fill the shopping basket
from Claude.

> Unofficial. This talks to an undocumented API that belongs to NorgesGruppen.
> It can change or break without notice. Built for personal meal planning;
> please keep request volume reasonable.

No login required - the whole product/pricing API is unauthenticated.

## Connect Claude

### Use the public instance

A hosted instance runs at **https://meny-mcp.kaberg.me/mcp**. Nothing to
install:

```json
{
  "mcpServers": {
    "meny": {
      "type": "http",
      "url": "https://meny-mcp.kaberg.me/mcp"
    }
  }
}
```

In Claude Desktop / claude.ai, add it as a custom connector with that URL. It
has no authentication and no per-user state beyond your MCP session, so treat
it as a convenience - [run your own](#run-your-own) if you would rather not
depend on someone else's box.

Older clients that cannot speak HTTP directly can bridge:

```json
{
  "mcpServers": {
    "meny": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://meny-mcp.kaberg.me/mcp"]
    }
  }
}
```

### Run your own

**Docker, stdio** - Claude Desktop launches the server itself:

```bash
docker build -t meny-mcp .
```

```json
{
  "mcpServers": {
    "meny": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "MENY_TRANSPORT=stdio",
        "-e", "MENY_STORE_ID=7080001150488",
        "meny-mcp"
      ]
    }
  }
}
```

`-i` is required - stdio MCP is JSON-RPC over stdin/stdout. `MENY_TRANSPORT=stdio`
is also required: the image defaults to `http`, and without the override the
container starts a web server that never speaks to the client.

**Docker Compose, HTTP** - host once, connect from several clients:

```bash
docker compose up -d
# endpoint: http://localhost:8000/mcp
```

The compose service expects `DOMAIN` to be set (it builds the `Host`/`Origin`
allow-list for a reverse proxy from it) - put it in a `.env` beside the compose
file, or drop those two variables when running purely on loopback. See
[Hosting over HTTP](docs/hosting.md) before exposing it beyond localhost.

**Local, no Docker:**

```bash
python -m venv .venv && .venv/bin/pip install .

.venv/bin/meny-mcp                                  # stdio
.venv/bin/meny-mcp --transport http --port 8000     # HTTP
```

```json
{
  "mcpServers": {
    "meny": {
      "command": "/absolute/path/to/.venv/bin/meny-mcp",
      "env": { "MENY_STORE_ID": "7080001150488" }
    }
  }
}
```

## Set your store

Prices, stock and assortment all differ per store, so a wrong store produces
answers that look fine and are wrong. Say `set_store("Moholt")` - or let the
server ask; it raises an MCP elicitation with the real store list when nothing
is set. Every response names the store it was answered against.

Full details, including the environment variable and the strict mode:
[Choosing a store](docs/stores.md).

## Drive it from a skill

These tools are the hands, not the brain. They rank whatever they are asked
about, but nothing here knows that you eat two portions, that Friday is taco
night, or that you avoid ultra-processed food. That belongs in a **skill** - a
standing instruction file Claude loads when the task matches, so "plan next
week" gives the same result as a paragraph of re-explained preferences.


> [!IMPORTANT]
> The example skill below WILL EAT up all your tokens, it's very heavy handed.
There is a worked example in
[`examples/ukemeny/SKILL.md`](examples/ukemeny/SKILL.md): a 7-day dinner
planner with a fixed household size, a quality rule, an approval checkpoint,
and Mealie integration that builds the week from recipes you already have. Copy
the folder to `~/.claude/skills/ukemeny/` and edit the preferences to yours.

How to write your own - what belongs in the skill, and the four things a good
one does: [Driving the server from a skill](docs/skills.md).

## Tools

Tools marked **batch** take a list and resolve it concurrently. Use them that
way - one call per ingredient is the expensive mistake.

| Tool | Purpose |
|---|---|
| `search_products` | Search the catalogue |
| `compare_prices` | Per-unit comparison table with premium vs cheapest |
| `find_best_value` | **batch** - best-value pick per ingredient, with pack-size sanity |
| `check_availability` | **batch** - stock check by EAN, with reasons |
| `get_product_details` | **batch** - ingredients, nutrition, allergens, origin |
| `find_stores` | Selectable stores by name, town, county or GLN |
| `set_store` | Pin a store for the session |
| `set_household` | Pin the pantry and the never-buy list for the session |
| `which_store_script` | JavaScript reporting which store the browser is on |
| `plan_week_menu` | Whole menu → aggregated, priced shopping list |
| `price_cart` | Authoritative total (read-only) |
| `build_basket_script` | JavaScript to set the store and fill the basket |
| `inspect_basket_script` | JavaScript reporting what is in the basket before you fill it |
| `verify_basket_script` | JavaScript that diffs the real basket against what was ordered |
| `clear_basket_script` | JavaScript to empty the basket |

Five more appear when `MEALIE_URL` and `MEALIE_TOKEN` are set -
`mealie_inventory`, `save_week_manifest`, `week_menu_for_planning`,
`apply_week_manifest` and `verify_week_run`, for driving a weekly meal plan
against a Mealie library. Those write with a stored credential, so over HTTP
they are hidden until the request carries `MENY_AUTH_TOKEN`; the grocery tools
above stay open. See [The weekly menu toolset](docs/ukemeny.md) and
[Hosting over HTTP](docs/hosting.md#security--two-tiers-on-one-endpoint).

Search in Norwegian - `kyllingfilet`, not `chicken breast`. The catalogue has no
English in it.

The last step needs a browser: `build_basket_script` returns JavaScript, because
the anonymous meny.no basket lives in `localStorage` and there is no server-side
cart to write to. See [Filling the basket](docs/basket.md).

## Docs

| | |
|---|---|
| [Driving the server from a skill](docs/skills.md) | The intended workflow, and what to put in a skill |
| [Choosing a store](docs/stores.md) | Precedence, elicitation, and where the store list comes from |
| [Ingredient matching](docs/matching.md) | Why avocado oil is not an avocado |
| [Household profile](docs/household.md) | Pantry staples and exclusions |
| [Filling the basket](docs/basket.md) | The browser script, and why it must be one |
| [The weekly menu toolset](docs/ukemeny.md) | Optional Mealie tools: manifest, contract, verification |
| [Hosting over HTTP](docs/hosting.md) | Transport flags, security, and all configuration |
| [Design notes](docs/design-notes.md) | Unit-group comparison, cart totals, count needs, API notes |

## Tests

```bash
python -m unittest discover -s tests
```

Stdlib only, no network, no pytest. Every case in
[`tests/test_matching.py`](tests/test_matching.py) is a product a live search
actually returned and a live ranking actually chose, written as the shopper's
question ("I asked for avokado") rather than as a unit test of a scoring
function.

## Licence

MIT.
