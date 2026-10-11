"""Is this product the thing that was asked for?

Token overlap answers "does the title mention the ingredient". That is not the
same question, and the gap between them is where every embarrassing pick comes
from. Measured against a real week's planning, ten of twenty-five ingredients
resolved to something that shared a word with the request and nothing else:

    avokado          -> Avokado Olje 250ml          (derived product)
    sitron           -> Cookie Sitron 54g           (derived product)
    torsk            -> Torsk i Saus, Fjordland     (prepared dish)
    cherrytomater    -> Cherrytomater, hermetisk    (wrong form)
    ingefær          -> Ingefær Malt                (wrong form)
    brokkoli         -> Brokkoli & Blomkål, frossen (wrong form + blend)
    spaghetti        -> Spaghetti Fersk             (wrong form)
    bacon            -> Smårettbacon Kylling&Kalkun (wrong species)
    kjøttdeig storfe -> Kjøttdeig Storfe&Svin       (wrong species)

The four failure kinds are distinguishable from the data, so this module names
them rather than hoping a relevance score sorts them out:

**Derived products.** Norwegian compounds are head-final, so the last part of
the word says what the thing *is*: "avokadoOLJE" is an oil, "limeSAFT" is a
squash, "fløteISPINNE" is an ice cream. If the head is something built *from*
an ingredient and the request did not ask for it, the product is disqualified -
not down-ranked. There is no price at which an oil answers "1 stk avokado".

**Prepared dishes.** "Torsk i saus" is dinner, not cod.

**Form.** Fresh, dried, frozen, canned and smoked are different goods sold at
different prices per kg. The rule needs no per-ingredient knowledge: a request
that names no form wants the *unmarked* product, because the shop marks the
variants and leaves the default bare. "Ingefær" is the root; "Ingefær Malt" is
the powder.

**Species.** A request naming an animal accepts no other animal, and a few
cuts imply their animal without naming it (bacon is pork). "Storfe&Svin" is
therefore not an answer to "kjøttdeig storfe".

The last piece is spelling. Five more ingredients came back as "no acceptable
candidate" while sitting on the shelf under a different spelling or word order
(``sjampinjong`` / ``champignon``, ``dijonsennep`` / ``Sennep Dijon``). Those
are cheap to rescue: see :func:`query_variants`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

_WORD_RE = re.compile(r"[a-z0-9æøå]+")


def normalise(text: str) -> str:
    """Casefold and strip diacritics, but keep Norwegian æ/ø/å distinct."""
    text = text.lower()
    keep = {"æ", "ø", "å"}
    out = []
    for ch in text:
        if ch in keep:
            out.append(ch)
        else:
            decomposed = unicodedata.normalize("NFD", ch)
            out.append("".join(c for c in decomposed
                               if unicodedata.category(c) != "Mn"))
    return "".join(out)


def tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return set(_WORD_RE.findall(normalise(text)))


# Loanwords are shelved in whichever orthography the supplier used, and the
# Norwegian one is not always it. Meny sells *Avocado*, not *Avokado* - which
# scored a flat 0.00 against the obvious query, leaving avocado OIL as the only
# thing that matched the word at all. Folding both sides onto a common key
# costs nothing and fixes the whole family: avocado/avokado, broccoli/brokkoli,
# cacao/kakao, courgette/kurgett.
_FOLDINGS: tuple[tuple[str, str], ...] = (
    ("ch", "k"), ("ph", "f"), ("th", "t"), ("qu", "kv"),
    ("x", "ks"), ("z", "s"), ("c", "k"), ("w", "v"),
)


def fold(word: str) -> str:
    """An orthography-insensitive key for one word."""
    for source, target in _FOLDINGS:
        word = word.replace(source, target)
    out: list[str] = []
    for ch in word:
        if not out or out[-1] != ch:   # collapse doubles: brokkoli -> brokoli
            out.append(ch)
    return "".join(out)


# Two words may differ by a short ending and still be the same word - Norwegian
# inflects with short suffixes, and it happens in both directions: the shop
# shelves "Tomat" as *Tomater* and "aubergine" as *Aubergin*. What they may not
# differ by is a whole morpheme: "Sitrongress" starts with "sitron", is
# lemongrass, and beat two real lemons on price when it scored a full 1.00.
MAX_INFLECTION = 3


def inflection_of(a: str, b: str) -> bool:
    """True when two words differ only by a short inflectional ending."""
    shorter, longer = sorted((a, b), key=len)
    return (longer.startswith(shorter)
            and len(longer) - len(shorter) <= MAX_INFLECTION)


# ---------------------------------------------------------------------------
# Derived products
# ---------------------------------------------------------------------------
# Head nouns that mean "made from the ingredient" rather than "is the
# ingredient". Matched as a whole token, or as the tail of a compound.
DERIVED_HEADS: frozenset[str] = frozenset({
    # extracts and liquids
    "olje", "juice", "saft", "nektar", "brus", "sirup", "essens", "ekstrakt",
    "aroma", "eddik", "likør", "vin",
    # baking and confectionery
    "kjeks", "cookie", "cookies", "kake", "kaker", "bolle", "boller",
    "muffins", "muffin", "pai", "vaffel", "vafler", "pannekake", "pannekaker",
    "brownie", "donut", "smultring", "wienerbrød", "lefse",
    # frozen desserts
    "iskrem", "ispinne", "isbåt", "sorbet", "milkshake", "smoothie",
    # dairy carrying a fruit's name. Exactly the avokado-olje failure in
    # another aisle: "Yoghurt Jordbær" at 33 kr/kg outbids real strawberries
    # at 120 kr/kg for the query "jordbær", and is not fruit.
    "yoghurt", "youghurt", "skyr", "kesam", "kvarg", "kefir", "pudding",
    "ostekake", "melkedrikk", "drikkeyoghurt",
    # sweets and snacks
    "sjokolade", "godteri", "drops", "pastill", "tyggegummi", "snacks",
    "chips", "popcorn", "nachos",
    # spreads and sauces
    "syltetøy", "marmelade", "gele", "pure", "puré", "mos", "dressing",
    "majones", "ketchup", "dip", "saus", "paté", "postei", "leverpostei",
    "pålegg", "smørepålegg", "spread", "salat",
    # other categories that share a word with food
    "te", "kaffe", "tilskudd", "kosttilskudd", "sjampo", "shampo", "såpe",
    "parfyme", "salve", "tannkrem", "kremtube", "duft", "servietter",
    "lys", "pynt",
})

# Suffix matching only fires on heads long enough to be unambiguous, and only
# when a real stem precedes them. Without this, "ris" ends with "is" and every
# bag of rice is an ice cream.
_MIN_SUFFIX_HEAD = 4
_MIN_STEM = 3

# Ready meals. Prefix-matched, so "smårettbacon" and "gryterett" both land.
PREPARED_PREFIXES: tuple[str, ...] = (
    "ferdigrett", "smårett", "gryterett", "porsjonsrett", "middagsrett",
    "gratinert", "gratin", "panert", "lapskaus", "risotto", "lasagne",
    "paella", "fløtepotet",
)
PREPARED_TOKENS: frozenset[str] = frozenset({
    "suppe", "wok", "pizza", "stuing", "stuet", "middag", "rett",
})


# ---------------------------------------------------------------------------
# Form
# ---------------------------------------------------------------------------
# Prefix-matched against title, subtitle and the shop's own category labels -
# "hermetisk" shows up as often in the group name as in the title.
FORM_MARKERS: dict[str, tuple[str, ...]] = {
    "tørket": ("tørket", "tørkede", "tørka", "tørr", "malt", "malte",
               "pulver", "frysetørk", "granulat"),
    # "tint" is the same story from the other end: a thawed fillet was frozen,
    # is priced as frozen, and does not keep like the fresh one.
    "frossen": ("frossen", "frosne", "fryst", "dypfryst", "frysevare", "frys",
                "tint", "tinte"),
    "hermetisk": ("hermetis", "syltet", "syltede", "konserv", "pickles",
                  "i lake"),
    "røkt": ("røkt", "røykt", "røkte", "gravet", "gravlaks", "spekt"),
    "saltet": ("saltet", "lettsaltet", "spekesalt", "klippfisk"),
    "marinert": ("marinert", "marinerte", "krydret", "krydrede"),
    "tilberedt": ("kokt", "kokte", "stekt", "stekte", "grillet", "bakt",
                  "bakte"),
    "syrnet": ("syrnet", "syrnede", "fermentert", "kulturmelk"),
    "fersk": ("fersk", "ferske"),
    "bearbeidet": ("revet", "skivet", "oppskåret", "hakket", "most", "presset"),
}

# Markers that must match a label word EXACTLY, because the prefix would be
# ambiguous. The shop shelves the dried jar of parsley under "Krydder" and the
# living pot under "Krydderurter" - one letter of prefix apart, and the whole
# difference between a 30 kr garnish and a 2 491 kr/kg one.
FORM_EXACT: dict[str, frozenset[str]] = {
    "tørket": frozenset({"krydder"}),
}

# Forms that change what the product *is* - a different price per kg, a
# different quantity in a recipe, often a different aisle.
SEVERE_FORMS: frozenset[str] = frozenset({
    "tørket", "frossen", "hermetisk", "røkt", "saltet", "syrnet",
})

# Rough dried:fresh weight ratios, for the warning on a substituted form. A
# recipe's 20 g of grated ginger is not 20 g of ground ginger.
DRIED_TO_FRESH: dict[str, float] = {
    "ingefær": 6.0, "persille": 10.0, "basilikum": 10.0, "timian": 8.0,
    "oregano": 10.0, "dill": 10.0, "gressløk": 10.0, "koriander": 10.0,
    "rosmarin": 8.0, "estragon": 10.0, "mynte": 10.0, "salvie": 8.0,
    "hvitløk": 6.0, "løk": 6.0, "sopp": 8.0, "chili": 6.0,
}


# ---------------------------------------------------------------------------
# Species
# ---------------------------------------------------------------------------
SPECIES_TOKENS: frozenset[str] = frozenset({
    "storfe", "okse", "oksekjøtt", "svin", "svinekjøtt", "gris", "kylling",
    "kalkun", "and", "gås", "lam", "sau", "får", "kalv", "rein", "elg",
    "hjort", "vilt", "kanin", "hest",
    "laks", "ørret", "torsk", "sei", "hyse", "lyr", "makrell", "sild",
    "tunfisk", "reke", "reker", "scampi", "krabbe", "blåskjell", "kveite",
    "uer", "breiflabb", "steinbit", "røye", "sjøkreps",
})

# Cuts and products that name no animal but only come from one. Matched as a
# substring, so "smårettbacon" still implies pork.
IMPLIED_SPECIES: dict[str, str] = {
    "bacon": "svin", "skinke": "svin", "ribbe": "svin", "medister": "svin",
    "spekeskinke": "svin", "serrano": "svin", "chorizo": "svin",
    "pancetta": "svin", "sideflesk": "svin", "flesk": "svin",
    "entrecote": "storfe", "roastbiff": "storfe", "høyrygg": "storfe",
    "bibringe": "storfe", "gravlaks": "laks", "røkelaks": "laks",
}


# ---------------------------------------------------------------------------
# Modifiers that say nothing about identity
# ---------------------------------------------------------------------------
# Colour, size, origin and grade words. A product is not a different product
# for carrying one, so they must not count as unexplained title words - but
# a *variety* name ("Padron", "Cherry", "Piccolini") is not on this list, and
# should cost something, because it usually is a different product.
BENIGN_MODIFIERS: frozenset[str] = frozenset({
    "rød", "rødt", "røde", "grønn", "grønne", "gul", "gule", "hvit", "hvite",
    "brun", "brune", "sort", "svart", "stor", "store", "liten", "små",
    "norsk", "norske", "økologisk", "økologiske", "øko", "ny", "nye", "fin",
    "fine", "grov", "grove", "mager", "magert", "lett", "ekstra", "original",
    "klassisk", "naturell", "vanlig", "premium", "utvalgte", "utvalgt",
})

# Words describing how a thing is sold, not what it is. "Sitron stykk" is a
# lemon; "Sitron 3pk" is three lemons.
PACKAGING_WORDS: frozenset[str] = frozenset({
    "stk", "stykk", "pk", "pakke", "pose", "nett", "boks", "glass", "beger",
    "kg", "g", "gram", "l", "dl", "ml", "cl", "pr", "ca", "net", "kartong",
    "brett", "skål", "flaske", "tube", "spann", "kurv",
})


# ---------------------------------------------------------------------------
# Query rewriting
# ---------------------------------------------------------------------------
# Documented spelling and word-order misses: the ingredient was on the shelf,
# under a name the search did not reach.
SPELLINGS: dict[str, tuple[str, ...]] = {
    "sjampinjong": ("champignon",),
    "sjampinjonger": ("champignon",),
    "champignon": ("sjampinjong",),
    "aubergine": ("eggplante",),
    "squash": ("zucchini",),
    "zucchini": ("squash",),
    "mais": ("maiskorn",),
    "kikerter": ("kikert",),
    "kikertbønner": ("kikert",),
    "vårløk": ("salatløk",),
    "matfløte": ("fløte matfløte",),
    "creme fraiche": ("crème fraîche", "seterrømme"),
    "chorizo": ("chorizopølse",),
    "parmesan": ("parmigiano",),
    "koriander": ("koriander frø", "koriander fersk"),
}

# Compound heads used to split a query the shop spells the other way round:
# "dijonsennep" is shelved as "Sennep Dijon".
COMPOUND_HEADS: tuple[str, ...] = (
    "kjøttdeig", "kotelett", "lefser", "krydder", "pulver", "sennep",
    "bønner", "skiver", "pølse", "juice", "lefse", "filet", "saus", "olje",
    "gryn", "biff", "stek", "rygg", "vinge", "deig", "mel", "ost", "lår",
)

# Linking letters Norwegian glues compounds with; dropping them turns the
# modifier back into a searchable word ("svine" -> "svin").
_LINKERS = ("s", "e")


def query_variants(query: str, limit: int = 4) -> list[str]:
    """Other spellings worth trying when a search finds nothing usable.

    Returns alternatives only - never the original, which the caller has
    already tried. Ordered cheapest-guess-first and capped, because each one
    costs a round trip and these fire on the failure path for a whole menu.
    """
    base = normalise(query).strip()
    out: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip()
        if candidate and candidate != base and candidate not in out:
            out.append(candidate)

    for alt in SPELLINGS.get(base, ()):
        add(alt)

    # A compound the shop writes as two words, in the other order.
    for head in COMPOUND_HEADS:
        if base.endswith(head) and len(base) - len(head) >= _MIN_STEM:
            stem = base[: -len(head)]
            for linker in _LINKERS:
                if stem.endswith(linker) and len(stem) > _MIN_STEM:
                    stem = stem[: -len(linker)]
                    break
            add(f"{head} {stem}")
            add(stem)
            break

    # Last resort for a long compound with an unknown head: the leading word
    # usually carries the identity ("tortillalefser" -> "tortilla").
    if not out and len(base) >= 10 and " " not in base:
        add(base[:-4])

    return out[:limit]


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------
def is_form_word(word: str) -> bool:
    return any(word.startswith(prefix)
               for prefixes in FORM_MARKERS.values() for prefix in prefixes)


def identity_words(words: set[str]) -> set[str]:
    """The words that say *what the thing is*.

    Form, packaging and grade words are stripped, because they are judged
    separately and scoring them as content corrupts the comparison. Asking for
    "frossen brokkoli" and scoring "frossen" as an identity token makes plain
    *Brokkoli* only half a match for a request it answers completely, and can
    push the correct frozen product under the relevance floor on the strength
    of the word the user typed to find it.
    """
    kept = {w for w in words
            if w not in PACKAGING_WORDS
            and w not in BENIGN_MODIFIERS
            and not is_form_word(w)}
    return kept or set(words)


@dataclass(frozen=True)
class Ask:
    """What the user asked for, parsed once and reused per candidate."""

    query: str
    words: frozenset[str]
    forms: frozenset[str]
    species: frozenset[str]
    heads: frozenset[str]      # derived heads the user explicitly wants
    # Forms the household will accept without having named them. Distinct from
    # `forms` in both directions, which is the whole point: a tolerated form
    # costs a candidate nothing, and its ABSENCE costs nothing either. Putting
    # "frossen" in `forms` instead would penalise every fresh product for not
    # being frozen, which is not what "frozen is fine" means.
    tolerated: frozenset[str] = frozenset()

    @property
    def food_words(self) -> frozenset[str]:
        return frozenset(identity_words(set(self.words)))

    @property
    def accepted_forms(self) -> frozenset[str]:
        return self.forms | self.tolerated


def _forms_in(words: set[str]) -> set[str]:
    found = set()
    for form, prefixes in FORM_MARKERS.items():
        if any(word.startswith(prefix) for word in words for prefix in prefixes):
            found.add(form)
    for form, exact in FORM_EXACT.items():
        if words & exact:
            found.add(form)
    return found


def _species_in(words: set[str]) -> set[str]:
    found = set()
    for word in words:
        if word in SPECIES_TOKENS:
            found.add(word)
        for cut, animal in IMPLIED_SPECIES.items():
            if cut in word:
                found.add(animal)
    return found


def _derived_heads_in(words: set[str]) -> set[str]:
    """Derived heads present as a whole word or as a compound tail."""
    found = set()
    for word in words:
        if word in DERIVED_HEADS:
            found.add(word)
            continue
        for head in DERIVED_HEADS:
            if (
                len(head) >= _MIN_SUFFIX_HEAD
                and word.endswith(head)
                and len(word) - len(head) >= _MIN_STEM
            ):
                found.add(head)
                break
    return found


def parse_ask(query: str, tolerated: Iterable[str] | None = None) -> Ask:
    words = tokens(query)
    return Ask(
        query=query,
        words=frozenset(words),
        forms=frozenset(_forms_in(words)),
        species=frozenset(_species_in(words)),
        heads=frozenset(_derived_heads_in(words)),
        tolerated=frozenset(f.strip().lower() for f in (tolerated or ()) if f),
    )


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------
@dataclass
class Verdict:
    """Whether a candidate may answer the request, and how well.

    ``ok=False`` is a disqualification, not a penalty: no price makes an oil
    the right answer to "avokado". ``factor`` scales relevance for the softer
    mismatches, where the product is the right thing in the wrong state.
    """

    ok: bool = True
    factor: float = 1.0
    reasons: list[str] = field(default_factory=list)
    forms: frozenset[str] = frozenset()      # every form the product carries
    unwanted: frozenset[str] = frozenset()   # the ones nobody asked for
    # Unwanted forms that were inferred from shelving rather than from the
    # product's own name, and so are not treated as severe.
    mild_forms: frozenset[str] = frozenset()

    @property
    def note(self) -> str | None:
        return "; ".join(self.reasons) or None

    @property
    def form_mismatch(self) -> bool:
        """A substitution that changes the *quantity*, not just the ranking.

        Deliberately narrower than "carries an unwanted form". Sliced bacon is
        still bacon by the gram, and treating it as a substitution would order
        one pack where the recipe wanted four - the arithmetic bug in reverse.
        Only the severe forms (dried, frozen, canned, smoked, salted, cultured)
        make the recipe's amount stop meaning what it says.

        A freezer-aisle product whose name never claims to be frozen is exempt
        for the same reason: 400 g of frozen cod fillet is 400 g of cod, so
        ordering one pack "because it is a substitution" would under-buy.
        """
        return bool((self.unwanted - self.mild_forms) & SEVERE_FORMS)


def _blend(title: str | None) -> bool:
    """A two-ingredient product: 'Brokkoli & Blomkål', 'Storfe og Svin'."""
    text = normalise(title or "")
    return "&" in text or re.search(r"\b(og|and)\b", text) is not None


def judge(
    title: str | None,
    subtitle: str | None,
    group: str | None,
    category: str | None,
    ask: Ask,
    leaf: str | None = None,
    freezer: bool = False,
) -> Verdict:
    """Decide whether this product can answer ``ask``, and at what discount.

    Kept free of the Product model so it can be applied to a search hit and to
    the full record without change - the verification pass in menu planning
    re-runs it against the fuller record, where the shop's own category label
    is populated and "hermetisk" finally becomes visible.

    ``leaf`` is the shop's most specific group label (``Torsk``, ``Avocado``,
    ``Fiskemåltid ferdig``). It is judged like the other labels rather than
    given its own rule: "Hermetisk fisk" makes a tin of cod roe visibly canned
    without anything in its title saying so.

    ``freezer`` is the ``frysevare`` environmental code - the product is filed
    under frozen goods. Treated as a MILD mismatch, not a severe one, because
    it means something weaker than the word "frossen" in a title. Fillets,
    peas and berries are ordinarily sold frozen; a product marketed as the
    frozen *variant* of a normally-fresh good says so in its name and is caught
    by :data:`FORM_MARKERS` at full strength.
    """
    verdict = Verdict()
    name_words = tokens(title) | tokens(subtitle)
    label_words = name_words | tokens(group) | tokens(category) | tokens(leaf)

    # --- derived product: made FROM the ingredient, not the ingredient.
    unwanted_heads = _derived_heads_in(name_words) - ask.heads
    if unwanted_heads:
        # A query token that itself ends in the head means the user asked for
        # it ("soyasaus" wants a sauce), so only heads no query word explains
        # are disqualifying.
        unwanted_heads = {
            head for head in unwanted_heads
            if not any(word.endswith(head) for word in ask.words)
        }
    if unwanted_heads:
        verdict.ok = False
        verdict.reasons.append(
            f"derived product ({', '.join(sorted(unwanted_heads))})")
        return verdict

    # --- prepared dish
    if not (ask.words & PREPARED_TOKENS) and not any(
        word.startswith(p) for word in ask.words for p in PREPARED_PREFIXES
    ):
        prepared = [
            word for word in name_words
            if word in PREPARED_TOKENS
            or any(word.startswith(p) for p in PREPARED_PREFIXES)
        ]
        if prepared:
            verdict.ok = False
            verdict.reasons.append(f"prepared dish ({prepared[0]})")
            return verdict

    # --- species: naming an animal excludes the others.
    if ask.species:
        found = _species_in(name_words)
        extra = found - set(ask.species)
        if extra:
            verdict.ok = False
            verdict.reasons.append(
                f"wrong species ({', '.join(sorted(extra))}, "
                f"asked for {', '.join(sorted(ask.species))})")
            return verdict

    # --- form: an unasked-for form is the wrong good, not a cheaper one.
    found_forms = _forms_in(label_words)
    # The freezer aisle only counts as evidence of frozen when nothing in the
    # product's own text already said so - otherwise it would double-penalise.
    freezer_only = freezer and "frossen" not in found_forms
    if freezer:
        found_forms = found_forms | {"frossen"}
    verdict.forms = frozenset(found_forms)
    unwanted = found_forms - set(ask.accepted_forms)
    verdict.unwanted = frozenset(unwanted)
    if unwanted:
        # A frozen good known only from the freezer aisle is downgraded to the
        # mild penalty; anything else severe keeps the full one.
        severe = unwanted & SEVERE_FORMS
        if freezer_only and severe == {"frossen"}:
            severe = frozenset()
        verdict.factor *= 0.35 if severe else 0.7
        verdict.reasons.append(f"form: {', '.join(sorted(unwanted))}")
        if freezer_only:
            verdict.mild_forms = verdict.mild_forms | {"frossen"}
    missing = set(ask.forms) - found_forms
    if missing:
        verdict.factor *= 0.5
        verdict.reasons.append(f"not {', '.join(sorted(missing))}")

    # --- blend: one request, two ingredients in the pack.
    if _blend(title) and len(ask.food_words) < 2:
        verdict.factor *= 0.7
        verdict.reasons.append("mixed product")

    return verdict


# ---------------------------------------------------------------------------
# The shop's own synonyms
# ---------------------------------------------------------------------------
def synonyms_name(synonyms: Iterable[str] | None, ask: Ask,
                  ignore: Iterable[str | None] = ()) -> bool:
    """True when the shop's synonym list says this product answers ``ask``.

    Some ingredients are a *category*, not a product, and no amount of string
    work on titles will find them. "biff" returned eight steak cuts - Mørbrad,
    Entrecote, Indrefilet, Ytrefilet - every one of them scoring a flat 0.00,
    because not one is called a biff. The full product record settles it: all
    eight list ``biff`` in ``synonyms``. So does *Aubergin* for "aubergine",
    and so do five different mushrooms for "sopp".

    Matched against WHOLE entries, exactly, and single-word entries only
    against single query tokens. The array is not a clean identity signal: it
    also carries brand and label noise (``meny``, ``bama``, ``firstprice``,
    ``nøkkelhullet``, ``sunnere``, ``mindre sukker``) that a loose match would
    turn into false positives. Multi-word entries therefore have to match the
    whole query as a phrase, which is what keeps "mindre salt" from answering
    "salt".

    No inflection tolerance either, deliberately - unlike the group labels,
    which carry one form each. The shop enumerates the forms here itself
    (``aubergin``, ``aubergine``, ``auberginer``, ``auberginen``,
    ``auberginene`` are five separate entries), so tolerance would buy nothing
    and did cost something: "sunn" reaches the marketing term ``sunnere``
    within three letters, and thousands of products carry it.

    Callers must treat this as permission, not preference - see
    :func:`pricing.rank`, which lifts a synonym match to exactly the relevance
    floor and no further. The record is only fetched on the failure path, so a
    product that something else already answered never pays for this.
    """
    if not synonyms:
        return False
    entries = {normalise(str(s)).strip() for s in synonyms if s}
    # A product's own brand, vendor and quality labels are listed here as
    # search aliases - "meny", "bama", "firstprice", "nøkkelhullet". They say
    # who made it, not what it is, so they can never establish identity.
    entries -= {normalise(str(word)).strip() for word in ignore if word}
    entries.discard("")
    if not entries:
        return False
    phrase = normalise(ask.query).strip()
    if phrase in entries:
        return True
    single = {e for e in entries if " " not in e}
    wanted = ask.food_words
    return bool(wanted) and wanted <= single


def dried_equivalent(ingredient: str, fresh_grams: float) -> float | None:
    """Dried grams standing in for a fresh amount, when a ratio is known.

    The direction that matters in practice: a recipe states the fresh weight,
    the shop sells the dried jar, and buying the fresh weight in dried form
    (20 g of ground ginger for 20 g of root) is several times too much.
    """
    words = tokens(ingredient)
    for name, ratio in DRIED_TO_FRESH.items():
        if name in words or any(word.endswith(name) for word in words):
            return round(fresh_grams / ratio, 1)
    return None
