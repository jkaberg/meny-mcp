# Hosting over HTTP

```
meny-mcp --transport http [--host H] [--port N] [--path /mcp]
         [--stateless] [--json-response]
```

| Variable | Default | Meaning |
|---|---|---|
| `MENY_TRANSPORT` | `stdio` | `stdio` or `http` |
| `MENY_HTTP_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` in the container) |
| `MENY_HTTP_PORT` | `8000` | Port |
| `MENY_HTTP_PATH` | `/mcp` | Endpoint path |
| `MENY_HTTP_STATELESS` | `0` | `1` = no session state, for multi-replica setups |
| `MENY_HTTP_JSON_RESPONSE` | `0` | `1` = plain JSON instead of SSE |
| `MENY_HTTP_ALLOWED_HOSTS` | - | Extra `Host` values (comma-separated) |
| `MENY_HTTP_ALLOWED_ORIGINS` | - | Extra `Origin` values (comma-separated) |
| `MENY_STATE_DIR` | `/data` in the image | Where the weekly-menu manifest lives |
| `MEALIE_URL` | - | Enables the [weekly menu toolset](ukemeny.md) |
| `MEALIE_TOKEN` | - | Enables the [weekly menu toolset](ukemeny.md) |
| `MENY_AUTH_TOKEN` | - | Shared secret unlocking the weekly-menu tools over HTTP |

`MENY_STATE_DIR` needs a **persistent volume** if you use the weekly-menu
tools. A container filesystem is ephemeral, and the manifest exists precisely
so a run survives losing its state - losing it on redeploy defeats the point.
The compose file mounts a named volume on `/data` for this.

## Security - two tiers on one endpoint

**The grocery tools are unauthenticated by design.** They are read-only against
a public catalogue and cannot touch anyone's basket on their own, so the worst
an anonymous caller achieves is consuming your rate budget. Leaving them open
is a deliberate choice, not an oversight.

**The weekly-menu tools are not, and are gated.** They hold a Mealie credential
and write with it. Over HTTP they are hidden from `tools/list` and refused on
`tools/call` unless the request carries `MENY_AUTH_TOKEN`. If Mealie is
configured and that token is not set, they are simply unreachable - the
dangerous middle state fails closed rather than quietly serving a credentialed
toolset to the internet. The server says which state it is in at startup.

```bash
MENY_AUTH_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
```

Shorter than 24 characters is refused rather than trusted: a guessable shared
secret on a public endpoint reads as protection while providing none.

### Three ways to present it

| Carrier | Use when |
|---|---|
| `Authorization: Bearer <token>` | The client can send headers. The only one not logged by default. |
| `https://host/mcp/<token>` | The client takes a URL and nothing else - which is the usual case for a hosted connector. |
| `https://host/mcp?token=<token>` | Last resort. Logged everywhere, and leaks via `Referer` if the URL reaches a browser. |

The secret path is rewritten back to `/mcp` before the MCP app sees it, so it
is one mount, not two. The check is a constant-time comparison and runs **per
request** - authorising the handshake does not authorise later calls on the
same session.

### Keep the path out of your access log

Proxies log request paths, so a secret in one ends up on disk. In nginx:

```nginx
location /mcp { access_log off; }
```

Caddy: `log { ... }` with `skip_hosts`, or omit the log directive for that
route. Traefik: set `accessLog.filters` or drop the path field.

### What a leak actually costs

Scope the Mealie API token to a user that owns only the recipe library. Nothing
in this toolset deletes a recipe, so the worst outcome from a leaked
`MENY_AUTH_TOKEN` is a library full of junk recipes and a wrong meal plan -
annoying to clean up, not data loss. Rotate by changing the variable and
restarting; the old URL stops working immediately.

### stdio needs none of this

There is no request to authenticate and no network to reach it over. A process
launched locally already has the machine and the environment the Mealie token
lives in, so the weekly-menu tools are available without `MENY_AUTH_TOKEN`.

The compose file therefore publishes on `127.0.0.1:8000:8000` - loopback only.
Remove the `127.0.0.1:` prefix only when a reverse proxy in front of it
provides TLS and authentication.

DNS-rebinding protection is **on by default** and validates `Host` and
`Origin`. Verified: a request carrying `Origin: https://evil.example.com` is
rejected with **403 Invalid Origin header**. Without this, any web page you
visit could drive a locally bound MCP server.

Behind a proxy, add the hostname clients actually use, or every request fails
with 421. The compose file already does this from `$DOMAIN`; set it directly if
you are not using compose:

```yaml
MENY_HTTP_ALLOWED_HOSTS: "meny-mcp.example.com,meny-mcp.example.com:443"
MENY_HTTP_ALLOWED_ORIGINS: "https://meny-mcp.example.com"
```

`MENY_HTTP_DISABLE_DNS_REBINDING_PROTECTION=1` disables the check entirely -
only appropriate when a trusted proxy already enforces it.

Sessions are held in memory, so the default assumes a single replica. Set
`MENY_HTTP_STATELESS=1` when running several behind a load balancer.

# Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MENY_CHAIN` | `meny` | `meny`, `spar` or `joker` |
| `MENY_STORE_ID` | `7080001150488` | Store GLN; affects price and stock |
| `MENY_REQUIRE_STORE` | `0` | `1` = refuse to answer without a chosen store |
| `MENY_PANTRY` | - | Extra staples to skip (comma-separated) |
| `MENY_PANTRY_REPLACE` | `0` | `1` = `MENY_PANTRY` replaces the shipped list |
| `MENY_EXCLUDE` | - | Ingredients never to buy (comma-separated) |
| `MENY_MAX_CONCURRENCY` | `4` | Parallel requests |
| `MENY_MIN_INTERVAL` | `0.12` | Minimum seconds between requests |
| `MENY_TIMEOUT` | `20` | HTTP timeout |
| `MENY_MAX_RETRIES` | `4` | Attempts per request; honours `Retry-After` |
| `MENY_CACHE_TTL_PRODUCT` | `900` | Product cache seconds |
| `MENY_OVERFETCH` | `1.5` | How far to over-fetch when filtering out-of-stock items |
| `MENY_MAX_OVERFETCH` | `60` | Ceiling on that over-fetch |

A search hit is ~2.4 kB on the wire, so `MENY_OVERFETCH` is the main lever on
network traffic: it exists only to stop the out-of-stock filter shrinking a
result set below the requested limit. Raise it if searches come back short.
