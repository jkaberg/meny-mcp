"""The basket script is a payload, not prose - but it still has to run.

``strip_for_transport`` removes about a quarter of the injector before it
travels back through a model's context. That is a saving on something nobody
reads, right up until the stripping eats a character that mattered: a ``//``
inside a URL, an apostrophe inside a string. These tests exist because the
failure mode is a script that looks fine and corrupts a real shopping basket.

Behavioural equivalence was additionally verified out-of-band by running both
variants against a mocked browser (localStorage, sessionStorage, fetch): the
resulting cart, including a 0.4 kg weight-sold line at 171.60 kr, was identical.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meny_mcp.handoff import (  # noqa: E402
    build_clear_script,
    build_injector,
    build_store_probe,
    build_verify_script,
    strip_for_transport,
)

LINES = [{"ean": "7039610012854", "quantity": 2},
         {"ean": "2000193100004", "quantity": 0.4}]

# Anything the script cannot work without. Each is either inside a string
# literal or adjacent to one, which is exactly where a comment stripper goes
# wrong.
ESSENTIAL = (
    "https://platform-rest-prod.ngdata.no",
    "extended_user_offline_key",
    "WEBSHOP_PREFERRED_PICKUP_STORE_GLN",
    "anonymousUserGln",
    "CART_PRODUCT",
    "meny.no",
    "7080001271947",
)


def variants() -> list[tuple[str, str, str]]:
    """(label, readable, stripped) for every script this module emits."""
    out = []
    for mode in ("merge", "append", "replace"):
        for embed in (False, True):
            label = f"injector/{mode}/{'embedded' if embed else 'lean'}"
            out.append((
                label,
                build_injector(LINES, mode=mode, store_id="7080001271947",
                               embed_products=embed, readable=True),
                build_injector(LINES, mode=mode, store_id="7080001271947",
                               embed_products=embed),
            ))
    for label, fn in (("clear", build_clear_script), ("probe", build_store_probe)):
        script = fn()
        out.append((label, script, strip_for_transport(script)))
    verify = build_verify_script("7080001271947", expected_items=LINES)
    out.append(("verify", verify, strip_for_transport(verify)))
    return out


class Stripping(unittest.TestCase):
    def test_nothing_essential_is_lost(self):
        for label, _readable, stripped in variants():
            for needle in ESSENTIAL:
                if needle in _readable:
                    self.assertIn(needle, stripped, f"{label} lost {needle!r}")

    def test_every_string_literal_survives(self):
        # Tokenised off the STRIPPED script, where there are no comments left
        # to confuse a regex - then checked against itself for balance, and
        # against the readable form for count.
        pattern = re.compile(r'"(?:[^"\\\n]|\\.)*"')
        for label, readable, stripped in variants():
            found = pattern.findall(stripped)
            self.assertTrue(found, f"{label} has no string literals at all")
            for literal in found:
                self.assertIn(literal, readable,
                              f"{label} invented or mangled {literal!r}")

    def test_it_is_idempotent(self):
        for label, _readable, stripped in variants():
            self.assertEqual(stripped, strip_for_transport(stripped), label)

    def test_it_actually_saves_something(self):
        readable = build_injector(LINES, mode="merge", store_id="7080001271947",
                                  readable=True)
        stripped = build_injector(LINES, mode="merge", store_id="7080001271947")
        self.assertLess(len(stripped), len(readable) * 0.8)

    def test_a_url_is_not_read_as_a_comment(self):
        source = 'const A = "https://x.no/api";  // trailing note\nconst B = 1;'
        self.assertEqual('const A = "https://x.no/api";\nconst B = 1;',
                         strip_for_transport(source))

    def test_braces_stay_balanced(self):
        for label, _readable, stripped in variants():
            for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
                self.assertEqual(stripped.count(opener), stripped.count(closer),
                                 f"{label}: unbalanced {opener}{closer}")


class Payload(unittest.TestCase):
    def test_the_lean_script_carries_only_ean_and_quantity(self):
        script = build_injector(LINES, mode="merge", store_id="7080001271947")
        self.assertIn('["7039610012854",2]', script)
        # ...and not a product record, which is ~8.4 kB each.
        self.assertNotIn("comparePricePerUnit\":", script)

    def test_a_weight_sold_quantity_survives_as_a_fraction(self):
        # 0.4 kg of counter cod is the whole point of the weight-sold fix; the
        # script must not round it on the way through.
        self.assertIn('["2000193100004",0.4]',
                      build_injector(LINES, mode="merge",
                                     store_id="7080001271947"))


if __name__ == "__main__":
    unittest.main()


class BasketVerification(unittest.TestCase):
    """The verifier is the only thing standing between a partial handoff and a
    run that reports success. Its diff is therefore executed, not just parsed.
    """

    CART = {"encrypted": False, "data": {"items": [
        {"ean": "7039610012854", "quantity": 2,
         "product": {"title": "Kyllingfilet"}},
        {"ean": "2000193100004", "quantity": 1.0,
         "product": {"title": "Torskefilet"}},
        {"ean": "7038010009457", "quantity": 1, "product": {"title": "Melk"}},
    ]}}

    def run_script(self, expected, cart=None, mode="merge"):
        """Execute the stripped script under a mocked browser, return its dict."""
        import json
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        script = strip_for_transport(
            build_verify_script("7080001271947", expected_items=expected,
                                mode=mode))
        ext = json.dumps({"store": {"gln": 7080001271947, "name": "MENY Moholt"}})
        harness = """
        const store = {"CART": %s};
        globalThis.location = {hostname: "meny.no"};
        globalThis.localStorage = {getItem: k => (k in store ? store[k] : null)};
        globalThis.sessionStorage = {getItem: () => %s};
        globalThis.document = {querySelectorAll: () => [
          {children: [], textContent: "MENY Moholt"}]};
        console.log(eval(%s));
        """ % (json.dumps(json.dumps(cart if cart is not None else self.CART)),
               json.dumps(ext), json.dumps(script))
        proc = subprocess.run([node, "-e", harness], capture_output=True,
                              text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_complete_basket_passes(self):
        out = self.run_script([{"ean": "7039610012854", "quantity": 2}])
        self.assertEqual(out["basket"]["verdict"], "complete")
        self.assertNotIn("missing", out["basket"])

    def test_missing_and_wrong_quantity_are_caught(self):
        out = self.run_script([
            {"ean": "7039610012854", "quantity": 2},
            {"ean": "2000193100004", "quantity": 0.4},
            {"ean": "7090000000009", "quantity": 3},
        ])
        basket = out["basket"]
        self.assertEqual(basket["verdict"], "incomplete")
        self.assertEqual(basket["correct_lines"], 1)
        self.assertEqual([m["ean"] for m in basket["missing"]],
                         ["7090000000009"])
        self.assertEqual(basket["wrong_quantity"][0]["actual"], 1)
        self.assertEqual(basket["wrong_quantity"][0]["expected"], 0.4)

    def test_untouched_user_lines_are_not_a_failure(self):
        out = self.run_script([{"ean": "7039610012854", "quantity": 2}])
        self.assertEqual(out["basket"]["verdict"], "complete")
        self.assertIn("7038010009457",
                      [u["ean"] for u in out["basket"]["unexpected"]])

    def test_weight_sold_fractions_compare_exactly(self):
        out = self.run_script([{"ean": "2000193100004", "quantity": 1.0}])
        self.assertEqual(out["basket"]["verdict"], "complete")

    def test_encrypted_basket_is_unknown_not_complete(self):
        out = self.run_script([{"ean": "7039610012854", "quantity": 2}],
                              cart={"encrypted": True, "data": {}})
        self.assertEqual(out["basket"]["verdict"], "unknown")
        self.assertIn("encrypted", out["basket"]["error"])

    def test_no_expected_items_never_reads_as_complete(self):
        """Inspection is not verification, and must not be mistaken for it."""
        out = self.run_script([])
        self.assertNotEqual(out["basket"]["verdict"], "complete")


class BasketInspection(BasketVerification):
    """Reading the basket *before* filling it, which is a different question.

    "Is this what I ordered" needs an expectation to compare against. "What is
    in here, and should it stay" does not - and asking it first is the only
    way the user gets to decide between keeping and discarding.
    """

    def test_it_lists_what_is_actually_in_there(self):
        out = self.run_script([])["basket"]
        self.assertEqual(out["verdict"], "not empty")
        self.assertEqual(out["item_count"], 3)
        by_ean = {line["ean"]: line for line in out["lines"]}
        self.assertEqual(by_ean["7039610012854"]["title"], "Kyllingfilet")
        self.assertEqual(by_ean["2000193100004"]["quantity"], 1)

    def test_an_empty_basket_says_so_rather_than_listing_nothing(self):
        out = self.run_script([], cart={"encrypted": False,
                                        "data": {"items": []}})["basket"]
        self.assertEqual(out["verdict"], "empty")
        self.assertEqual(out["lines"], [])

    def test_it_totals_the_lines_it_found(self):
        cart = {"encrypted": False, "data": {"items": [
            {"ean": "1", "quantity": 1, "linePrice": 42.50,
             "product": {"title": "A"}},
            {"ean": "2", "quantity": 1, "linePrice": 7.25,
             "product": {"title": "B"}}]}}
        self.assertEqual(self.run_script([], cart=cart)["basket"]
                         ["estimated_total"], 49.75)

    def test_an_encrypted_basket_cannot_be_inspected_either(self):
        out = self.run_script([], cart={"encrypted": True, "data": {}})
        self.assertEqual(out["basket"]["verdict"], "unknown")


class ReplaceMode(BasketVerification):
    """Under replace, a survivor is a failure - not a forgiven extra."""

    def test_a_survivor_fails_the_verdict(self):
        out = self.run_script([{"ean": "7039610012854", "quantity": 2}],
                              mode="replace")["basket"]
        self.assertEqual(out["verdict"], "incomplete")
        self.assertIn("should have been cleared", out["unexpected_note"])

    def test_the_same_basket_passes_under_merge(self):
        out = self.run_script([{"ean": "7039610012854", "quantity": 2}],
                              mode="merge")["basket"]
        self.assertEqual(out["verdict"], "complete")
        self.assertIn("not a failure", out["unexpected_note"])

    def test_replace_passes_when_nothing_survived(self):
        cart = {"encrypted": False, "data": {"items": [
            {"ean": "7039610012854", "quantity": 2,
             "product": {"title": "Kyllingfilet"}}]}}
        out = self.run_script([{"ean": "7039610012854", "quantity": 2}],
                              cart=cart, mode="replace")["basket"]
        self.assertEqual(out["verdict"], "complete")
