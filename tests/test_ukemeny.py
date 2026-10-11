"""End-to-end test of the ukemeny toolset against a fake Mealie.

A validator that wrongly passes, or a verifier that reports PASS on an
empty week plan, is worse than no check at all, because the run is trusted.

The fake below is a real HTTP server speaking the endpoints the client
uses, and the assertions are the failures that actually happened in
production: a recipe whose tags were wiped by a second write, and a week plan
reported as scheduled while it was in fact empty.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from meny_mcp.config import settings  # noqa: E402
from meny_mcp.ukemeny import contract, mealie, state, tools  # noqa: E402

UNITS = [{"id": "u1", "name": "gram"}, {"id": "u2", "name": "stk"},
         {"id": "u3", "name": "desiliter"}]
FOODS = [{"id": "f1", "name": "kyllingfilet"}, {"id": "f2", "name": "løk"},
         {"id": "f3", "name": "fløte"}, {"id": "f4", "name": "vann"},
         {"id": "f5", "name": "tomat"}]
TAGS = [{"id": "t1", "name": "Ukemeny", "slug": "ukemeny"}]


class FakeMealie(BaseHTTPRequestHandler):
    recipes: dict = {}
    mealplans: list = []
    reject_mealplan_for: set = set()
    reject_write_for: set = set()

    def log_message(self, *a):
        pass

    def _send(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self, items):
        self._send({"items": items, "page": 1, "total_pages": 1})

    def _body(self):
        return json.loads(self.rfile.read(
            int(self.headers.get("Content-Length") or 0)) or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/units":
            return self._page(UNITS)
        if path == "/api/foods":
            return self._page(FOODS)
        if path == "/api/organizers/tags":
            return self._page(TAGS)
        if path == "/api/recipes":
            return self._page(list(self.recipes.values()))
        if path.startswith("/api/recipes/"):
            slug = path.rsplit("/", 1)[-1]
            if slug not in self.recipes:
                return self._send({"detail": "not found"}, 404)
            return self._send(self.recipes[slug])
        if path == "/api/households/mealplans":
            return self._page(self.mealplans)
        return self._send({"detail": "unhandled " + path}, 404)

    def do_POST(self):
        body = self._body()
        if self.path == "/api/recipes":
            slug = body["name"].lower().replace(" ", "-")
            self.recipes[slug] = {"id": "r-" + slug, "slug": slug,
                                  "name": body["name"], "recipeIngredient": [],
                                  "tags": [], "nutrition": {}}
            return self._send(slug)
        if self.path in ("/api/units", "/api/foods"):
            return self._send({"id": "new-" + body["name"],
                               "name": body["name"]})
        if self.path == "/api/households/mealplans":
            if body.get("date") in self.reject_mealplan_for:
                return self._send({"detail": "nope"}, 500)
            self.mealplans.append({"date": body["date"],
                                   "recipeId": body["recipeId"]})
            return self._send(self.mealplans[-1], 201)
        return self._send({"detail": "unhandled"}, 404)

    def do_PUT(self):
        """Mealie's PUT, including the trap.

        A PUT applies the submitted object field by field, so one that omits
        `id` writes NULL over a NOT NULL column. Mealie turns the resulting
        SQL integrity error into HTTP 400 "Recipe already exists" - a message
        that names neither the cause nor the field, and that fires even when
        updating a recipe that plainly exists. Reproduced against Mealie 3.22
        on 2026-08-20; kept here so nothing quietly goes back to PUT.
        """
        body = self._body()
        if self.path.startswith("/api/recipes/"):
            slug = self.path.rsplit("/", 1)[-1]
            if not body.get("id"):
                return self._send(
                    {"detail": {"message": "Recipe already exists",
                                "error": True, "exception": None}}, 400)
            self.recipes[slug] = {**body, "id": "r-" + slug, "slug": slug}
            return self._send(self.recipes[slug])
        return self._send({"detail": "unhandled"}, 404)

    def do_PATCH(self):
        """Merge the submitted fields; leave everything else alone."""
        body = self._body()
        if self.path.startswith("/api/recipes/"):
            slug = self.path.rsplit("/", 1)[-1]
            if slug not in self.recipes:
                return self._send({"detail": "not found"}, 404)
            if slug in self.reject_write_for:
                return self._send(
                    {"detail": {"message": "Recipe already exists",
                                "error": True}}, 400)
            self.recipes[slug] = {**self.recipes[slug], **body,
                                  "id": "r-" + slug, "slug": slug}
            return self._send(self.recipes[slug])
        return self._send({"detail": "unhandled"}, 404)


def make_day(n: int, **over) -> dict:
    day = {
        "date": f"2026-08-{25 + n:02d}", "day": f"Dag{n}",
        "dish": f"Rett {n}", "source": "new", "servings": 2,
        "prep_time": "10 minutter", "perform_time": "25 minutter",
        "total_time": "35 minutter", "description": "God mat.",
        "tags": ["Ukemeny"],
        "nutrition": {"calories": "650", "proteinContent": "48",
                      "carbohydrateContent": "40", "fatContent": "22"},
        "instructions": ["Brun kyllingen.", "Tilsett fløten."],
        "ingredients": [
            {"quantity": 450, "unit": "gram", "food": "kyllingfilet",
             "note": "i biter"},
            {"quantity": 2, "unit": "stk", "food": "løk", "note": "finhakket"},
            {"quantity": 2, "unit": "desiliter", "food": "fløte"},
        ],
    }
    day.update(over)
    return day


WEEK = [make_day(i) for i in range(7)]


class ToolRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeMealie)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeMealie.recipes = {}
        FakeMealie.mealplans = []
        FakeMealie.reject_mealplan_for = set()
        FakeMealie.reject_write_for = set()
        self.dir = tempfile.mkdtemp()
        fake = dataclasses.replace(settings, mealie_url=self.url,
                                   mealie_token="test", state_dir=self.dir)
        for module in (state, mealie, tools):
            patch = mock.patch.object(module, "settings", fake)
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, coro):
        return asyncio.run(coro)

    def full_run(self, days=None):
        self.call(tools.mealie_inventory())
        saved = self.call(tools.save_week_manifest(days or WEEK,
                                                   week_start="2026-08-25"))
        applied = self.call(tools.apply_week_manifest())
        return saved, applied

    # -- inventory ---------------------------------------------------------
    def test_inventory_keeps_the_id_maps_out_of_the_response(self):
        out = self.call(tools.mealie_inventory())
        self.assertEqual(out["library"]["foods"], 5)
        blob = json.dumps(out)
        for ident in ("f1", "u1", "t1"):
            self.assertNotIn(f'"{ident}"', blob)
        saved = json.loads(
            (pathlib.Path(self.dir) / "inventory.json").read_text())
        self.assertEqual(saved["foods"]["kyllingfilet"]["id"], "f1")

    def test_cooldown_status_is_precomputed(self):
        FakeMealie.recipes = {"gammel": {"id": "r1", "slug": "gammel",
                                         "name": "Gammel",
                                         "lastMade": "2020-01-01"}}
        out = self.call(tools.mealie_inventory())
        self.assertIn("free", out["reuse_shortlist"][0]["status"])

    # -- the contract gate -------------------------------------------------
    def test_a_clean_week_validates(self):
        self.call(tools.mealie_inventory())
        out = self.call(tools.save_week_manifest(WEEK, week_start="2026-08-25"))
        self.assertTrue(out["valid"], out.get("errors"))

    def test_every_contract_breach_is_named(self):
        self.call(tools.mealie_inventory())
        broken = [make_day(0, ingredients=[
            {"quantity": "2", "unit": "boks", "food": "Tomater",
             "note": "1 stk hakket"},
            {"quantity": 0, "unit": "", "food": "fløte"}])] + \
            [make_day(i) for i in range(1, 7)]
        out = self.call(tools.save_week_manifest(broken,
                                                 week_start="2026-08-25"))
        self.assertFalse(out["valid"])
        joined = " | ".join(out["errors"])
        self.assertIn("must be a number", joined)
        self.assertIn("'boks' is banned", joined)
        self.assertIn("must be lowercase", joined)
        self.assertIn("did you mean: tomat", joined)
        self.assertIn("contains a number", joined)
        self.assertIn("quantity is 0", joined)
        self.assertIn("no unit", joined)

    def test_an_ingredient_only_the_instructions_mention_is_flagged(self):
        self.call(tools.mealie_inventory())
        day = make_day(0, instructions=["Brun kyllingen.", "Tilsett vannet."])
        out = self.call(tools.save_week_manifest(
            [day] + [make_day(i) for i in range(1, 7)],
            week_start="2026-08-25"))
        self.assertTrue(any("vann" in w for w in out["warnings"]),
                        out.get("warnings"))

    def test_a_recipe_inside_its_cooldown_is_refused(self):
        import datetime as dt
        recent = (dt.date.today() - dt.timedelta(days=5)).isoformat()
        FakeMealie.recipes = {"nylig": {"id": "r1", "slug": "nylig",
                                        "name": "Nylig", "lastMade": recent}}
        self.call(tools.mealie_inventory())
        day = make_day(0, source="mealie", slug="nylig")
        out = self.call(tools.save_week_manifest(
            [day] + [make_day(i) for i in range(1, 7)],
            week_start="2026-08-25"))
        self.assertFalse(out["valid"])
        self.assertTrue(any("cooldown" in e for e in out["errors"]))

    def test_days_merge_by_date_so_one_fix_is_one_call(self):
        self.call(tools.mealie_inventory())
        self.call(tools.save_week_manifest(WEEK, week_start="2026-08-25"))
        out = self.call(tools.save_week_manifest([make_day(3, dish="Byttet")]))
        self.assertEqual(out["days"], 7)
        manifest = state.load_manifest()
        self.assertEqual(
            [d["dish"] for d in manifest["days"] if d["date"] == "2026-08-28"],
            ["Byttet"])

    def test_nothing_is_written_while_the_manifest_is_invalid(self):
        self.call(tools.mealie_inventory())
        self.call(tools.save_week_manifest(
            [make_day(0, tags=[])] + [make_day(i) for i in range(1, 7)],
            week_start="2026-08-25"))
        out = self.call(tools.apply_week_manifest())
        self.assertIn("refused", out)
        self.assertEqual(FakeMealie.recipes, {})

    # -- writing -----------------------------------------------------------
    def test_a_full_run_lands_and_verifies(self):
        self.full_run()
        out = self.call(tools.verify_week_run(basket_result={
            "store_gln": "708", "basket": {"verdict": "complete",
                                           "correct_lines": 12,
                                           "expected_lines": 12}}))
        self.assertEqual(out["verdict"], "PASS", out["report"])

    def test_every_recipe_keeps_its_tags_and_nutrition(self):
        self.full_run()
        for slug, stored in FakeMealie.recipes.items():
            self.assertTrue(stored["tags"], f"{slug} lost its tags")
            self.assertEqual(stored["nutrition"]["calories"], "650")
            self.assertEqual(stored["recipeIngredient"][0]["unit"]["id"], "u1")
            self.assertEqual(stored["recipeIngredient"][0]["food"]["id"], "f1")

    def test_rerunning_creates_nothing_twice(self):
        self.full_run()
        before = (len(FakeMealie.recipes), len(FakeMealie.mealplans))
        self.call(tools.apply_week_manifest())
        self.assertEqual((len(FakeMealie.recipes), len(FakeMealie.mealplans)),
                         before)

    def test_unknown_foods_stop_the_write_until_confirmed(self):
        self.call(tools.mealie_inventory())
        day = make_day(0, ingredients=[{"quantity": 100, "unit": "gram",
                                        "food": "sellerirot"}])
        self.call(tools.save_week_manifest(
            [day] + [make_day(i) for i in range(1, 7)],
            week_start="2026-08-25"))
        blocked = self.call(tools.apply_week_manifest())
        self.assertIn("sellerirot", blocked["missing_foods"])
        self.assertEqual(FakeMealie.recipes, {})
        out = self.call(tools.apply_week_manifest(create_missing=True))
        self.assertEqual(len(out["recipes"]), 7)

    # -- the gate ----------------------------------------------------------
    def test_an_empty_week_plan_is_reported_as_failure(self):
        self.full_run()
        FakeMealie.mealplans = []          # the production failure, exactly
        out = self.call(tools.verify_week_run(basket_result={
            "basket": {"verdict": "complete"}}))
        self.assertEqual(out["verdict"], "FAIL")
        self.assertIn("nothing scheduled", out["report"])

    def test_a_rejected_mealplan_write_does_not_pass(self):
        self.call(tools.mealie_inventory())
        self.call(tools.save_week_manifest(WEEK, week_start="2026-08-25"))
        FakeMealie.reject_mealplan_for = {"2026-08-27"}
        self.call(tools.apply_week_manifest())
        out = self.call(tools.verify_week_run(basket_result={
            "basket": {"verdict": "complete"}}))
        self.assertEqual(out["verdict"], "FAIL")
        self.assertIn("2026-08-27", out["report"])

    def test_an_unchecked_basket_cannot_pass(self):
        self.full_run()
        out = self.call(tools.verify_week_run())
        self.assertEqual(out["verdict"], "FAIL")
        self.assertIn("not checked", out["report"])

    def test_an_incomplete_basket_fails_the_run(self):
        self.full_run()
        out = self.call(tools.verify_week_run(basket_result=json.dumps({
            "basket": {"verdict": "incomplete", "correct_lines": 11,
                       "expected_lines": 12,
                       "missing": [{"ean": "70900", "expected": 2}]}})))
        self.assertEqual(out["verdict"], "FAIL")
        self.assertIn("missing 70900", out["report"])

    def test_a_recipe_mangled_after_the_write_is_caught(self):
        self.full_run()
        FakeMealie.recipes["rett-0"]["recipeIngredient"][0]["unit"] = None
        out = self.call(tools.verify_week_run(basket_result={
            "basket": {"verdict": "complete"}}))
        self.assertEqual(out["verdict"], "FAIL")
        self.assertIn("has no unit", out["report"])

    # -- the anti-drift guarantee -----------------------------------------
    def test_the_basket_is_priced_for_what_the_recipes_say(self):
        self.full_run()
        menu = self.call(tools.week_menu_for_planning())["menu"]
        stored = FakeMealie.recipes["rett-0"]["recipeIngredient"]
        from_mealie = [f"{r['quantity']:g} {r['unit']['name']} "
                       f"{r['food']['name']}" for r in stored]
        self.assertEqual(menu["Dag0"]["ingredients"], from_mealie)


class ContractUnits(unittest.TestCase):
    def test_time_parsing_handles_hours_and_minutes(self):
        self.assertEqual(contract.minutes("35 minutter"), 35)
        self.assertEqual(contract.minutes("1 time 20 minutter"), 80)
        self.assertIsNone(contract.minutes("raskt"))

    def test_the_food_index_folds_case_plural_and_alias(self):
        idx = contract.index([{"id": "f1", "name": "Tomat",
                               "pluralName": "Tomater",
                               "aliases": [{"name": "tomater, hermetiske"}]}])
        for spelling in ("tomat", "TOMAT", "Tomater", "tomater, hermetiske"):
            self.assertEqual(idx[contract.key(spelling)]["id"], "f1")


if __name__ == "__main__":
    unittest.main()


class RecipeWrites(unittest.TestCase):
    """What the 2026-08-20 live failure taught, kept as tests.

    Seven recipes failed with HTTP 400 "Recipe already exists" - including
    four that already existed and were only being updated - and three empty
    shells were left behind. The cause was PUT semantics, not the menu.
    """

    def test_slugs_match_what_mealie_would_generate(self):
        # Read off the live instance on 2026-08-20: Mealie stores these tags
        # under transliterated slugs, so a new tag must be spelled the same
        # way or the library grows a second copy of it.
        self.assertEqual(contract.slugify("Høyt Proteininnhold"),
                         "hoyt-proteininnhold")
        self.assertEqual(contract.slugify("Ren Mat"), "ren-mat")
        self.assertEqual(contract.slugify("Under 40 min"), "under-40-min")
        self.assertEqual(contract.slugify("Ovnsrett"), "ovnsrett")

    def test_tag_lookup_survives_mealies_title_casing(self):
        # Mealie title-cases tag names on creation, so the manifest's
        # "Under 40 min" has to find the stored "Under 40 Min".
        idx = {contract.key("Under 40 Min"): {"id": "t", "name": "Under 40 Min",
                                              "slug": "under-40-min"}}
        body = contract.recipe_body({"tags": ["Under 40 min"]},
                                    {"tags": idx, "foods": {}, "units": {}})
        self.assertEqual(body["tags"][0]["id"], "t")


class WriteRecovery(ToolRun):
    """A write that fails must leave a run that can be finished, not restarted."""

    def test_a_failed_write_records_the_slug_for_a_retry(self):
        self.call(tools.mealie_inventory())
        self.call(tools.save_week_manifest(WEEK, week_start="2026-08-25"))
        FakeMealie.reject_write_for = {"rett-3"}
        out = self.call(tools.apply_week_manifest())

        self.assertIn("rett-3", out["empty_recipes_awaiting_retry"])
        self.assertEqual(len(out["recipes"]), 6)
        # The slug is on the manifest, so the shell is addressable.
        saved = {d["date"]: d for d in state.load_manifest()["days"]}
        self.assertEqual(saved["2026-08-28"]["slug"], "rett-3")

        verified = self.call(tools.verify_week_run(basket_result={
            "basket": {"verdict": "complete"}}))
        self.assertEqual(verified["verdict"], "FAIL")

        # Retrying finishes the job without creating a second copy.
        FakeMealie.reject_write_for = set()
        before = len(FakeMealie.recipes)
        retry = self.call(tools.apply_week_manifest())
        self.assertEqual(len(FakeMealie.recipes), before)
        self.assertNotIn("empty_recipes_awaiting_retry", retry)
        self.assertEqual(
            len(FakeMealie.recipes["rett-3"]["recipeIngredient"]), 3)

        verified = self.call(tools.verify_week_run(basket_result={
            "basket": {"verdict": "complete"}}))
        self.assertEqual(verified["verdict"], "PASS", verified["report"])

    def test_a_write_does_not_null_the_fields_it_does_not_send(self):
        """PUT wiped dateAdded/createdAt by omission even when it succeeded."""
        self.full_run()
        FakeMealie.recipes["rett-0"]["dateAdded"] = "2026-01-01"
        FakeMealie.recipes["rett-0"]["image"] = "cover.jpg"
        self.call(tools.apply_week_manifest())
        stored = FakeMealie.recipes["rett-0"]
        self.assertEqual(stored["dateAdded"], "2026-01-01")
        self.assertEqual(stored["image"], "cover.jpg")
        self.assertTrue(stored["tags"])          # and still writes ours
