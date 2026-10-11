# Household profile

Two facts no catalogue can supply, and that otherwise get re-explained in prose
every week.

**Pantry.** A menu that buys honey, sesame seeds, curry powder and Dijon
mustard from scratch every Sunday is restocking a kitchen, not planning a week -
about 300 kr of one real basket. The shipped default covers ~70 staples:
seasoning, oils, vinegars, sweeteners, baking goods, and the sauces that live in
bottles and jars. Matching follows compound edges, so a pantry holding `karri`
covers `karripulver` and one holding `sennep` covers `dijonsennep` - but never
across a phrase, or `salt` would swallow `saltet torsk` and the menu would
quietly stop buying the fish.

**Exclusions.** Allergies, dislikes, house rules. An exclusion drops the
ingredient from the plan *and* refuses products containing it - the half that
hand-editing the menu misses, since dill turns up inside dressings and prawns
inside salads.

```
set_household(pantry=["kokosmelk", "fiskesaus"], exclude=["reker", "dill"])
```

Both stick for the session. `MENY_PANTRY` / `MENY_EXCLUDE` set the default for a
single-user deployment, and `MENY_PANTRY_REPLACE=1` replaces the shipped list
rather than extending it. `plan_week_menu` and `find_best_value` also take
`extra_pantry` and `exclude` for one call.
