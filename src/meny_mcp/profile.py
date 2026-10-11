"""Household profile: what is already in the cupboard, and what to never buy.

Two facts about a household that no amount of catalogue data can supply, and
that get re-explained in prose every single week until something stores them.

**Pantry.** A week menu that buys honey, sesame seeds, curry powder and Dijon
mustard from scratch every Sunday is not planning a week, it is restocking a
kitchen. A real run bought ~300 kr of exactly that. The shipped default is
deliberately larger than "salt and pepper" - dry spices and long-life
condiments are the things households actually keep - and it is fully
overridable, because a pantry list that cannot be corrected is worse than a
short one.

**Exclusions.** "No prawns and no dill" is an allergy, a dislike or a house
rule; it should not have to be enforced by hand in the menu text. An exclusion
removes the ingredient from the plan *and* refuses products that contain it,
which is the half that manual filtering misses - dill turns up inside dressings
and prawns inside salads.

Both are resolved per MCP session, the same way the store is: an explicit
argument wins, then a choice made with ``set_household`` during this session,
then the environment. See :mod:`.store` for why the session key looks the way
it does.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from .matching import normalise, tokens
from .store import session_key

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
# Staples and long-life goods a household is assumed to own. Anything here is
# left out of a shopping list unless the caller says otherwise.
BASE_PANTRY: frozenset[str] = frozenset({
    # seasoning
    "salt", "havsalt", "pepper", "sort pepper", "hvit pepper", "pepperkorn",
    "krydder", "paprikapulver", "karri", "spisskummen", "kanel", "muskat",
    "chiliflak", "chilipulver", "oregano", "timian", "basilikum", "laurbær",
    "laurbærblad", "gurkemeie", "kardemomme", "nellik", "ingefærpulver",
    "hvitløkspulver", "løkpulver", "buljong", "buljongterning",
    # fats, acids, sweeteners
    "olje", "olivenolje", "rapsolje", "solsikkeolje", "sesamolje", "matolje",
    "eddik", "balsamico", "sitronsaft", "sukker", "brunt sukker", "honning",
    "sirup", "vaniljesukker",
    # baking and dry goods
    "mel", "hvetemel", "maizena", "bakepulver", "natron", "gjær", "havregryn",
    # bottles and jars that outlive a week
    "soyasaus", "fiskesaus", "ostersaus", "sriracha", "tabasco", "sambal",
    "ketchup", "sennep", "dijonsennep", "majones", "worcestershiresaus",
    "tomatpuré", "tomatpure", "sesamfrø", "nøtter",
    # fridge staples
    "smør", "margarin", "vann",
})

_MIN_STEM = 3


def _split(raw: str) -> set[str]:
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _env_pantry() -> tuple[set[str], bool]:
    """``(items, replace)`` from the environment.

    ``MENY_PANTRY`` extends the shipped list; ``MENY_PANTRY_REPLACE=1`` makes
    it the whole list instead, for a household that disagrees with the default.
    """
    items = _split(os.getenv("MENY_PANTRY", ""))
    replace = os.getenv("MENY_PANTRY_REPLACE", "").lower() in ("1", "true", "yes")
    return items, replace


def _env_exclusions() -> set[str]:
    return _split(os.getenv("MENY_EXCLUDE", ""))


# ---------------------------------------------------------------------------
# Session memory
# ---------------------------------------------------------------------------
_pantry: dict[str, tuple[set[str], bool]] = {}
_exclusions: dict[str, set[str]] = {}
_accept_forms: dict[str, set[str]] = {}


def remember(ctx: Any, *, pantry: Iterable[str] | None = None,
             replace_pantry: bool = False,
             exclude: Iterable[str] | None = None,
             accept_forms: Iterable[str] | None = None) -> None:
    key = session_key(ctx)
    if pantry is not None:
        _pantry[key] = ({p.strip().lower() for p in pantry if p.strip()},
                        replace_pantry)
    if exclude is not None:
        _exclusions[key] = {e.strip().lower() for e in exclude if e.strip()}
    if accept_forms is not None:
        _accept_forms[key] = {f.strip().lower() for f in accept_forms
                              if f.strip()}


def forget(ctx: Any) -> None:
    key = session_key(ctx)
    _pantry.pop(key, None)
    _exclusions.pop(key, None)
    _accept_forms.pop(key, None)


def pantry_for(ctx: Any, extra: Iterable[str] | None = None) -> set[str]:
    """The effective pantry for this call."""
    session, replace = _pantry.get(session_key(ctx), (set(), False))
    if not session:
        session, replace = _env_pantry()
    out = set(session) if replace else set(BASE_PANTRY) | set(session)
    out |= {e.strip().lower() for e in (extra or []) if e.strip()}
    return out


def exclusions_for(ctx: Any, extra: Iterable[str] | None = None) -> set[str]:
    """Ingredients this household will not buy, for this call."""
    out = set(_exclusions.get(session_key(ctx)) or _env_exclusions())
    out |= {e.strip().lower() for e in (extra or []) if e.strip()}
    return out


def accept_forms_for(ctx: Any, extra: Iterable[str] | None = None) -> list[str]:
    """Forms this household accepts without asking for them.

    Kept beside the pantry because it is the same kind of fact: a standing
    property of the household, not of any one menu. "We are happy with frozen
    fish and vegetables" otherwise has to be re-expressed as the word "frossen"
    in every ingredient line, which additionally penalises the fresh products
    for *not* being frozen - the opposite of what was meant.
    """
    out = set(_accept_forms.get(session_key(ctx))
              or _split(os.getenv("MENY_ACCEPT_FORMS", "")))
    out |= {f.strip().lower() for f in (extra or []) if f.strip()}
    return sorted(out)


def describe(ctx: Any) -> dict[str, Any]:
    """What the profile currently is, for echoing back to the user."""
    session, replace = _pantry.get(session_key(ctx), (set(), False))
    return {
        "pantry_size": len(pantry_for(ctx)),
        "pantry_custom": sorted(session) or None,
        "pantry_replaces_default": replace or None,
        "exclude": sorted(exclusions_for(ctx)) or None,
        "accept_forms": accept_forms_for(ctx) or None,
    }


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def matches(name: str, entries: Iterable[str]) -> str | None:
    """The entry that covers ``name``, if any.

    Exact-token matching alone is too literal for Norwegian compounds: a pantry
    holding "karri" should cover "karripulver", and one holding "sennep" should
    cover "dijonsennep".

    But a compound edge only counts *within a single word*, and only with a
    real stem on the other side. Matching across a phrase makes "salt" cover
    "saltet torsk" - a menu that quietly stops buying the fish - and a
    two-letter remainder makes it cover "saltet" outright.
    """
    target = normalise(name).strip()
    if not target:
        return None
    words = tokens(target)
    for entry in entries:
        needle = normalise(entry).strip()
        if not needle:
            continue
        if target == needle or needle in words:
            return entry
        if " " in needle:
            # A multi-word entry ("sort pepper") only matches as a phrase.
            if needle in target:
                return entry
            continue
        for word in words:
            if len(word) >= len(needle) + _MIN_STEM and (
                word.startswith(needle) or word.endswith(needle)
            ):
                return entry
    return None
