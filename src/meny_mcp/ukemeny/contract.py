"""The ingredient contract, and the two payloads derived from a manifest.

Every rule here is mechanical: a number is a number, a unit is in the table
or it is not, a recipe was served 9 days ago or it was not. Checking them
before the write means a bad recipe never lands in Mealie, so there is no
repair loop.

The Mealie recipe and the Meny basket are both projections of one manifest,
so they cannot drift apart.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
from typing import Any

ALLOWED_UNITS = {
    "gram", "kilogram", "milliliter", "desiliter", "liter",
    "teskje", "spiseskje", "stk", "fedd", "klype",
}
BANNED_UNITS = {
    "boks", "porsjon", "hode", "skvett", "kopp", "unse", "pund", "pint",
    "kvart", "gallon", "væskeunse", "pakke", "neve",
}
REQUIRED_NUTRITION = ("calories", "proteinContent", "carbohydrateContent",
                      "fatContent")
COOLDOWN_DAYS = 14
ASK_FIRST_DAYS = 28
MAX_MINUTES = 40
KCAL_RANGE = (600, 700)


def key(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def index(records: list[dict]) -> dict[str, dict]:
    """name/plural/alias -> {id, name}, so a lookup cannot miss on case alone.

    A library grows a `tomat` / `Tomater` pair precisely when a lookup misses
    and the caller creates what it could not find, so every alias points at the
    same record.
    """
    out: dict[str, dict] = {}
    for rec in records:
        canonical = {"id": rec.get("id"), "name": rec.get("name")}
        keys = [rec.get("name"), rec.get("pluralName"),
                rec.get("abbreviation"), rec.get("slug")]
        for alias in (rec.get("aliases") or []):
            keys.append(alias.get("name") if isinstance(alias, dict) else alias)
        for k in keys:
            if k and key(k) not in out:
                out[key(k)] = canonical
    return out


# Mealie slugifies with unidecode, so a Norwegian tag name loses its diacritics:
# "Høyt Proteininnhold" is stored under "hoyt-proteininnhold". Emitting
# "høyt-proteininnhold" for a tag Mealie has not seen yet creates a second tag
# that differs from the one Mealie would have made, which is the tag version of
# the `tomat` / `Tomater` problem.
_TRANSLITERATE = str.maketrans({"æ": "ae", "ø": "o", "å": "a",
                                "Æ": "ae", "Ø": "o", "Å": "a",
                                "é": "e", "è": "e", "ü": "u", "ö": "o",
                                "ä": "a", "ç": "c", "ñ": "n"})


def slugify(name: str) -> str:
    """Mealie's slug for a name, so a new tag matches what Mealie would make."""
    lowered = (name or "").strip().lower().translate(_TRANSLITERATE)
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", lowered)).strip("-")


def minutes(text: str | None) -> int | None:
    if not text:
        return None
    total, found = 0, False
    for value, word in re.findall(r"(\d+)\s*(t|time[rn]?|min\w*)",
                                  str(text).lower()):
        found = True
        total += int(value) * (60 if word.startswith("t") else 1)
    return total if found else None


def _suggest(name: str, pool: dict) -> str:
    close = difflib.get_close_matches(key(name), list(pool), n=3, cutoff=0.75)
    return ("  did you mean: " + ", ".join(sorted({pool[c]["name"]
                                                   for c in close}))
            if close else "")


