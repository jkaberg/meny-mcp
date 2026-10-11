# Ingredient matching

## The shop already knows

Every search hit carries three levels of classification, and only the finest one
answers *what is this*:

| | Torskefilet 800g | Torsk i Saus | Avokado Olje | Avocado |
|---|---|---|---|---|
| `categoryName` | Fisk & skalldyr | Fisk & skalldyr | Matvarer | Frukt & grønt |
| `shoppingListGroupName` | Fisk | Ferdigretter | Matoljer | Frukt |
| **`shoppingListGroupName1`** | **Torsk** | **Fiskemåltid ferdig** | **Øvrig matolje** | **Avocado** |

Reading only the coarse two made *torsk* unanswerable. Every real fillet is
titled "Torskefilet" or "Torskeloins" - a prefix match worth 0.45, under the 0.5
relevance floor - while its groups say only "Fisk", shared with tinned cod roe
and cod-flavoured cat food. So the whole shortlist was discarded and the two
products titled bare *Torsk*, both from the fresh counter, were all that
survived: **429 and 519 kr/kg, when the cheapest genuine fillet is 144.75**.

The head-final rule is simply wrong for grocery compounds - in "torskefilet" the
*modifier* carries the ingredient and the head is the cut - and the shop's own
taxonomy is a better answer than tuning the score. Adding the leaf label as
relevance evidence took the candidate pool for `torsk` from 2 to 7 and for
`laks` from 5 to 11.

A label may inflect past the query (`Tomat` → *Tomater*) but may not grow a
whole morpheme: *Sitrongress* starts with "sitron", is lemongrass, and beat two
real lemons on price when it scored a full 1.00.

## The four failure kinds

A recipe says *avokado*. The catalogue contains avocado oil, avocado spread,
avocado in a gratin, and - spelled *Avocado* - the fruit. Ranking on word
overlap picks the oil, confidently, at 175 kr.

That is not a hypothetical. On one real week of planning, ten of twenty-five
ingredients resolved to something that shared a word with the request and
nothing else:

| Asked for | Got | Failure |
|---|---|---|
| avokado | Avokado Olje 250ml | ingredient → derived product |
| sitron | Cookie Sitron 54g | ingredient → baked good |
| torsk | Torsk i Saus (Fjordland) | raw ingredient → ready meal |
| cherrytomater | Cherrytomater Mutti | fresh → canned |
| bacon | Smårettbacon Kylling&Kalkun | pork → poultry |
| kjøttdeig storfe | Kjøttdeig Storfe&Svin | named species ignored |
| brokkoli | Brokkoli&Blomkål, frossen | fresh → frozen blend |
| spaghetti | Spaghetti Fersk | dry → fresh |
| ingefær | Ingefær Malt | root → ground |
| paprika | Paprika Padron | ordinary → a different variety |

The four kinds are distinguishable from the data, so [`matching.py`](../src/meny_mcp/matching.py)
names them instead of hoping a score sorts them out. Two of them **disqualify**
a product outright - there is no price at which an oil answers "avokado" - and
two scale its relevance down.

**Derived products** (disqualifying). Norwegian compounds are head-final, so
the tail of the word says what the thing *is*: "avokadoOLJE" is an oil,
"limeSAFT" is a squash, "fløteISPINNE" is an ice cream. Suffix matching is
restricted to heads of four letters or more with a real stem in front, because
otherwise `ris` ends in `is` and every bag of rice becomes an ice cream. Asking
for the derived thing still works: `soyasaus` matches a sauce, because the query
explains the head.

**Wrong species** (disqualifying). A request naming an animal accepts no other
animal, and some cuts imply theirs without naming it - bacon is pork, so
"Smårettbacon Kylling&Kalkun" is out. A request naming *no* animal still accepts
a blend, which is why plain "kjøttdeig" is unaffected.

**Wrong form** (relevance × 0.35). Fresh, dried, frozen, canned, smoked, salted
and cultured are different goods at different prices per kg. This needs no
per-ingredient knowledge: a request naming no form wants the *unmarked*
product, because the shop marks the variants and leaves the default bare.
"Ingefær" is the root; "Ingefær Malt" is the powder. Ask for `frossen brokkoli`
and the frozen one wins instead.

**Unexplained variety words** (relevance × 0.82 each). Colour, size, origin and
grade words are free - "Paprika Rød" is a paprika - but "Padron" is a different
pepper, and pays for it.

Nothing is discarded on a form mismatch alone. If the shop stocks nothing but
frozen brokkoli today, the frozen one comes back with the reason attached
rather than the ingredient being reported as unavailable.

### The freezer aisle is weaker evidence than the label

