"""Best-value selection.

The headline requirement is "prefer the best price per measured unit". That is
harder than sorting by ``comparePricePerUnit``, for three reasons found in live
data:

1. A single query mixes unit groups. "melk" returns kg, l AND porsjon; "laks"
   returns kg, stk and l. Sorting across them is meaningless, so candidates are
   bucketed by ``unit_group`` and only ranked within a bucket.

2. Cheapest per kg is often the wrong buy. A 5 kg sack of rice at 24 kr/kg
   beats a 1 kg bag at 32 kr/kg, but if the recipe needs 200 g you have just
   spent 120 kr to save 8. ``max_pack_multiple`` guards against this.

3. The cheapest hit is frequently not the thing you asked for. Searching
   "kyllingfilet" surfaces marinated variants, strips and whole birds. A
   relevance floor keeps the optimiser from optimising the wrong product.

Point 3 is the one that bites hardest, and word overlap alone cannot fix it:
"Avokado Olje" and "Cookie Sitron" both contain their query exactly. The
category judgement - derived product, prepared dish, wrong form, wrong species
- lives in :mod:`.matching`; this module ranks what survives it.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .matching import Ask, PACKAGING_WORDS as _PACKAGING_WORDS, Verdict, judge
from .matching import (BENIGN_MODIFIERS, fold, identity_words, inflection_of,
                       parse_ask, synonyms_name, tokens as _tokens)
from .models import Product

log = logging.getLogger("meny_mcp.pricing")

# Folded once: relevance compares folded tokens, so the sets it checks them
# against have to be folded too ("klassisk" -> "klasisk").
_FOLDED_PACKAGING = frozenset(fold(w) for w in _PACKAGING_WORDS)
_FOLDED_BENIGN = frozenset(fold(w) for w in BENIGN_MODIFIERS)


# ---------------------------------------------------------------------------
# Text relevance
# ---------------------------------------------------------------------------
def relevance(product: Product, query: str) -> float:
    """0..1 confidence that this product IS the thing asked for.

    Naive token matching fails badly on Norwegian, in both directions. Two real
    cases found against live data:

      * "fløte" vs "Kremfløte"    - scored 0.00 (no bare token, no prefix)
      * "fløte" vs "Fløteispinne" - scored 1.00 (prefix match... on an ice cream)

    Norwegian compounds are head-final, so the meaningful word is usually at the
    END ("kremFLØTE", "matFLØTE" are creams; "FLØTEispinne" is not). Prefix
    matches are therefore weak evidence and suffix matches are strong.

    The decisive signal, though, is the shop's own product-group label, and
    specifically its DEEPEST level - ``shoppingListGroupName1`` / ``...Leaf``,
    exposed as :attr:`Product.leaf_group`. When the query matches that, the
    product is the right kind of thing regardless of its title.

    Using only the coarse levels is what made "torsk" unanswerable. Every real
    cod fillet is titled "Torskefilet" or "Torskeloins", which is a *prefix*
    match worth 0.45 - below the 0.5 floor - while its groups say only "Fisk"
    and "Fisk & skalldyr", shared with tinned cod roe and a ready meal. So the
    entire shortlist was discarded and the two products titled bare "Torsk",
    both from the fresh counter at 429 and 519 kr/kg, were all that survived.
    The leaf label says "Torsk" on every one of the fillets and "Fiskemåltid
    ferdig" on the ready meal, which settles it: the cheapest genuine fillet is
    144.75 kr/kg. The head-final rule is simply wrong for grocery compounds -
    in "torskefilet" the modifier carries the ingredient and the head is the
    cut - and the shop's own taxonomy is a better answer than tuning the score.

    Scoring per query token, best evidence wins:
        1.00  matches a product-group label, or an exact title token
        0.65  title compound ending in the token   (kremfløte <- fløte)
        0.45  title compound starting with the token (laksefilet <- laks)
        0.20  only appears in subtitle or brand
        0.00  absent

    A group match still caps at 0.5 when the title carries no trace of the
    query (see below), which is what keeps "Skrei Backloin" - genuinely leaf
    group ``Torsk``, and 899 kr/kg - selectable but never preferred.

    Containing the word is necessary but not sufficient. "Avocado Spread" and
    "Treats Gulrot Muffins" both contain the query exactly and both scored a
    flat 1.00, which let a jar of spread answer "1 stk avocado" whenever it
    happened to be a few øre cheaper. So the score is scaled down by how much of
    the TITLE the query fails to explain: each unexplained title word is
    evidence that this is a different product built from the ingredient rather
    than the ingredient itself.
    """
    # Score identity only. "frossen" in "frossen brokkoli" is a form, judged
    # separately by `matching.judge`; counting it here would mark a plain
    # brokkoli as half-right for a request it answers in full.
    want = identity_words(_tokens(query))
    if not want:
        return 1.0

    # Both sides folded, so "Avocado" answers "avokado".
    want = {fold(w) for w in want}
    title = {fold(w) for w in _tokens(product.title)}
    group = {fold(w) for w in
             _tokens(product.leaf_group)
             | _tokens(product.shopping_list_group_name)
             | _tokens(product.category_name)}
    weak = {fold(w) for w in
            _tokens(product.subtitle) | _tokens(product.brand)}

    def names(word: str, token: str) -> bool:
        """The title word IS the query word, allowing for inflection.

        Same tolerance as the group labels, and for the same reason: the shop
        sells *Aubergin* and the recipe says "aubergine". Scored strictly, that
        one letter was the difference between a 0.00 and an ingredient nobody
        could buy.
        """
        return word == token or inflection_of(word, token)

    total = 0.0
    for token in want:
        if token in group or any(inflection_of(g, token) for g in group):
            total += 1.0
        elif any(names(t, token) for t in title):
            total += 1.0
        elif any(t.endswith(token) for t in title):
            total += 0.65
        elif any(t.startswith(token) for t in title):
            total += 0.45
        elif token in weak or any(w.startswith(token) for w in weak):
            total += 0.20
    score = min(1.0, total / len(want))

    # The shop's own product group is strong evidence, but on its own it only
    # says "shelved next to" - "Bokhvete Boil In Bag" sits in the Ris group and
    # is not rice. With no trace of the query in the title, cap at the default
    # relevance floor: still selectable when nothing better exists, never
    # preferred over something actually named after the ingredient.
    if not any(
        names(t, token) or t.startswith(token) or t.endswith(token)
        for token in want for t in title
    ):
        score = min(score, 0.5)

    # Unexplained title words. A word is explained when some query token is a
    # prefix or suffix of it, so "Kremfløte" is explained by "fløte".
    # Packaging words describe how a thing is sold, not what it is, so they are
    # never evidence of a different product - "Sitron stykk" is a lemon. Nor are
    # colour/size/grade words: "Paprika Rød" is a paprika. What is left is
    # mostly variety names, and a variety name usually IS a different product -
    # "Paprika Padron" is not what a recipe calling for paprika wants - so the
    # penalty is steep enough to lose to a plainly-named alternative.
    unexplained = sum(
        1 for word in title
        if word not in _FOLDED_PACKAGING
        and word not in _FOLDED_BENIGN
        and not any(names(word, t) or word.startswith(t) or word.endswith(t)
                    for t in want)
    )
    return round(score * max(0.45, 1.0 - 0.18 * unexplained), 4)


def _brand_words(product: Product) -> list[str]:
    """Words in a product's synonym list that identify its maker, not its kind.

    The shop lists a product's brand, vendor and quality labels as search
    aliases alongside the genuine ones, so "Storfe Ytrefilet" answers to
    ``meny`` and ``nøkkelhullet`` as readily as to ``biff``.
    """
    words = [product.brand, product.vendor]
    for entry in product.environmental_codes or []:
        if isinstance(entry, dict):
            words += [entry.get("name"), entry.get("displayName")]
        else:
            words.append(entry)
    return [w for w in words if w]


def verdict_for(product: Product, ask: Ask) -> Verdict:
    """Category judgement for one product against a parsed request."""
    return judge(
        product.title,
        product.subtitle,
        product.shopping_list_group_name,
        product.category_name,
        ask,
        leaf=product.leaf_group,
        freezer=product.freezer_goods,
    )


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------
@dataclass
class Preferences:
    """Knobs for ``pick_best``."""

    prefer_organic: bool = False
    prefer_norwegian: bool = False
    prefer_offers: bool = True
    exclude_brands: set[str] = field(default_factory=set)
    prefer_brands: set[str] = field(default_factory=set)
    # Ingredients the household will not buy - allergies, dislikes, house
    # rules. Checked against the product's own ingredient list too, so "dill"
    # also rejects the dressing that contains it.
    exclude_words: set[str] = field(default_factory=set)
    # Forms to accept without having asked for them, e.g. {"frossen"}. A
    # household that is happy with frozen fish and vegetables says so once
    # here instead of writing "frossen" into every ingredient line - and
    # crucially without the reverse penalty that spelling it into the query
    # would carry, which would mark every fresh product as "not frossen".
    accept_forms: set[str] = field(default_factory=set)
    max_age_limit: int = 0          # 0 = refuse age-restricted goods
    exclude_alcohol: bool = True
    min_relevance: float = 0.5
    # Reject a pack whose size exceeds need * this factor, even if cheaper/unit.
    max_pack_multiple: float = 2.5
    # A cheaper unit price must beat the incumbent by this fraction to displace
    # a more relevant product. Stops 1 kr/kg differences reordering everything.
    unit_price_margin: float = 0.02


@dataclass
class Candidate:
    product: Product
    relevance: float
    unit_price: float | None
    reason: str = ""
    verdict: Verdict = field(default_factory=Verdict)
    # Reached the floor only because the shop's own synonyms vouch for it.
    via_synonym: bool = False

    @property
    def caveat(self) -> str | None:
        """Why this pick is not exactly what was asked for, if it is not."""
        return self.verdict.note

    def summary(self, *, verbose: bool = False) -> dict:
        data = self.product.summary(verbose=verbose)
        data["relevance"] = round(self.relevance, 2)
        notes = [n for n in (self.reason, self.verdict.note) if n]
        if notes:
            data["note"] = "; ".join(notes)
        return data


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def dominant_unit_group(products: Sequence[Product]) -> str | None:
    """The unit group most candidates agree on.

    Ties break toward mass, then volume, then count - the groups that carry
    genuine per-unit meaning for groceries.
    """
    counts = Counter(p.unit_group for p in products if p.unit_group)
    if not counts:
        return None
    best = max(counts.values())
    tied = [g for g, c in counts.items() if c == best]
    for preferred in ("mass", "volume", "count"):
        if preferred in tied:
            return preferred
    return tied[0]


def group_by_unit(products: Iterable[Product]) -> dict[str, list[Product]]:
    buckets: dict[str, list[Product]] = defaultdict(list)
    for product in products:
        if product.unit_group:
            buckets[product.unit_group].append(product)
    return dict(buckets)


def _acceptable(product: Product, prefs: Preferences) -> tuple[bool, str]:
    if not product.available:
        return False, "unavailable"
    if prefs.exclude_alcohol and product.contains_alcohol:
        return False, "alcohol excluded"
    if (product.age_limit or 0) > prefs.max_age_limit:
        return False, f"age limit {product.age_limit}"
    if product.is_medicin:
        return False, "medicine"
    brand = (product.brand or "").lower()
    if brand and any(b.lower() == brand for b in prefs.exclude_brands):
        return False, "brand excluded"
    if prefs.exclude_words:
        # Title first (cheap, always present), then the declared ingredient
        # list, which is what catches dill inside a dressing. Only available on
        # the full record - the verification pass in menu planning re-checks
        # there, so a search hit passing here is not the last word.
        haystack = _tokens(product.title) | _tokens(product.subtitle)
        text = (product.ingredients or "").lower()
        for word in prefs.exclude_words:
            needle = word.strip().lower()
            if not needle:
                continue
            if needle in haystack or any(t.startswith(needle) for t in haystack):
                return False, f"excluded: {needle}"
            if text and needle in text:
                return False, f"excluded ingredient: {needle}"
    return True, ""


def _preference_bonus(product: Product, prefs: Preferences) -> float:
    """Small multiplicative discount applied to the ranking key only.

    This never changes the reported price - it only nudges ordering between
    products that are already close on genuine unit price.
    """
    bonus = 1.0
    if prefs.prefer_organic and product.organic:
        bonus *= 0.95
    if prefs.prefer_norwegian and (product.country_of_origin or "").lower().startswith("norge"):
        bonus *= 0.97
    if prefs.prefer_offers and product.on_sale:
        bonus *= 0.97
    if product.brand and product.brand.lower() in {b.lower() for b in prefs.prefer_brands}:
        bonus *= 0.90
    return bonus


def _count_cost(product: Product, needed: float | None) -> float | None:
    """Kroner to satisfy a need of N items with this product.

    One metric, in one currency, for every candidate - which is what makes a
    2-pack, a loose item and a 1 kg bag comparable at all.

    Price per piece alone picks wrong at the edges: for "1 stk avocado" a 2-pack
    at 46.90 beats a single at 29.90 on price-per-piece (23.45 vs 29.90) while
    costing 17 kr more, because you cannot buy half a 2-pack. Whole packs times
    pack price is the number the shopper actually pays, and it still prefers the
    2-pack once two are needed.

    A pack with no knowable piece count (a bag, a net) costs one pack: you
    cannot buy part of it, and one is what ``_packs_required`` will order.
    Ranking those on kr/kg instead - or putting them in a separate tier - is
    what makes "2 stk gulrot" resolve to a countable box of carrot muffins
    rather than the 10 kr bag of carrots sitting next to it.
    """
    price = product.effective_price
    if price is None:
        return None
    pieces = product.pieces_per_pack
    if not pieces:
        return round(price, 4)
    packs = math.ceil((needed / pieces) - 1e-9) if needed else 1
    return round(price * max(1, packs), 4)


def _amount_cost(product: Product, needed: float) -> float | None:
    """Kroner to cover a mass or volume need, packs being indivisible.

    The same argument as :func:`_count_cost`, for the other kind of need. Price
    per kilo answers "which is better value" and the shopper is asking "which
    costs less", and for small needs those come apart: 65 g of babyspinat at
    153.85 kr/kg beats a 200 g bag at 184.50 kr/kg on kr/kg, but covering 200 g
    means four bags at 10.00 against one at 36.90.

    ``max_pack_multiple`` only ever caught the gross version of this (the 5 kg
    sack of rice bought for a 200 g need). Costing the need directly catches
    both, and agrees by construction with what ``_packs_required`` will order.
    """
    price = product.effective_price
    if price is None:
        return None
    # Sold by weight: you buy the amount, rounded to the order step.
    if product.product_sold_by_weight:
        return round(price * product.quantise(needed), 4)
    pack = product.pack_constraint
    if not pack or pack <= 0:
        return round(price, 4)
    return round(price * max(1, math.ceil((needed / pack) - 1e-9)), 4)


def rank(
    products: Sequence[Product],
    query: str = "",
    prefs: Preferences | None = None,
    *,
    needed_quantity: float | None = None,
    count_need: bool = False,
) -> list[Candidate]:
    """Rank candidates by effective price per unit, within one unit group.

    ``count_need`` switches the ranking key to the total kroner needed to cover
    N items (see ``_count_cost``). A recipe wanting "2 stk rødløk" is not served
    by the cheapest kr/kg: a 1 kg bag at 47.90 wins on kr/kg and loses on the
    actual question by 4x against a 2-pack at 28.90.

    Two rejection strengths, and the difference matters. A category verdict of
    ``ok=False`` (an oil, a ready meal, the wrong animal) removes the product
    outright: there is no price at which it answers the question. A form
    mismatch only scales relevance down, and if *every* candidate carries one -
    the shop stocks nothing but frozen brokkoli today - the penalised ones come
    back rather than the caller being told the ingredient does not exist. What
    they come back with is the reason attached, which is the part that was
    missing before: 20 g of ground ginger did get bought for 20 g of fresh
    ginger, silently.
    """
    prefs = prefs or Preferences()
    ask = parse_ask(query, prefs.accept_forms) if query else None

    viable: list[Candidate] = []
    penalised: list[Candidate] = []
    for product in products:
        ok, why = _acceptable(product, prefs)
        if not ok:
            log.debug("reject %s: %s", product.title, why)
            continue
        verdict = verdict_for(product, ask) if ask else Verdict()
        if not verdict.ok:
            log.debug("reject %s: %s", product.title, verdict.note)
            continue
        rel = relevance(product, query) if query else 1.0
        # The shop's own synonyms, when the record carries them. Nothing is
        # named after "biff" - not Mørbrad, not Entrecote, not Indrefilet - so
        # the whole shortlist scored 0.00 and the ingredient came back
        # unbuyable. All eight list `biff` as a synonym.
        #
        # Lifted to EXACTLY the floor, never above it. The array also carries
        # brand and label noise, and a product the shop merely cross-references
        # should be selectable when nothing else answers, never preferred over
        # something actually named after the ingredient. Search hits carry no
        # synonyms at all, so this only ever fires on a hydrated record.
        via_synonym = False
        if ask and rel < prefs.min_relevance and synonyms_name(
            product.synonyms, ask, ignore=_brand_words(product)
        ):
            rel = prefs.min_relevance
            via_synonym = True
        cand = Candidate(product, rel * verdict.factor, product.unit_price,
                         verdict=verdict, via_synonym=via_synonym)
        if cand.relevance < prefs.min_relevance:
            # Below the floor only because of the form penalty? Keep it aside;
            # it is a real answer to the question in the wrong state, and that
            # beats reporting the ingredient as unavailable.
            if verdict.factor < 1.0 and rel >= prefs.min_relevance:
                penalised.append(cand)
            continue
        viable.append(cand)

    if not viable:
        viable = penalised
    if not viable:
        return []

    # Restrict to one comparable unit group - but only when ranking on price
    # per unit, which is the thing that is meaningless across groups.
    #
    # A count need ranks on total kroner, which IS comparable across groups, and
    # applying the filter there actively destroys the answer: "1 stk sitron"
    # surfaces six juices, iced teas and essences (volume) against two actual
    # lemons (mass), so majority rule discards every lemon before ranking.
    if not count_need:
        group = dominant_unit_group([c.product for c in viable])
        if group:
            viable = [c for c in viable if c.product.unit_group == group]

    # Pack-size sanity: drop absurdly large packs relative to what is needed.
    # Uses pack_quantity (physical pack size), NOT min_quantity (order step) -
    # a 2 kg bag of rice has an order step of 1 but a pack size of 2 kg.
    #
    # Skipped for count needs: there `needed_quantity` is a number of items
    # while pack_quantity is a mass, so the comparison would be nonsense.
    # _count_cost already penalises oversized packs by their real cost.
    # Goods sold by weight are exempt: there is no pack to be oversized by. The
    # counter's cod is labelled "2KG" and sells 200 g at a time, and judging it
    # on that label threw the cheapest honest answer out of the ranking before
    # the price was ever looked at.
    if needed_quantity and not count_need:
        kept = []
        for cand in viable:
            size = cand.product.pack_constraint
            if size is None:
                kept.append(cand)
                continue
            size = size or cand.product.min_quantity
            if size > needed_quantity * prefs.max_pack_multiple:
                cand.reason = (
                    f"pack of {size} exceeds need {needed_quantity} "
                    f"by more than {prefs.max_pack_multiple}x"
                )
                continue
            kept.append(cand)
        if kept:
            viable = kept
        else:
            # Everything is oversized; keep the smallest rather than nothing.
            viable.sort(key=lambda c: c.product.pack_constraint
                        or c.product.min_quantity)
            viable = viable[:1]
            viable[0].reason = "smallest available pack still exceeds need"

    def metric(cand: Candidate) -> float | None:
        """The number this ranking is trying to minimise.

        With a known need it is kroner to cover it; without one there is
        nothing to cover, so value per measured unit is the only sensible
        question.
        """
        if count_need:
            return _count_cost(cand.product, needed_quantity)
        if needed_quantity:
            return _amount_cost(cand.product, needed_quantity)
        return cand.unit_price

    def key(cand: Candidate) -> tuple[float, float, float]:
        # tier 0 = ranked on the metric that answers the question,
        # tier 1 = ranked on a fallback, tier 2 = no price at all.
        price = metric(cand)
        if price is None or price <= 0:
            return (1, float("inf"), -cand.relevance)
        # Divide by relevance so a less-relevant product must be proportionally
        # cheaper to win, rather than winning on a rounding difference. With
        # min_relevance at 0.5 the worst case is a 2x handicap.
        adjusted = (price * _preference_bonus(cand.product, prefs)
                    / max(cand.relevance, 0.1))
        return (0, adjusted, -cand.relevance)

    viable.sort(key=key)

    # Prefer a clearly-more-relevant product when prices are near-identical.
    # Compared on the same metric the sort used, so the two cannot disagree.
    # Only within one tier: promoting a bag of unknown piece count over a
    # countable item on a relevance tie would undo the tiering above.
    if len(viable) > 1:
        head, second = viable[0], viable[1]
        head_price, second_price = metric(head), metric(second)
        if (
            key(head)[0] == key(second)[0]
            and head_price and second_price
            and second.relevance > head.relevance
            and second_price <= head_price * (1 + prefs.unit_price_margin)
        ):
            viable[0], viable[1] = second, head

    return viable


def pick_best(
    products: Sequence[Product],
    query: str = "",
    prefs: Preferences | None = None,
    *,
    needed_quantity: float | None = None,
    count_need: bool = False,
) -> Candidate | None:
    ranked = rank(products, query, prefs, needed_quantity=needed_quantity,
                  count_need=count_need)
    return ranked[0] if ranked else None


def compare_table(products: Sequence[Product], limit: int = 10) -> list[dict]:
    """Per-unit comparison rows, cheapest first within each unit group.

    Rows are deliberately narrow: the caller is choosing between products on
    price per unit, so anything that does not feed that decision is noise. The
    cross-group caveat is stated once by the caller rather than stamped onto
    every row.
    """
    buckets = group_by_unit(products)
    rows: list[dict] = []
    for group, items in buckets.items():
        items = sorted(
            [p for p in items if p.unit_price],
            key=lambda p: p.unit_price or float("inf"),
        )
        cheapest = items[0].unit_price if items else None
        for product in items[:limit]:
            row = {
                "ean": product.ean,
                "title": " ".join(
                    x for x in (product.title, product.subtitle) if x
                ),
                "price": product.effective_price,
                "unit_price": product.unit_price,
                "unit": product.canonical_compare_unit,
                "unit_group": group,
            }
            if cheapest and product.unit_price:
                row["vs_cheapest_pct"] = round(
                    (product.unit_price / cheapest - 1) * 100, 1
                )
            if product.on_sale:
                row["on_sale"] = True
            rows.append(row)
    return rows
