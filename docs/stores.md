# Choosing a store

Prices, stock and assortment all differ per store, so a wrong store produces
answers that look fine and are wrong. There is no silent default worth relying
on: with nothing configured the server falls back to a hard-coded GLN that is
somebody else's shop, and says so in every response.

Three ways to set it, highest priority first:

1. **`store_id`** on an individual tool call.
2. **`set_store`** - pins it for the rest of the MCP session. Takes a GLN or a
   name that matches exactly one store (`set_store("Moholt")`), and returns the
   shop's name and address so it can be confirmed. Ambiguous names return the
   candidates rather than guessing.
3. **`MENY_STORE_ID`** in the environment, for a single-user deployment.

If none is set, the server **asks**. Tools that need a store raise an MCP
elicitation - the client shows a picker of the real store list, and the answer
is remembered for the session. Clients that don't support elicitation get a
warning on the response instead; no tool ever fails over this.

Every response carries the store it was answered against, by name:

```json
"store": {"store_id": "7080001271947", "name": "MENY Moholt",
          "city": "Trondheim", "source": "argument"}
```

`source` is `argument`, `session`, `environment` or `fallback`. This exists
because the interesting failure is not "the server guessed wrong" but "nobody
ever checked" - a whole week can be planned against a default while the subject
never comes up, and the first sign is the total at the till. Set
`MENY_REQUIRE_STORE=1` to refuse outright instead: tools then return
`{"error": "no store chosen"}` rather than pricing against the fallback GLN.

Session memory is keyed on the `mcp-session-id` header over HTTP, so several
clients sharing one server each keep their own store. Under stdio the process
is the session. In `--stateless` mode there is no session to remember into -
pass `store_id` or set the environment variable.

## Where the store list comes from

`/api/handoveroptions/{chain}` is the only endpoint that lists stores **with
their GLNs**. It is nominally the click-and-collect options, but its `BUTIKK`
locations are exactly the stores the meny.no basket can switch between, which
makes it the right list to choose from. It covers 48 stores - pickup-enabled
ones only, so a store without collect-in-store will not appear.

Two dead ends worth recording, because both look like they should work:

- `autosuggest?types=stores` returns **CMS content ids** (`72498`), not GLNs.
  Nothing else accepts them. Feeding one into a GLN-scoped call yields
  confident nonsense.
- `/api/stores/{chain}` is a 404, and the public `/butikker` page is
  client-rendered with no ids in the HTML.

`which_store_script` covers the gap: run it on meny.no and it reports the GLN
that browser session is actually using.
