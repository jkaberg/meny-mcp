"""Regression tests for the picks that were wrong on a real week's planning.

Every case below is a product that a live search actually returned and a live
ranking actually chose. They are written as the shopper's question ("I asked
for avokado") rather than as unit tests of a scoring function, because the
scoring function is an implementation detail and the question is not.

Run: ``python -m unittest discover -s tests`` (stdlib only, no pytest needed).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meny_mcp.matching import (  # noqa: E402
    inflection_of, parse_ask, query_variants, synonyms_name,
)
from meny_mcp.menu import AggregatedNeed, MenuService  # noqa: E402
from meny_mcp.models import Product  # noqa: E402
from meny_mcp.pricing import (  # noqa: E402
    Preferences, pick_best, verdict_for,
)
from meny_mcp.profile import BASE_PANTRY, matches as pantry_match  # noqa: E402


def product(ean: str, title: str, price: float, *, subtitle: str = "",
            group: str = "", leaf: str = "", unit: str = "kg",
            unit_price: float | None = None, frozen: bool = False,
            **extra) -> Product:
    """A product in the shape a search hit arrives in."""
    return Product.model_validate({
        "ean": ean,
        "title": title,
        "subtitle": subtitle,
        "shoppingListGroupName": group,
        "shoppingListGroupName1": leaf,
        "pricePerUnit": price,
        "comparePricePerUnit": unit_price if unit_price is not None else price,
        "compareUnit": unit,
        "isForSale": True,
        **({"environmentalCodes": [{"name": "frysevare"}]} if frozen else {}),
        **extra,
    })


def rejected(query: str, item: Product) -> bool:
    return not verdict_for(item, parse_ask(query)).ok


def chosen(query: str, items: list[Product], **kw) -> str:
    best = pick_best(items, query, Preferences(), **kw)
    return best.product.title if best else ""


class DerivedProducts(unittest.TestCase):
    """Made *from* the ingredient is not the ingredient, at any price."""

    def test_oil_is_not_the_fruit(self):
        self.assertTrue(rejected("avokado", product(
            "1", "Avokado Olje", 175.0, subtitle="250ml")))

    def test_biscuit_is_not_the_fruit(self):
        self.assertTrue(rejected("sitron", product(
            "2", "Cookie Sitron", 12.0, subtitle="54g")))

    def test_squash_is_not_the_fruit(self):
        # Scored 0.85 relevance against "sitron" before this rule existed.
        self.assertTrue(rejected("sitron", product(
            "3", "Sitron&Limesaft", 29.9, subtitle="1l")))

    def test_asking_for_the_derived_thing_still_works(self):
        self.assertFalse(rejected("soyasaus", product(
            "4", "Soyasaus", 34.9, subtitle="150ml")))
        self.assertFalse(rejected("olivenolje", product(
            "5", "Olivenolje Extra Virgin", 89.0)))

    def test_rice_is_not_an_ice_cream(self):
        # "ris" ends with "is". Short heads must never suffix-match.
        self.assertFalse(rejected("ris", product("6", "Jasminris", 32.0)))


class PreparedDishes(unittest.TestCase):
    def test_ready_meal_is_not_the_raw_fish(self):
        self.assertTrue(rejected("torsk", product(
            "7", "Torsk i Saus", 69.0, subtitle="Fjordland 400g")))

    def test_plain_fish_survives(self):
        self.assertFalse(rejected("torsk", product(
            "8", "Torskefilet", 159.0, subtitle="400g")))


class Species(unittest.TestCase):
    def test_named_animal_excludes_the_others(self):
        self.assertTrue(rejected("kjøttdeig storfe", product(
            "9", "Kjøttdeig Storfe&Svin", 89.9, subtitle="400g")))

    def test_implied_animal_excludes_the_others(self):
        # bacon is pork; poultry bacon is a different product.
        self.assertTrue(rejected("bacon", product(
            "10", "Smårettbacon Kylling&Kalkun", 39.9)))

    def test_unspecified_request_accepts_a_blend(self):
        self.assertFalse(rejected("kjøttdeig", product(
            "11", "Kjøttdeig Storfe&Svin", 89.9)))


class Form(unittest.TestCase):
    """An unrequested form is the wrong good, not a cheaper one."""

    def test_fresh_beats_canned(self):
        self.assertEqual("Cherrytomater", chosen("cherrytomater", [
            product("12", "Cherrytomater Hermetisk", 18.9,
                    subtitle="Mutti 400g", group="Hermetiske grønnsaker"),
            product("13", "Cherrytomater", 29.9, subtitle="250g"),
        ]))

    def test_root_beats_powder(self):
        self.assertEqual("Ingefær", chosen("ingefær", [
            product("14", "Ingefær Malt", 24.9, subtitle="30g Santa Maria"),
            product("15", "Ingefær", 49.9, subtitle="pr kg"),
        ]))

    def test_dry_pasta_beats_fresh_pasta(self):
        self.assertEqual("Spaghetti", chosen("spaghetti", [
            product("16", "Spaghetti Fersk", 39.9, subtitle="250g"),
            product("17", "Spaghetti", 19.9, subtitle="500g"),
        ]))

    def test_fresh_beats_frozen_blend(self):
        self.assertEqual("Brokkoli", chosen("brokkoli", [
            product("18", "Brokkoli & Blomkål", 29.9,
                    subtitle="Frossen 500g", group="Frysevarer grønnsaker"),
            product("19", "Brokkoli", 34.9, subtitle="pr stk"),
        ]))

    def test_requested_form_is_honoured(self):
        self.assertEqual("Brokkoli & Blomkål", chosen("frossen brokkoli", [
            product("20", "Brokkoli & Blomkål", 29.9, subtitle="Frossen 500g"),
            product("21", "Brokkoli", 34.9, subtitle="pr stk"),
        ]))

    def test_wrong_form_survives_when_it_is_all_there_is(self):
        # Nothing else on the shelf: report the substitution, do not report
        # the ingredient as unavailable.
        best = pick_best([product("22", "Ingefær Malt", 24.9, subtitle="30g")],
                         "ingefær", Preferences())
        self.assertIsNotNone(best)
        self.assertIn("form", best.caveat or "")


class Varieties(unittest.TestCase):
    def test_plain_beats_an_unrequested_variety(self):
        self.assertEqual("Paprika Rød", chosen("paprika", [
            product("23", "Paprika Padron", 39.9, subtitle="150g"),
            product("24", "Paprika Rød", 42.9, subtitle="pr stk"),
        ]))


class Spellings(unittest.TestCase):
    """The five that came back 'not found' while sitting on the shelf."""

    def test_french_spelling(self):
        self.assertIn("champignon", query_variants("sjampinjong"))

    def test_reversed_compound(self):
        self.assertIn("sennep dijon", query_variants("dijonsennep"))

    def test_cut_before_animal(self):
        self.assertIn("filet svin", query_variants("svinefilet"))

    def test_powder_suffix(self):
        self.assertIn("karri", query_variants("karripulver"))

    def test_leading_word_carries_the_identity(self):
        self.assertIn("tortilla", query_variants("tortillalefser"))

    def test_no_variants_for_a_plain_word(self):
        self.assertEqual([], query_variants("melk"))


class PieceCounts(unittest.TestCase):
    def test_loose_item_priced_per_piece_holds_one_piece(self):
        lemon = product("25", "Sitron", 4.9, unit="stk")
        self.assertEqual(1.0, lemon.pieces_per_pack)

    def test_multipack_is_counted(self):
        self.assertEqual(3.0, product("26", "Avokado", 39.9, unit="stk",
                                      packageSize="3PK").pieces_per_pack)

    def test_a_bag_is_still_unknown(self):
        bag = product("27", "Gulrot", 19.9, unit="kg", packageSize="1KG")
        self.assertIsNone(bag.pieces_per_pack)


class Pantry(unittest.TestCase):
    def test_compound_is_covered_by_its_stem(self):
        self.assertEqual("karri", pantry_match("karripulver", BASE_PANTRY))
        self.assertEqual("sennep", pantry_match("dijonsennep", {"sennep"}))

    def test_staples_that_used_to_be_bought_every_week(self):
        for staple in ("honning", "sesamfrø", "dijonsennep", "soyasaus"):
            self.assertIsNotNone(pantry_match(staple, BASE_PANTRY), staple)

    def test_salt_does_not_swallow_salted_fish(self):
        self.assertIsNone(pantry_match("saltet torsk", BASE_PANTRY))

    def test_real_ingredients_are_not_pantry(self):
        for food in ("laks", "brokkoli", "kjøttdeig", "sitron"):
            self.assertIsNone(pantry_match(food, BASE_PANTRY), food)


class Exclusions(unittest.TestCase):
    def test_named_ingredient_is_dropped_from_the_menu(self):
        meals = __import__("meny_mcp.menu", fromlist=["parse_menu"]).parse_menu({
            "Mandag": {"name": "Fiskesuppe",
                       "ingredients": ["200 g reker", "10 g dill", "1 kg torsk"]},
        })
        needs, _pantry, excluded = MenuService.aggregate(
            meals, exclude=["reker", "dill"])
        self.assertEqual(["torsk"], [n.name for n in needs])
        self.assertEqual(2, len(excluded))

    def test_product_containing_it_is_refused(self):
        prefs = Preferences(exclude_words={"dill"})
        dressing = product("28", "Dressing", 29.9,
                           ingredients="Rapsolje, eddik, dill, salt")
        self.assertIsNone(pick_best([dressing], "dressing", prefs))


class Quantities(unittest.TestCase):
    def test_a_mild_form_is_not_a_substitution(self):
        # Sliced bacon is still bacon by the gram. Flagging it would order one
        # pack where the recipe wanted four.
        verdict = verdict_for(
            product("30", "Bacon Bøkerøkt", 84.0, subtitle="skivet 400g"),
            parse_ask("bacon"))
        self.assertTrue(verdict.ok)
        self.assertFalse(verdict.form_mismatch)

    def test_a_severe_form_is_a_substitution(self):
        verdict = verdict_for(
            product("31", "Ingefær Malt", 24.9, subtitle="30g"),
            parse_ask("ingefær"))
        self.assertTrue(verdict.form_mismatch)

    def test_a_dried_substitute_orders_one_pack_not_the_fresh_weight(self):
        # 20 g of fresh ginger bought two 10 g jars of ground ginger.
        need = AggregatedNeed(name="ingefær", group="mass", quantity=0.02,
                              unit="kg")
        jar = product("29", "Ingefær Malt", 24.9, subtitle="10g",
                      measurementValue=10, measurementType="g")
        self.assertEqual(
            2.0, MenuService._packs_required(jar, need, form_mismatch=False))
        self.assertEqual(
            1.0, MenuService._packs_required(jar, need, form_mismatch=True))


class LeafGroup(unittest.TestCase):
    """The shop's own deepest label decides what a product IS.

    Live case: "torsk" returned twelve candidates and only the two titled bare
    "Torsk" survived, both from the fresh counter at 429 and 519 kr/kg. Every
    real fillet is titled "Torskefilet", which is a prefix match worth 0.45 -
    under the relevance floor - while its coarse groups say only "Fisk".
    """

    def cod(self) -> list[Product]:
        return [
            product("40", "Torsk Filet", 429.0, subtitle="u/Skinn Vill pr Kg",
                    group="Fisk", leaf="Torsk", unit_price=429.0),
            product("41", "Torskefilet", 149.0, subtitle="800g First Price",
                    group="Fisk", leaf="Torsk", unit_price=186.25),
        ]

    def test_a_fillet_answers_the_fish(self):
        self.assertEqual("Torskefilet", chosen("torsk", self.cod()))

    def test_the_fillet_is_unreachable_without_the_leaf_label(self):
        # Same two products with only the coarse group the old code read.
        blind = [product(p.ean, p.title or "", p.effective_price or 0,
                         subtitle=p.subtitle or "", group="Fisk",
                         unit_price=p.unit_price)
                 for p in self.cod()]
        self.assertEqual("Torsk Filet", chosen("torsk", blind))

    def test_a_ready_meal_shelved_elsewhere_is_still_refused(self):
        self.assertTrue(rejected("torsk", product(
            "42", "Torsk i Saus", 79.9, subtitle="550g Fjordland",
            group="Ferdigretter", leaf="Fiskemåltid ferdig")))

    def test_a_label_may_inflect_but_not_grow_a_morpheme(self):
        # "Sitrongress" starts with "sitron" and is lemongrass. It scored a
        # full 1.00 and beat two real lemons on price.
        lemons = [
            product("43", "Sitron", 12.98, subtitle="stykk", leaf="Sitron",
                    unit="stk", unit_price=12.98),
            product("44", "Sitrongress", 11.0, subtitle="50g pakke",
                    leaf="Sitrongress", unit="stk", unit_price=11.0),
        ]
        self.assertEqual("Sitron", chosen("sitron", lemons))
        # ...while a plural still matches.
        self.assertEqual("Tomat Cherry", chosen("tomat", [
            product("45", "Tomat Cherry", 25.0, leaf="Tomater"),
        ]))

    def test_the_shop_may_spell_it_shorter_than_the_recipe(self):
        # Meny sells "Aubergin". Scored strictly that is a flat 0.00, and the
        # ingredient came back unbuyable while sitting on the shelf.
        self.assertEqual("Aubergin", chosen("aubergine", [
            product("46", "Aubergin", 29.9, subtitle="stykk", leaf="Aubergin"),
        ]))

    def test_a_morpheme_is_still_not_an_inflection(self):
        # The tolerance runs both ways, and must stay short enough in both
        # that a compound cannot slip through it. These are the exact pairs
        # the boundary exists to separate.
        for label, token, same in (
            ("tomater", "tomat", True),       # plural
            ("aubergin", "aubergine", True),  # the shop drops the -e
            ("epler", "eple", True),
            ("sitrongress", "sitron", False),  # lemongrass is not a lemon
            ("risotto", "ris", False),
            ("kyllingfilet", "kylling", False),
            ("torskefilet", "torsk", False),
        ):
            self.assertIs(same, inflection_of(label, token),
                          f"{label!r} vs {token!r}")
            self.assertIs(same, inflection_of(token, label),
                          f"{token!r} vs {label!r} (reversed)")


class ShopSynonyms(unittest.TestCase):
    """Some ingredients are a category, and no string work on titles finds them.

    "biff" returned eight steak cuts - Mørbrad, Entrecote, Indrefilet,
    Ytrefilet - every one scoring a flat 0.00, because not one is called a
    biff. All eight list ``biff`` in the ``synonyms`` on their full record.
    """

    # Trimmed from the live record for Storfe Ytrefilet, brand and label noise
    # included, because that noise is the reason matching is conservative.
    STEAK = ["biff", "biffene", "biffen", "biffer", "ytrefilet",
             "storfekjøtt", "oksekjøtt", "meny", "nøkkelhullet", "sunnere",
             "mindre salt", "mindre sukker", "ferskt kjøtt"]

    def steak(self) -> Product:
        return product("80", "Storfe Ytrefilet", 155.0, subtitle="Ca400g Meny",
                       leaf="Ytrefilet av storfe", unit_price=387.5,
                       brand="Meny", synonyms=self.STEAK)

    def test_a_category_word_reaches_the_cuts(self):
        self.assertTrue(synonyms_name(self.STEAK, parse_ask("biff")))

    def test_a_search_hit_carries_none_so_nothing_changes_there(self):
        # The field only exists on the full record; ranking a hit must be
        # unaffected, or the rescue would fire on the cheap path too.
        self.assertFalse(synonyms_name([], parse_ask("biff")))
        self.assertEqual("", chosen("biff", [
            product("81", "Storfe Ytrefilet", 155.0, leaf="Ytrefilet av storfe"),
        ]))

    def test_hydrating_the_record_makes_it_selectable(self):
        self.assertEqual("Storfe Ytrefilet", chosen("biff", [self.steak()]))

    def test_a_synonym_never_outranks_a_real_name(self):
        # Permission, not preference: the cheaper synonym match must lose to a
        # product actually named after the ingredient.
        self.assertEqual("Entrecote", chosen("entrecote", [
            product("82", "Storfe Ytrefilet", 90.0, leaf="Ytrefilet av storfe",
                    unit_price=225.0, synonyms=["entrecote", "biff"]),
            product("83", "Entrecote", 155.0, leaf="Entrecote",
                    unit_price=387.5),
        ]))

    def test_label_noise_does_not_answer_a_query(self):
        # "mindre salt" is a nøkkelhull marketing phrase carried by thousands
        # of products. It must not make every one of them an answer to "salt".
        for noise in ("salt", "sunn", "sukker"):
            self.assertFalse(synonyms_name(self.STEAK, parse_ask(noise)),
                             f"{noise!r} matched label noise")

    def test_the_brand_is_not_an_identity(self):
        # The shop lists a product's own brand as a search alias.
        self.assertTrue(synonyms_name(self.STEAK, parse_ask("meny")))
        self.assertFalse(synonyms_name(self.STEAK, parse_ask("meny"),
                                       ignore=["Meny"]))
        self.assertEqual("", chosen("meny", [self.steak()]))

    def test_a_multiword_query_must_match_every_word(self):
        self.assertTrue(synonyms_name(self.STEAK, parse_ask("ferskt kjøtt")))
        self.assertFalse(synonyms_name(self.STEAK, parse_ask("biff kylling")))

    def test_the_shop_enumerates_the_inflections_itself(self):
        # So this matches exactly, and does not need the tolerance the group
        # labels get. "sunn" reaching "sunnere" is what that would have cost.
        forms = ["aubergin", "aubergine", "auberginer", "auberginen"]
        self.assertTrue(synonyms_name(forms, parse_ask("aubergine")))
        self.assertTrue(synonyms_name(forms, parse_ask("auberginer")))
        self.assertFalse(synonyms_name(["auberginer"], parse_ask("aubergine")))


class FruitFlavouredDairy(unittest.TestCase):
    """The avokado-olje failure, one aisle over.

    "Yoghurt Jordbær" at 33 kr/kg outbid real strawberries at 120 kr/kg for the
    query "jordbær", because a yoghurt was not on the list of things built
    *from* an ingredient.
    """

    def test_a_yoghurt_is_not_the_fruit(self):
        self.assertTrue(rejected("jordbær", product(
            "48", "Yoghurt Jordbær", 50.0, subtitle="4x150g Tine",
            leaf="Yoghurt", unit_price=33.33)))

    def test_asking_for_the_yoghurt_still_works(self):
        self.assertFalse(rejected("yoghurt", product(
            "49", "Yoghurt Jordbær", 50.0, subtitle="4x150g Tine",
            leaf="Yoghurt")))

    def test_the_fruit_wins_even_when_dearer_per_kilo(self):
        self.assertEqual("Jordbær", chosen("jordbær", [
            product("48", "Yoghurt Jordbær", 50.0, leaf="Yoghurt",
                    unit_price=33.33),
            product("52", "Jordbær", 59.9, subtitle="Norsk 500g",
                    leaf="Jordbær", unit_price=119.8),
        ]))


class SoldByWeight(unittest.TestCase):
    """Goods ordered by weight have no pack to round up to.

    The counter's cod is labelled ``packageSize: "2KG"`` and sells in 200 g
    steps. Reading the label as a pack charged 429 kr for a 400 g need; the
    calculator prices the honest order at 171.60 kr.
    """

    def counter_cod(self) -> Product:
        return product("50", "Torsk Filet", 429.0, subtitle="u/Skinn pr Kg",
                       leaf="Torsk", productSoldByWeight=True,
                       amountInterval=0.2, unitRule=0.2,
                       measurementValue=2000, measurementType="g",
                       packageSize="2KG")

    def test_the_need_is_ordered_not_a_nominal_pack(self):
        need = AggregatedNeed(name="torsk", group="mass", quantity=0.4,
                              unit="kg")
        self.assertEqual(0.4, MenuService._packs_required(
            self.counter_cod(), need))

    def test_the_order_still_steps_by_the_interval(self):
        need = AggregatedNeed(name="torsk", group="mass", quantity=0.5,
                              unit="kg")
        self.assertEqual(0.6, MenuService._packs_required(
            self.counter_cod(), need))

    def test_a_nominal_label_no_longer_looks_oversized(self):
        # 2 kg against a 400 g need is 5x the pack guard, so the cheapest
        # honest answer used to be thrown out before its price was read.
        self.assertIsNone(self.counter_cod().pack_constraint)

    def test_a_real_pack_still_constrains(self):
        bag = product("51", "Ris", 46.9, subtitle="500g", leaf="Ris",
                      measurementValue=500, measurementType="g")
        self.assertEqual(0.5, bag.pack_constraint)


class CostToCoverTheNeed(unittest.TestCase):
    """Cheapest per kilo is not the same question as cheapest.

    65 g of babyspinat at 153.85 kr/kg beats a 200 g bag at 184.50 kr/kg on
    kr/kg, and covering 200 g means four bags at 10.00 against one at 36.90.
    """

    def test_small_packs_do_not_win_on_unit_price_alone(self):
        spinach = [
            product("60", "Babyspinat", 10.0, subtitle="65g", leaf="Spinat",
                    unit_price=153.85, measurementValue=65,
                    measurementType="g"),
            product("61", "Spinat", 36.9, subtitle="200g", leaf="Spinat",
                    unit_price=184.5, measurementValue=200,
                    measurementType="g"),
        ]
        self.assertEqual("Spinat", chosen("spinat", spinach,
                                          needed_quantity=0.2))

    def test_with_no_stated_need_value_per_unit_still_decides(self):
        spinach = [
            product("60", "Babyspinat", 10.0, subtitle="65g", leaf="Spinat",
                    unit_price=153.85),
            product("61", "Spinat", 36.9, subtitle="200g", leaf="Spinat",
                    unit_price=184.5),
        ]
        self.assertEqual("Babyspinat", chosen("spinat", spinach))


class FreezerAisle(unittest.TestCase):
    """``frysevare`` says frozen when nothing in the name does.

    "Torskefilet Blokk 400g" and "Torskefilet 800g First Price" are both frozen
    and neither says so in any text field.
    """

    def frozen_fillet(self) -> Product:
        return product("70", "Torskefilet", 149.0, subtitle="800g First Price",
                       leaf="Torsk", unit_price=186.25, frozen=True)

    def test_the_freezer_code_is_read(self):
        self.assertTrue(self.frozen_fillet().freezer_goods)
        self.assertIn("frossen",
                      verdict_for(self.frozen_fillet(), parse_ask("torsk")).forms)

    def test_shelving_is_milder_evidence_than_the_name(self):
        # A product marketed AS the frozen variant keeps the severe penalty.
        named = product("71", "Brokkoli & Blomkål", 30.0, subtitle="frossen",
                        leaf="Kål")
        self.assertLess(verdict_for(named, parse_ask("brokkoli")).factor,
                        verdict_for(self.frozen_fillet(),
                                    parse_ask("torsk")).factor)

    def test_freezer_alone_is_not_a_quantity_substitution(self):
        # 400 g of frozen cod is 400 g of cod; ordering "one pack, flagged"
        # would under-buy.
        self.assertFalse(
            verdict_for(self.frozen_fillet(), parse_ask("torsk")).form_mismatch)

    def test_a_household_can_accept_frozen_outright(self):
        prefs = Preferences(accept_forms={"frossen"})
        verdict = verdict_for(self.frozen_fillet(),
                              parse_ask("torsk", prefs.accept_forms))
        self.assertEqual(1.0, verdict.factor)
        self.assertIsNone(verdict.note)

    def test_accepting_a_form_does_not_penalise_its_absence(self):
        # Spelling "frossen" into the query would mark the fresh one wrong.
        fresh = product("72", "Torsk Filet", 429.0, leaf="Torsk")
        prefs = Preferences(accept_forms={"frossen"})
        self.assertEqual(
            1.0, verdict_for(fresh, parse_ask("torsk", prefs.accept_forms)).factor)


if __name__ == "__main__":
    unittest.main()