def validate(manifest: dict, inventory: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block every write."""
    errors: list[str] = []
    warnings: list[str] = []
    foods = inventory.get("foods", {})
    units = inventory.get("units", {})
    library = {r["slug"]: r for r in inventory.get("recipes", []) if r.get("slug")}
    days = manifest.get("days") or []

    if len(days) != 7:
        errors.append(f"manifest: {len(days)} days, expected 7")
    seen: set[str] = set()

    for day in days:
        date = day.get("date") or "?"
        where = f"{date} {day.get('dish') or '(unnamed)'}"

        if not day.get("dish"):
            errors.append(f"{where}: no dish name")
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            errors.append(f"{where}: date {date!r} is not YYYY-MM-DD")
        if date in seen:
            errors.append(f"{where}: duplicate date")
        seen.add(date)

        source = day.get("source")
        if source not in ("mealie", "new"):
            errors.append(f"{where}: source must be 'mealie' or 'new', "
                          f"got {source!r}")

        if source == "mealie":
            slug = day.get("slug")
            if not slug:
                errors.append(f"{where}: source is 'mealie' but no slug given")
            elif slug not in library:
                errors.append(f"{where}: slug {slug!r} is not in the library")
            else:
                ago = library[slug].get("days_ago")
                if ago is not None and ago <= COOLDOWN_DAYS:
                    errors.append(f"{where}: served {ago} days ago - inside "
                                  f"the {COOLDOWN_DAYS}-day cooldown")

        if day.get("servings", 2) != 2:
            errors.append(f"{where}: servings is {day.get('servings')}, "
                          "expected 2")

        mins = minutes(day.get("total_time"))
        if mins is None:
            errors.append(f"{where}: total_time missing or unparseable "
                          "(want e.g. '35 minutter')")
        elif mins > MAX_MINUTES:
            errors.append(f"{where}: total_time is {mins} min, over the "
                          f"{MAX_MINUTES} min limit")

        if not day.get("tags"):
            errors.append(f"{where}: no tags")

        nutrition = day.get("nutrition") or {}
        absent = [k for k in REQUIRED_NUTRITION if not nutrition.get(k)]
        if absent:
            errors.append(f"{where}: nutrition missing " + ", ".join(absent))
        else:
            try:
                kcal = float(re.sub(r"[^\d.]", "", str(nutrition["calories"])))
                if not KCAL_RANGE[0] <= kcal <= KCAL_RANGE[1]:
                    warnings.append(f"{where}: {kcal:.0f} kcal is outside the "
                                    f"{KCAL_RANGE[0]}-{KCAL_RANGE[1]} target")
            except ValueError:
                errors.append(f"{where}: calories "
                              f"{nutrition['calories']!r} is not a number")

        instructions = day.get("instructions") or []
        if not instructions:
            errors.append(f"{where}: no instructions")
        rows = day.get("ingredients") or []
        if not rows:
            errors.append(f"{where}: no ingredients")

        named: set[str] = set()
        for n, row in enumerate(rows, 1):
            at = f"{where} / ingredient {n}"
            food = (row.get("food") or "").strip()
            unit = (row.get("unit") or "").strip()
            qty = row.get("quantity")

            if not food:
                errors.append(f"{at}: no food")
            else:
                named.add(key(food))
                if food != food.lower():
                    errors.append(f"{at}: food {food!r} must be lowercase")
                if key(food) not in foods:
                    errors.append(f"{at}: food {food!r} is not in Mealie"
                                  + _suggest(food, foods))

            if isinstance(qty, bool) or not isinstance(qty, (int, float)):
                errors.append(f"{at}: quantity {qty!r} must be a number, not "
                              "a string, a range or null")
            elif qty <= 0:
                errors.append(f"{at}: quantity is {qty}, must be > 0")

            if not unit:
                errors.append(f"{at}: no unit - every row needs one, "
                              "garnishes included")
            elif key(unit) in BANNED_UNITS:
                errors.append(f"{at}: unit {unit!r} is banned - use a weight "
                              "or a volume and put the rest in note")
            elif key(unit) not in ALLOWED_UNITS:
                errors.append(f"{at}: unit {unit!r} is not in the allowed "
                              f"table ({', '.join(sorted(ALLOWED_UNITS))})")
            elif key(unit) not in units:
                errors.append(f"{at}: unit {unit!r} does not exist in Mealie "
                              "yet - apply with create_missing to add it")

            note = row.get("note") or ""
            if re.search(r"\d", note):
                errors.append(f"{at}: note {note!r} contains a number - "
                              "amounts belong in quantity, never in the note")

        # Anything the instructions cook with should be on the list. Matched
        # against Mealie's own food vocabulary, so this catches "tilsett
        # vannet" with no water listed without inventing ingredients.
        text = " ".join(str(i) for i in instructions).lower()
        for word in sorted(set(re.findall(r"[a-zæøå]{3,}", text))):
            # Norwegian inflects by suffix, so match the longest prefix that
            # is a known food: "løkringer" is løk, not a new word.
            for end in range(len(word), 2, -1):
                stem = word[:end]
                if stem not in foods:
                    continue
                if stem not in named:
                    warnings.append(f"{where}: instructions mention {word!r} "
                                    f"but {foods[stem]['name']!r} is not an "
                                    "ingredient row")
                break

    return errors, warnings


def ingredient_strings(day: dict) -> list[str]:
    """The plan_week_menu form of a day's rows: '450 gram kyllingfilet'."""
    out = []
    for row in day.get("ingredients") or []:
        qty = row.get("quantity")
        text = f"{qty:g}" if isinstance(qty, (int, float)) else str(qty)
        out.append(f"{text} {row.get('unit')} {row.get('food')}".strip())
    return out


def planning_menu(manifest: dict) -> dict:
    return {day.get("day") or day.get("date"):
            {"name": day.get("dish"), "ingredients": ingredient_strings(day)}
            for day in manifest.get("days") or []}


def recipe_body(day: dict, inventory: dict) -> dict:
    """The complete Mealie PUT body, rebuilt from the manifest every time.

    Mealie's recipe update is a PUT: any field left out is wiped. Generating
    the whole body from one source is what makes "the tags disappeared on the
    second write" unreachable rather than merely discouraged.
    """
    foods = inventory.get("foods", {})
    units = inventory.get("units", {})
    tags = inventory.get("tags", {})

    rows = []
    for row in day.get("ingredients") or []:
        unit = units.get(key(row.get("unit") or ""))
        food = foods.get(key(row.get("food") or ""))
        entry: dict[str, Any] = {"quantity": row.get("quantity")}
        entry["unit"] = ({"id": unit["id"], "name": unit["name"]} if unit
                         else {"name": row.get("unit")})
        entry["food"] = ({"id": food["id"], "name": food["name"]} if food
                         else {"name": row.get("food")})
        if row.get("note"):
            entry["note"] = row["note"]
        rows.append(entry)

    resolved = []
    for tag in day.get("tags") or []:
        name = tag if isinstance(tag, str) else tag.get("name", "")
        known = tags.get(key(name))
        resolved.append({"id": known["id"], "name": known["name"],
                         "slug": known["slug"]} if known
                        else {"name": name, "slug": slugify(name)})

    return {
        "name": day.get("dish"),
        "slug": day.get("slug"),
        "description": day.get("description", ""),
        "recipeServings": day.get("servings", 2),
        "recipeYieldQuantity": day.get("servings", 2),
        "prepTime": day.get("prep_time"),
        "performTime": day.get("perform_time"),
        "totalTime": day.get("total_time"),
        "recipeIngredient": rows,
        "recipeInstructions": [{"text": t} for t in day.get("instructions") or []],
        "tags": resolved,
        "nutrition": {k: str(v) for k, v in (day.get("nutrition") or {}).items()},
        "settings": {"public": True, "showNutrition": True,
                     "showAssets": False, "landscapeView": False,
                     "disableComments": False, "locked": False},
    }


def check_stored_recipe(fresh: dict) -> list[str]:
    """Re-check the contract against what Mealie actually stored."""
    problems: list[str] = []
    rows = fresh.get("recipeIngredient") or []
    if not rows:
        problems.append("no ingredients")
    for n, row in enumerate(rows, 1):
        qty, unit, food = row.get("quantity"), row.get("unit"), row.get("food")
        label = food.get("name") if isinstance(food, dict) else food
        if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0:
            problems.append(f"row {n} ({label or '?'}) quantity {qty!r}")
        unit_name = unit.get("name") if isinstance(unit, dict) else unit
        if not unit_name:
            problems.append(f"row {n} ({label or '?'}) has no unit")
        elif key(unit_name) not in ALLOWED_UNITS:
            problems.append(f"row {n} unit {unit_name!r} not allowed")
        if not label:
            problems.append(f"row {n} has no food")
        if any(ch.isdigit() for ch in (row.get("note") or "")):
            problems.append(f"row {n} note carries a number: {row['note']!r}")
    if fresh.get("recipeServings") not in (2, 2.0):
        problems.append(f"servings is {fresh.get('recipeServings')!r}")
    if not fresh.get("tags"):
        problems.append("no tags")
    nutrition = fresh.get("nutrition") or {}
    absent = [k for k in REQUIRED_NUTRITION if not nutrition.get(k)]
    if absent:
        problems.append("nutrition missing " + ", ".join(absent))
    return problems