`environmentalCodes` carries `frysevare` outright, which is the only way to know
that "Torskefilet Blokk 400g" and "Torskefilet 800g First Price" are frozen -
neither says so in any text field. But being *sold* frozen means something
weaker than being marketed as the frozen variant of a normally-fresh good:
fillets, peas and berries are ordinarily sold that way. So a freezer-aisle
product takes the mild penalty (×0.7) rather than the severe one (×0.35), and
does not trigger the one-pack substitution rule - 400 g of frozen cod is 400 g
of cod, and ordering one pack "because it is a substitution" would under-buy.

A household that simply wants frozen goods says so once:

```python
set_household(accept_forms=["frossen"])
```

That is deliberately not the same as putting "frossen" in the query, which would
additionally mark every *fresh* product as failing to be frozen.

## The quantity consequence

A dried substitute is not just a different product, it invalidates the
arithmetic. A menu asking for 20 g of fresh grated ginger bought two 10 g jars
of the ground spice - roughly six times the ginger - and 10 g of fresh parsley
became three jars of dried, at 90 kr, for a garnish. When the form differs
severely the planner orders **one pack** and says so:

```json
"substitution": "tørket - the request named no form; 20 g fresh is roughly
                 3 g dried, so one pack is ordered rather than the fresh weight"
```

Milder differences (sliced, grated, cooked) deliberately do *not* trigger this.
Sliced bacon is still bacon by the gram, and treating it as a substitution would
order one pack where the recipe wanted four - the same bug in reverse.

## Spelling

Five more ingredients came back "no acceptable candidate" while sitting on the
shelf under another spelling or word order. Those are cheap to rescue, and the
retry costs one extra search **only on the failure path**:

| Asked for | Shelved as | How it is found |
|---|---|---|
| sjampinjong | Champignon | spelling table |
| dijonsennep | Sennep Dijon | compound split, reversed |
| svinefilet | Ytrefilet av svin | compound split, linking `-e-` dropped |
| karripulver | Karri Malt | compound split, head dropped |
| tortillalefser | Tortilla | leading word carries the identity |

Loanword orthography is handled without a table: both sides are folded onto a
common key (`c→k`, `ch→k`, `x→ks`, doubles collapsed), so *Avocado* answers
`avokado` and *Broccoli* answers `brokkoli`. A label and a query may also differ
by a short inflection in either direction - the shop shelves `tomat` as
*Tomater* and `aubergine` as *Aubergin* - but never by a whole morpheme, which
is what keeps *Sitrongress* from answering "sitron".

## When the ingredient is a category

Some words name no product at all. `biff` returns Mørbrad, Entrecote,
Indrefilet and Ytrefilet; not one of them is called a biff, every one scored a
flat 0.00, and the ingredient came back unbuyable. No query rewrite reaches
this - it is not a spelling problem.

The full product record answers it directly. All eight cuts list `biff` in
`synonyms`, five different mushrooms list `sopp`, and *Aubergin* lists
`aubergine`. So an ingredient that has failed both the plain search **and**
every spelling variant gets one last pass: the products the original search
already returned are re-fetched as full records and re-judged with the synonyms
present.

Three things keep that safe:

- **The array is not clean.** It carries brand and label aliases - `meny`,
  `bama`, `firstprice`, `nøkkelhullet`, `sunnere`, `mindre sukker`. A product's
  own brand, vendor and environmental labels are subtracted before matching,
  and multi-word entries must match the whole query as a phrase, so
  "mindre salt" never answers `salt`.
- **Exact entries only, no inflection tolerance.** The shop enumerates the
  forms itself (`aubergin`, `aubergine`, `auberginer`, `auberginen`,
  `auberginene` are five separate entries), so tolerance buys nothing - and it
  cost something: `sunn` reaches the marketing term `sunnere` within three
  letters.
- **Permission, not preference.** A synonym match lifts a product to *exactly*
  the relevance floor and no further, so it is selectable when nothing else
  answers and never outranks something actually named after the ingredient.

The pick is reported as `matched_by: "shop synonym"`, because it is the one
choice a reader cannot check from the title alone.

It costs nothing on the happy path: search hits carry no `synonyms` field, so
the rescue only fires after a failure, and the records it fetches are cached and
needed again for pack arithmetic anyway.

## Verification

Every pick in a week menu is re-judged against its **full product record**
before it reaches the basket, walking down the shortlist until one passes. The
full record carries the shop's own category label and the declared ingredient
list - which is where `hermetisk` and an excluded allergen actually become
visible; a search hit shows neither. It costs no extra HTTP: those records are
fetched for pack arithmetic and cart building anyway, and the client caches
them. Products thrown out at this stage are reported, not hidden:

```json
"rejected": ["Persille 12g glass Hindu: form: tørket"]
```
