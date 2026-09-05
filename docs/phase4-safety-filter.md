# Phase 4 — Safety filter (pipeline stage 1)

`airflow/dags/food_pipeline/safety_filter.py`. This is CLAUDE.md **stage 1**:
it removes disqualifying menu items *before* the Layer A calculator, the
Layer B `cluster_id` read, or the Layer C ranking touch the candidate pool.

## Guarantees

- **Pure rule-based.** No ML, no clustering, no ranking. The module imports
  only `logging`, `re`, `dataclasses`, `typing`, `__future__` — nothing from
  the rest of `food_pipeline`. A test (`test_module_has_no_ml_or_pipeline_imports`)
  parses the AST and fails if that ever changes. A clustering or ranking
  model can never be the sole safeguard against an allergen.
- **Always runs.** With empty restrictions it excludes 0 items and logs
  `"no restrictions — 0 excluded (filter still ran, N items)"`. It is never
  skipped.
- **Every exclusion carries a reason.** `SafetyResult` has `kept`,
  `excluded` (each an `Exclusion(menu_id, name, rule, reason)`),
  `undetermined` (diet cases that couldn't be decided), and
  `excluded_count`.

## Inputs

`apply_safety_filter(items: Iterable[MenuItem], restrictions: Restrictions)`

- `MenuItem(menu_id, name, ingredients, diet_tags)` — build one from a
  `menu_catalog` row with `menu_item_from_row(row)`.
- `Restrictions(allergies, avoid, diet)` — build from loose user input with
  `parse_restrictions(allergies=[...], diet="vegan", avoid=[...])`, which
  normalises wording ("Peanuts", "no pork", "gluten-free" → `nut`, avoid
  `pork`, `gluten`) and routes "no X" items to `avoid`.

## What it checks

| restriction | source | keywords (excerpt) |
|---|---|---|
| allergy `nut` | ingredient text | peanut/groundnut, almond, cashew, walnut, pecan, hazelnut, pistachio, macadamia, pine nut, "nut butter/oil", praline, marzipan, nougat, frangipane, nutella |
| allergy `shellfish` | ingredient text | shrimp, prawn, crab, lobster, crayfish, scampi, clam, mussel, oyster, scallop, squid/calamari, octopus, krill |
| allergy `dairy` | ingredient text | milk, cream(y), butter(y), cheese/cheesy, ranch, yogurt, whey, casein, ghee, buttermilk, custard, named cheeses, "sour/ice cream", lactose |
| allergy `egg` | ingredient text | egg/egg white/yolk, albumen, mayonnaise/mayo, aioli, meringue, egg noodle, egg wash |
| allergy `soy` | ingredient text | soy/soya/soybean, soy sauce, shoyu, tamari, edamame, tofu, tempeh, miso, natto, soy lecithin, TVP |
| allergy `gluten` / wheat | ingredient text | wheat, flour, barley, rye, malt, bulgur, couscous, semolina, farro, spelt, seitan, breadcrumbs/panko, pasta, noodles, cracker, beer, roux, **soy sauce** |
| allergy `fish` | ingredient text | fish, salmon, tuna, cod, anchovy, sardine, herring, mackerel, "fish sauce/stock", **worcestershire**, **caesar dressing**, surimi, bonito, dashi, roe |
| allergy `sesame` | ingredient text | sesame, tahini, sesame oil/seed, halva, gomashio, za'atar, **hummus** |
| avoid `pork` | ingredient text | pork, bacon, ham, prosciutto, pancetta, lard, chorizo, salami, pepperoni, gammon, carnitas, pig |
| avoid `beef` | ingredient text | beef, steak, brisket, corned beef, pastrami, veal, oxtail, sirloin, ribeye, ground/minced beef, hamburger |
| diet `vegan` | ingredients + tag | any meat/poultry/fish/shellfish/dairy/egg/honey/gelatin/lard/rennet/carmine; cleared by API tag `vegan` |
| diet `vegetarian` | ingredients + tag | any meat/poultry/fish/shellfish + gelatin/lard/rennet/anchovy/fish sauce; cleared by tags `vegan`/`vegetarian`/`lacto*/ovo*` |
| diet `pescatarian` | ingredients + tag | any meat/poultry (fish & shellfish allowed); cleared by tags `vegan`/`vegetarian`/`pescatarian` |
| diet `halal` | ingredients | pork + alcohol (wine/beer/rum/mirin/sake…) + gelatin/lard |
| diet `kosher` | ingredients | pork + shellfish + gelatin/lard + catfish/rabbit |

**Suppressors** cancel a match when a non-allergen qualifier is in the same
ingredient string: e.g. `coconut milk` / `almond milk` / `peanut butter` /
`cocoa butter` don't trigger `dairy`; `coconut` / `nutmeg` / `butternut` /
`water chestnut` don't trigger `nut`; `almond flour` / `buckwheat` /
`rice noodles` don't trigger `gluten`; `eggplant` doesn't trigger `egg`.

## Decision order (per item × restriction)

1. Scan ingredient names for the restriction's keywords.
2. **Any ingredient match → EXCLUDE** (a concrete match wins even over a
   "free-from" API tag).
3. Else, a clearing diet tag (`vegan`, `gluten free`, `dairy free`, …) → keep.
4. Else, the item has ingredient data → keep (no evidence of a conflict).
5. Else (no ingredients **and** no clearing tag):
   - allergy / avoidance → **EXCLUDE** as `unverifiable:<key>` (conservative
     — a medical allergy filter should not pass what it can't check);
   - diet → keep, record an `undetermined` note, and log it (never guess).

`halal` / `kosher` always log a partial-determination note: certification and
slaughter method / meat-dairy separation can't be verified from ingredient
text.

## Known limitation — state this plainly

**The safety filter matches against ingredient text and the known allergen /
diet tag fields only. It can miss an allergen that is phrased unusually or
hidden inside a compound ingredient. Anyone with a serious food allergy must
still check the full ingredient list themselves. This system is not a sole
safeguard.**

Concretely, from `tests/test_safety_filter.py`:

*Caught* (the compound name still contains a tell-tale word):
`creamy ranch dressing` → dairy · `worcestershire sauce` → fish ·
`oyster sauce` → shellfish · `panko breadcrumbs` → gluten ·
`mayonnaise` → egg.

*Missed* (no tell-tale substring — these items wrongly pass, asserted in the
test so the gap stays visible):
`pesto` hides parmesan (dairy) **and** pine nuts (nut) ·
`thai red curry paste` hides shrimp paste (shellfish) ·
`hoisin sauce` hides wheat (gluten) ·
`fresh ladyfingers` hides egg ·
`vegetable broth` can hide soy.

Also: Spoonacular's `diets[]` tags are used only to *clear* a diet, never to
prove a violation (their absence is not evidence). Halal/kosher can only be
partially assessed. New allergen keywords should be added as they surface in
real Spoonacular ingredient data.

## Tests

`tests/test_safety_filter.py` — **34 tests, all passing**: every named
allergen; no-pork/no-beef; each diet type; `vegan + nut allergy` combined;
tag-clears-diet-but-not-allergen; case/wording variants
(`peanut`/`Peanuts`/`PEANUT`/`groundnut`/`Ground Nuts`/`tree nut`);
suppressors (coconut milk, nutmeg, almond flour, buckwheat, eggplant);
compound-ingredient catches **and** misses; unverifiable-when-no-ingredients;
empty restrictions → `excluded_count == 0` with the filter still invoked;
the no-ML-imports AST check.
