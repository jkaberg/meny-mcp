"""The ukemeny toolset: the mechanical half of the weekly run.

Registered on the main server only when Mealie is configured. These tools
do everything mechanical - inventory, validation, building a complete PUT
body, reading back what landed. Whether a dish is good or a week is varied is
left to the caller.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..config import settings
from . import contract, state
from .mealie import Mealie, MealieError


def _error(exc: Exception) -> dict[str, Any]:
    return {"error": str(exc), "type": type(exc).__name__}


def _need_inventory() -> tuple[dict | None, dict | None]:
    inventory = state.load_inventory()
    if inventory is None:
        return None, {"error": "no inventory yet",
                      "fix": "call mealie_inventory first - it resolves the "
                             "unit, food and tag ids everything else needs"}
    return inventory, None


def _need_manifest() -> tuple[dict | None, dict | None]:
    manifest = state.load_manifest()
    if manifest is None:
        return None, {"error": "no week manifest yet",
                      "fix": "call save_week_manifest with the approved menu"}
    return manifest, None


def _status(days_ago: int | None) -> str:
    if days_ago is None:
        return "never served"
    if days_ago <= contract.COOLDOWN_DAYS:
        return f"blocked ({days_ago}d)"
    if days_ago <= contract.ASK_FIRST_DAYS:
        return f"on request ({days_ago}d)"
    return f"free ({days_ago}d)"


async def mealie_inventory(days: int = 90, limit: int = 40) -> dict[str, Any]:
    api = Mealie()
    today = dt.date.today()
    recipes = await api.recipes()
    plans = await api.mealplans(
        (today - dt.timedelta(days=days)).isoformat(),
        (today + dt.timedelta(days=14)).isoformat())
    units, foods, tags = await api.units(), await api.foods(), await api.tags()

    last_plan: dict[str, str] = {}
    upcoming: list[dict] = []
    for entry in plans:
        date = (entry.get("date") or "")[:10]
        rid = entry.get("recipeId") or (entry.get("recipe") or {}).get("id")
        if not date or not rid:
            continue
        if date > today.isoformat():
            upcoming.append({"date": date,
                             "recipe": (entry.get("recipe") or {}).get("name")
                             or entry.get("title") or rid})
        elif date > last_plan.get(rid, ""):
            last_plan[rid] = date

    rows = []
    for rec in recipes:
        rid = rec.get("id")
        served = max(filter(None, [last_plan.get(rid, ""),
                                   (rec.get("lastMade") or "")[:10]]),
                     default="")
        rows.append({"name": rec.get("name"), "slug": rec.get("slug"),
                     "id": rid, "last_served": served or None,
                     "days_ago": (today - dt.date.fromisoformat(served)).days
                     if served else None})
    # Never-served first, then longest-ago first: that ordering *is* the
    # shortlist, so nothing downstream re-sorts or re-reasons about it.
    rows.sort(key=lambda r: (r["days_ago"] is not None, -(r["days_ago"] or 0)))

    path = state.save_inventory({
        "generated": today.isoformat(),
        "units": contract.index(units),
        "foods": contract.index(foods),
        "tags": {contract.key(t.get("name", "")): {
            "id": t.get("id"), "name": t.get("name"), "slug": t.get("slug")}
            for t in tags},
        "recipes": rows,
    })

    eligible = [r for r in rows if r["days_ago"] is None
                or r["days_ago"] > contract.COOLDOWN_DAYS]
    out: dict[str, Any] = {
        "library": {"recipes": len(rows), "eligible_to_reuse": len(eligible),
                    "units": len(units), "foods": len(foods),
                    "tags": len(tags)},
        # The id maps are lookup data. They stay in the file and are resolved
        # server-side; carrying several hundred of them through the
        # conversation is what degrades the steps at the end of this run.
        "id_maps": f"resolved and saved to {path} - do not ask for them",
        "reuse_shortlist": [
            {"slug": r["slug"], "name": r["name"],
             "last_served": r["last_served"], "status": _status(r["days_ago"])}
            for r in rows[:limit]],
    }
    if len(rows) > limit:
        out["shortlist_note"] = (f"{len(rows) - limit} older recipes not "
                                 "listed; these are the least recently served")
    if upcoming:
        out["already_scheduled"] = sorted(upcoming, key=lambda e: e["date"])
        out["already_scheduled_note"] = "do not double-book these days"
    return out


async def save_week_manifest(days: list[dict], week_start: str | None = None,
                             store: dict | None = None,
                             replace: bool = False) -> dict[str, Any]:
    inventory, missing = _need_inventory()
    if missing:
        return missing

    existing = state.load_manifest() or {}
    if replace or not existing.get("days"):
        merged = list(days)
    else:
        # Merge by date so fixing one day does not mean re-sending seven.
        by_date = {d.get("date"): dict(d) for d in existing.get("days") or []}
        for day in days:
            by_date[day.get("date")] = {**by_date.get(day.get("date"), {}),
                                        **day}
        merged = [by_date[k] for k in sorted(by_date)]

    manifest = {
        "week_start": week_start or existing.get("week_start")
        or (merged[0].get("date") if merged else None),
        "store": store or existing.get("store"),
        "days": merged,
        "steps_done": existing.get("steps_done", []),
    }
    path = state.save_manifest(manifest)
    errors, warnings = contract.validate(manifest, inventory)
    out: dict[str, Any] = {
        "saved_to": path,
        "days": len(merged),
        "valid": not errors,
    }
    if warnings:
        out["warnings"] = warnings
    if errors:
        out["errors"] = errors
        out["next"] = ("fix these and call save_week_manifest again with just "
                       "the corrected days - nothing has been written to "
                       "Mealie or the basket")
    else:
        out["next"] = ("contract-clean. Present the menu to the user and wait "
                       "for approval before calling apply_week_manifest")
    return out


async def week_menu_for_planning() -> dict[str, Any]:
    manifest, missing = _need_manifest()
    if missing:
        return missing
    return {
        "menu": contract.planning_menu(manifest),
        "servings_scale": 1,
        "note": ("pass this straight to plan_week_menu. These are the same "
                 "ingredient strings Mealie holds, which is what stops the "
                 "recipes and the basket from drifting apart - do not retype "
                 "them"),
    }


async def apply_week_manifest(create_missing: bool = False,
                              dry_run: bool = False) -> dict[str, Any]:
    inventory, missing = _need_inventory()
    if missing:
        return missing
    manifest, missing = _need_manifest()
    if missing:
        return missing
    api = Mealie()

    # Foods and units the manifest names but Mealie lacks. Creating one
    # because a lookup missed on case or plural is how a library grows
    # `tomat`, `tomat ` and `Tomater`, so it is opt-in.
    want_foods, want_units = set(), set()
    for day in manifest.get("days") or []:
        for row in day.get("ingredients") or []:
            if row.get("food") and contract.key(row["food"]) not in inventory["foods"]:
                want_foods.add(row["food"])
            if row.get("unit") and contract.key(row["unit"]) not in inventory["units"]:
                want_units.add(row["unit"])
    if (want_foods or want_units) and not create_missing:
        return {"blocked": "Mealie has no entry for these",
                "missing_foods": sorted(want_foods),
                "missing_units": sorted(want_units),
                "next": ("check the near-matches save_week_manifest reported "
                         "first; if none of these is an existing entry under "
                         "another spelling, call again with "
                         "create_missing=true")}
    if create_missing and not dry_run:
        for name in sorted(want_units):
            rec = await api.create_unit(name)
            inventory["units"][contract.key(name)] = {"id": rec["id"],
                                                      "name": rec["name"]}
        for name in sorted(want_foods):
            rec = await api.create_food(name)
            inventory["foods"][contract.key(name)] = {"id": rec["id"],
                                                      "name": rec["name"]}
        state.save_inventory(inventory)

    errors, _ = contract.validate(manifest, inventory)
    if errors:
        return {"refused": "the manifest is not contract-clean",
                "errors": errors,
                "note": "nothing has been written"}

    days = manifest.get("days") or []
    if dry_run:
        return {"dry_run": True,
                "would_write": [{"date": d.get("date"), "dish": d.get("dish"),
                                 "slug": d.get("slug"), "action":
                                 "update" if d.get("slug") else "create"}
                                for d in days],
                "would_create": {"foods": sorted(want_foods),
                                 "units": sorted(want_units)}}

    recipes_out, plan_out, failures = [], [], []
    shells: list[str] = []
    for day in days:
        slug = day.get("slug")
        created_here = False
        try:
            action = "updated"
            if not slug:
                slug = await api.create_recipe(day.get("dish"))
                # Record the slug on the manifest immediately. Mealie's create
                # yields an empty shell that only the following write fills in,
                # so if that write fails the slug is the only thing that lets a
                # re-run repair the shell instead of stranding it.
                day["slug"] = slug
                created_here = True
                action = "created"
            body = contract.recipe_body(day, inventory)
            body["slug"] = slug
            await api.update_recipe(slug, body)
            fresh = await api.recipe(slug)
            day["recipe_id"] = fresh.get("id")
            recipes_out.append({"slug": slug, "action": action})
        except MealieError as exc:
            failures.append({"dish": day.get("dish"), "slug": slug,
                             "error": str(exc)})
            if created_here:
                shells.append(slug)

    # Persist before touching the meal plan: a crash after this point must not
    # cost the slugs, or the next run creates a second copy of everything.
    state.save_manifest(manifest)

    dates = sorted(d["date"] for d in days if d.get("date"))
    existing = await api.mealplans(dates[0], dates[-1]) if dates else []
    already = {((e.get("date") or "")[:10],
                e.get("recipeId") or (e.get("recipe") or {}).get("id"))
               for e in existing}
    for day in days:
        date, rid = day.get("date"), day.get("recipe_id")
        if not rid:
            failures.append({"date": date, "error": "no recipe id - the "
                                                    "recipe write failed"})
            continue
        if (date, rid) in already:
            plan_out.append({"date": date, "slug": day.get("slug"),
                             "action": "already present"})
            continue
        try:
            await api.create_mealplan(date, rid)
            plan_out.append({"date": date, "slug": day.get("slug"),
                             "action": "added"})
        except MealieError as exc:
            failures.append({"date": date, "error": str(exc)})

    for step in ("recipes", "mealplan"):
        if step not in manifest.setdefault("steps_done", []):
            manifest["steps_done"].append(step)
    state.save_manifest(manifest)

    out: dict[str, Any] = {"recipes": recipes_out, "week_plan": plan_out,
                           "next": ("call verify_week_run. This tool reports "
                                    "what it sent; only that one reports what "
                                    "Mealie actually holds")}
    if failures:
        out["failures"] = failures
        out["next"] = ("fix the cause, then call apply_week_manifest again - "
                       "it is idempotent and will finish what failed")
    if shells:
        out["empty_recipes_awaiting_retry"] = shells
        out["empty_recipes_note"] = (
            "these were created but not filled in, because the write that "
            "follows the create failed. Their slugs are saved on the "
            "manifest, so re-running this tool fills them in rather than "
            "creating duplicates. Do not report the week as done until "
            "verify_week_run passes.")
    return out


async def verify_week_run(basket_result: Any = None) -> dict[str, Any]:
    """Read back what actually landed, and return one PASS/FAIL verdict.

    The trust boundary of the whole workflow. Everything upstream reports what
    it *sent*; this reports what the systems now *hold*, fetched fresh. It
    deliberately does not share a code path with the writer - a verifier built
    on the writer's assumptions verifies nothing.
    """
    manifest, missing = _need_manifest()
    if missing:
        return missing
    api = Mealie()
    days = manifest.get("days") or []
    checks: list[dict] = []
    lines: list[str] = []

    def check(ok: bool, section: str, what: str, detail: str = "") -> None:
        checks.append({"section": section, "check": what, "pass": ok,
                       "detail": detail})
        status = "PASS" if ok else "FAIL"
        lines.append(f"  [{status}] {what}" + (f"    {detail}" if detail else ""))

    lines.append("recipes in Mealie")
    ids: dict[str, str] = {}
    for day in days:
        slug, dish = day.get("slug"), day.get("dish") or "(unnamed)"
        if not slug:
            check(False, "recipes", dish, "never written - no slug")
            continue
        try:
            fresh = await api.recipe(slug)
        except MealieError as exc:
            check(False, "recipes", slug, str(exc)[:80])
            continue
        ids[day.get("date", "")] = fresh.get("id")
        problems = contract.check_stored_recipe(fresh)
        check(not problems, "recipes", slug,
              "; ".join(problems[:3]) if problems
              else f"{len(fresh.get('recipeIngredient') or [])} ingredients")

    lines.append("week plan")
    dates = sorted(d["date"] for d in days if d.get("date"))
    scheduled: dict[str, set] = {}
    if dates:
        for entry in await api.mealplans(dates[0], dates[-1]):
            date = (entry.get("date") or "")[:10]
            rid = entry.get("recipeId") or (entry.get("recipe") or {}).get("id")
            scheduled.setdefault(date, set()).add(rid)
    for day in days:
        date = day.get("date", "")
        want = day.get("recipe_id") or ids.get(date)
        got = scheduled.get(date, set())
        label = f"{date} {day.get('slug') or day.get('dish')}"
        if not got:
            check(False, "week_plan", label, "nothing scheduled on this date")
        elif want and want in got:
            check(True, "week_plan", label)
        else:
            check(False, "week_plan", label,
                  "a different recipe is scheduled here")

    lines.append("meny.no basket")
    if basket_result is None:
        check(False, "basket", "basket",
              "not checked - run verify_basket_script on meny.no and pass "
              "its result here")
    else:
        raw = basket_result
        if isinstance(raw, str):
            import json
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        basket = (raw or {}).get("basket") or {}
        verdict = basket.get("verdict")
        mode = basket.get("mode")
        check(verdict == "complete", "basket", "basket contents",
              f"{basket.get('correct_lines', '?')}/"
              f"{basket.get('expected_lines', '?')} lines"
              + (f" ({mode})" if mode else "")
              + ("" if verdict == "complete" else f" - {verdict}"))
        for line in basket.get("unexpected") or []:
            if mode == "replace":
                check(False, "basket", f"  not cleared {line.get('ean')}",
                      str(line.get("title") or "")[:40])
        for line in basket.get("missing") or []:
            check(False, "basket", f"  missing {line.get('ean')}",
                  f"expected {line.get('expected')}")
        for line in basket.get("wrong_quantity") or []:
            check(False, "basket", f"  wrong qty {line.get('ean')}",
                  f"want {line.get('expected')}, got {line.get('actual')}")
        expected_gln = (manifest.get("store") or {}).get("gln")
        if expected_gln:
            check(str((raw or {}).get("store_gln")) == str(expected_gln),
                  "basket", "store",
                  f"{(raw or {}).get('store_name')} "
                  f"({(raw or {}).get('store_gln')})")

    failed = [c for c in checks if not c["pass"]]
    verdict = "PASS" if not failed else "FAIL"
    lines.append("")
    lines.append(f"{verdict} - {len(checks) - len(failed)}/{len(checks)} checks"
                 + ("" if not failed else
                    ". The week is NOT done; fix these and re-run "
                    "apply_week_manifest, then this."))
    return {
        "verdict": verdict,
        "report": "\n".join(lines),
        "failed": failed,
        "show_the_user": ("paste the report verbatim. This output is the "
                          "deliverable; 'I checked and it looks right' is not"),
    }


# The tools that write to Mealie with a stored credential. `meny_mcp.auth`
# hides and refuses these for unauthenticated HTTP callers; everything else on
# this server is read-only against a public catalogue and stays open.
TOOL_NAMES = ("mealie_inventory", "save_week_manifest", "week_menu_for_planning",
              "apply_week_manifest", "verify_week_run")


def register(server: Any) -> None:
    """Attach the toolset. Called only when Mealie is configured."""
    if not settings.mealie_configured:
        return

    server.tool(
        description=(
            "Step 0 of a weekly menu run: take inventory of Mealie. Resolves "
            "every unit, food and tag id and saves them server-side, then "
            "returns only what needs judgement - the reuse shortlist, least "
            "recently served first, with each recipe's cooldown status "
            "already computed. Do NOT ask for the id maps or call Mealie's "
            "list_foods/list_units yourself: several hundred ids in the "
            "conversation is what degrades the later steps of this run."
        ),
    )(mealie_inventory)

    server.tool(
        description=(
            "Save the week's menu as the plan of record and check it against "
            "the ingredient contract. days is a list of 7 day objects "
            "(date, day, dish, source 'mealie'|'new', slug, servings, "
            "prep_time, perform_time, total_time, description, tags, "
            "nutrition, instructions, ingredients). Ingredient rows use "
            "NAMES, not ids: {quantity: 450, unit: 'gram', food: "
            "'kyllingfilet', note: 'i biter'}. Returns valid:true or a list "
            "of precise errors - quantities that are not numbers, banned "
            "units, amounts hidden in notes, foods Mealie does not have "
            "(with near-matches), recipes inside their cooldown. Nothing is "
            "written to Mealie or the basket. Call again with only the "
            "corrected days to fix errors; days merge by date."
        ),
    )(save_week_manifest)

    server.tool(
        description=(
            "Return the saved manifest as a plan_week_menu argument. Use this "
            "instead of retyping the ingredients: it guarantees the basket is "
            "priced for exactly what the recipes say."
        ),
    )(week_menu_for_planning)

    server.tool(
        description=(
            "Write the approved manifest into Mealie: creates and updates the "
            "recipes, then schedules all 7 meal plan entries. Idempotent - "
            "re-run it after any interruption and it finishes the job without "
            "creating anything twice. The PUT body is regenerated complete "
            "from the manifest every time, so no write can wipe tags or "
            "nutrition. Refuses to run if the manifest is not contract-clean. "
            "Stops and lists anything Mealie has no entry for until "
            "create_missing=true. Only call this AFTER the user has approved "
            "the menu."
        ),
    )(apply_week_manifest)

    server.tool(
        description=(
            "The gate. Fetches every recipe and meal plan entry back from "
            "Mealie and folds in the browser's basket diff, then returns one "
            "PASS/FAIL report. Pass the result of the meny verify_basket_"
            "script tool as basket_result; without it the basket cannot be "
            "confirmed and the verdict is FAIL. You may NOT report the week "
            "as done without this returning PASS - show the user the report."
        ),
    )(verify_week_run)
