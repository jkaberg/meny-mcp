"""Which store a request is about.

Prices, stock and even assortment differ per store, so every call needs one.
There are three ways to say which, in descending priority:

  1. an explicit ``store_id`` argument on the tool call
  2. a store chosen during this MCP session (``set_store``, or the elicitation
     prompt below)
  3. ``MENY_STORE_ID`` in the environment

If none of those apply the server falls back to a hard-coded GLN that is
nobody's actual store, which is worth being loud about: it silently returns
prices from an Oslo shop to a user in Trondheim, and the failure looks like bad
data rather than a misconfiguration.

Session memory
--------------
A choice is remembered for the life of the MCP session.

Finding a stable handle for "the session" takes some care. ``ctx.session`` is a
fresh ``ServerSession`` object on every single call - keying on it (even weakly)
forgets the choice immediately. What is stable:

* **stdio** - the process *is* the session. One client, one server, one store.
* **HTTP** - the ``mcp-session-id`` header identifies the session, so several
  clients sharing a process each keep their own store rather than overwriting
  one another's.
* **stateless HTTP** - there is no session by construction. Everything shares
  the process-wide slot, so pass ``store_id`` explicitly or set
  ``MENY_STORE_ID`` when running that way.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import settings

log = logging.getLogger("meny_mcp.store")

# A GLN is 13 digits. Norwegian grocery GLNs start 70800, but that is not
# guaranteed for every chain, so only the shape is enforced here - the real
# check is that the store endpoint resolves it.
GLN_LENGTH = 13

_PROCESS = "__process__"
_stores: dict[str, str] = {}


def session_key(ctx: Any) -> str:
    """A stable handle for "this session", for anything remembered per client.

    Shared with :mod:`.profile`, which remembers the household's pantry and
    exclusions against the same key.
    """
    headers = getattr(ctx, "headers", None) or {}
    try:
        for name, value in headers.items():
            if name.lower() == "mcp-session-id" and value:
                return str(value)
    except AttributeError:
        pass
    return _PROCESS


_key = session_key


def remember(ctx: Any, store_id: str) -> None:
    _stores[_key(ctx)] = store_id


def chosen(ctx: Any) -> str | None:
    """The store picked during this session, if any."""
    return _stores.get(_key(ctx))


def resolve(ctx: Any, store_id: str | None = None) -> tuple[str, str]:
    """Return ``(store_id, source)`` for this call.

    ``source`` is one of ``argument``, ``session``, ``environment`` or
    ``fallback`` - the last meaning nobody ever said which store this is.
    """
    if store_id:
        return store_id, "argument"
    picked = chosen(ctx)
    if picked:
        return picked, "session"
    if settings.store_configured:
        return settings.store_id, "environment"
    return settings.store_id, "fallback"


def normalise(raw: str) -> str | None:
    """Accept a GLN with spaces or dashes; reject anything not 13 digits."""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    return digits if len(digits) == GLN_LENGTH else None
