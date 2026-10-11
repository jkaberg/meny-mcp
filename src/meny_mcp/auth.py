"""Gating the write-capable tools behind a shared secret.

The grocery tools are safe to leave open: they are read-only against a public
catalogue and cannot touch anyone's basket on their own. The weekly-menu tools
are not - they hold a Mealie credential and write with it. This module is what
lets both live on one endpoint.

**How the secret travels.** The realistic client here is a hosted connector
that is configured with a URL and nothing else, so a header is not always
available. Three carriers are accepted, best first:

1. ``Authorization: Bearer <token>`` - use this whenever the client can send
   headers. It is the only one that does not end up in a log by default.
2. A secret path segment: ``https://host/mcp/<token>``. The path is rewritten
   back to ``/mcp`` before the MCP app sees it, so the transport is unchanged.
   Proxies log paths, so exclude it from the access log (see docs/hosting.md).
3. ``?token=<token>``. Works everywhere, logged everywhere, and leaks into
   ``Referer`` if a URL ever reaches a browser. Accepted as a last resort.

**What gating means.** An unauthorised caller does not see the gated tools in
``tools/list`` and cannot call them. Hiding rather than merely refusing is
deliberate: a tool that is advertised and always fails wastes the model's
context and invites it to keep trying.

**stdio is exempt.** There is no request to authenticate and no network to
reach it over - whoever launched the process already has the machine and the
environment the Mealie token lives in. Requiring a second secret there would
be friction with nothing on the other side of it.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Awaitable, Callable, Iterable

log = logging.getLogger("meny_mcp.auth")

# Long enough that guessing is not a strategy. A short shared secret on an
# endpoint anyone can reach is worse than no secret, because it reads as
# protection while providing none.
MIN_TOKEN_LENGTH = 24

SCOPE_FLAG = "meny_auth_via_path"


def configured_token() -> str:
    return os.getenv("MENY_AUTH_TOKEN", "").strip()


def token_problem(token: str) -> str | None:
    """Why this token cannot be used, or None if it is fine."""
    if not token:
        return "MENY_AUTH_TOKEN is not set"
    if len(token) < MIN_TOKEN_LENGTH:
        return (f"MENY_AUTH_TOKEN is {len(token)} characters; "
                f"at least {MIN_TOKEN_LENGTH} are required "
                f"(try: python -c \"import secrets;print(secrets.token_urlsafe(32))\")")
    return None


def _matches(candidate: str | None, token: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, token)


def authorised(request: Any, token: str) -> bool:
    """Does this request carry the shared secret?

    ``request`` is the Starlette request the HTTP transport attached, or None
    for stdio - see the module docstring for why None passes.
    """
    if request is None:
        return True
    if not token:
        return False

    header = (request.headers.get("authorization") or "").strip()
    if header.lower().startswith("bearer "):
        if _matches(header[7:].strip(), token):
            return True

    if request.scope.get(SCOPE_FLAG):
        return True

    return _matches(request.query_params.get("token"), token)


def path_unwrapping_app(app: Any, base_path: str, token: str) -> Any:
    """Wrap an ASGI app so ``{base}/{token}`` is served as ``{base}``.

    Done at the ASGI layer rather than by mounting a second route, because the
    MCP app has one endpoint path baked into its own routing: rewriting the
    scope keeps it a single mount that neither knows nor cares that a secret
    was in the URL.
    """
    base = "/" + base_path.strip("/")
    secret_path = f"{base}/{token}"

    async def wrapped(scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path == secret_path or path.startswith(secret_path + "/"):
                scope = dict(scope)
                scope["path"] = base + path[len(secret_path):]
                scope["raw_path"] = scope["path"].encode()
                scope[SCOPE_FLAG] = True
        await app(scope, receive, send)

    return wrapped


def gate(gated_tools: Iterable[str], token: str) -> Callable[..., Awaitable[Any]]:
    """Server middleware hiding and refusing ``gated_tools`` when unauthorised."""
    from mcp.shared.exceptions import MCPError
    from mcp_types import INVALID_REQUEST

    gated = frozenset(gated_tools)

    async def middleware(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        if not gated or authorised(getattr(ctx, "request", None), token):
            return await call_next(ctx)

        if ctx.method == "tools/list":
            result = await call_next(ctx)
            if isinstance(result, dict) and isinstance(result.get("tools"), list):
                result = dict(result)
                result["tools"] = [t for t in result["tools"]
                                   if t.get("name") not in gated]
            return result

        if ctx.method == "tools/call":
            name = (ctx.params or {}).get("name")
            if name in gated:
                raise MCPError(
                    INVALID_REQUEST,
                    f"{name} requires authentication. Connect to this server "
                    "with its authenticated URL, or send the shared secret as "
                    "an Authorization: Bearer header.",
                )

        return await call_next(ctx)

    return middleware


def describe(token: str, path: str, mealie_configured: bool) -> list[str]:
    """Startup lines explaining what is exposed, and to whom."""
    if not mealie_configured:
        return ["weekly-menu tools: off (MEALIE_URL/MEALIE_TOKEN not set)"]
    problem = token_problem(token)
    if problem:
        return [
            f"weekly-menu tools: HIDDEN over HTTP - {problem}",
            "  They write to Mealie, so they stay gated until a token is set. "
            "Set MENY_AUTH_TOKEN and reach the server at "
            f"{path.rstrip('/')}/<token>.",
        ]
    return [
        "weekly-menu tools: gated (Mealie configured, MENY_AUTH_TOKEN set)",
        f"  authenticated endpoint: {path.rstrip('/')}/<token>",
        f"  unauthenticated {path} still serves the grocery tools",
    ]
