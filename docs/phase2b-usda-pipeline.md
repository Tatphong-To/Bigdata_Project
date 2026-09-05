# Phase 2b — TheMealDB → USDA estimated nutrition

Supplements the recipe catalog with TheMealDB recipes (which carry no
nutrition of their own) by estimating nutrition per ingredient from USDA
FoodData Central. Separate from the Phase 1 Spoonacular path —
`spoonacular.py` is untouched.

## Modules (all in `airflow/dags/food_pipeline/`)

| module | responsibility |
|---|---|
| `themealdb.py` | fetch recipes (test key `1`), parse `strIngredientN` / `strMeasureN` into `(name, quantity_text)` pairs. Light retry (flaky host). |
| `usda_client.py` | search FDC for an ingredient name, return candidate `UsdaFood`s with macros **per 100 g**. Key from `USDA_FDC_API_KEY` — **no `DEMO_KEY` fallback**. Retry on transient 400/5xx/timeout (api.data.gov edge is flaky); 429 surfaced with `Retry-After`. |
| `unit_converter.py` | free-text measure → grams. Explicit supported units; anything else **rejected with a reason**, never estimated. |
| `ingredient_matcher.py` | fuzzy-match TheMealDB name ↔ USDA description, confidence in [0,1]. Threshold is **config** (`MatchConfig.min_confidence`). Below threshold → rejected + logged. |
| `compute_recipe_nutrition.py` | orchestrate the three above, sum matched ingredients, compute `pct_calories_from_*` via `features.compute_feature_row` (identical formula), tag `nutrition_source='usda_estimated'`. |

## Unit converter — what is supported

- **Mass (exact):** `g`, `kg`, `mg`, `oz`, `lb` (+ common aliases).
- **Volume (needs density):** `ml`, `l`, `tsp`, `tbsp`, `cup`, `fl oz`,
  `pint`, `quart`, `gallon`. Converted via `UnitConverterConfig.g_per_ml`
  (default **1.0**, water-equivalent). This single assumption is documented
  and overridable: pass a real `g_per_ml`, per-ingredient `density_overrides`
  (substring → g/ml), or `allow_volume=False` to reject all volume.
- **Rejected (never estimated):** empty measures; non-quantitative
  ("to taste", "a pinch", "handful", "as needed", …); count-based with no
  unit ("2", "1 onion"); unknown units ("1 can", "2 cloves", "1 sprig").
  Each returns `ConversionResult(grams=None, reason=...)`.

Fractions handled: `1/2`, `1 1/2`, `.5`, unicode `½`/`1½` etc.

## Ingredient matcher — confidence & logging

- Score = `0.5 * difflib ratio + 0.5 * token Jaccard` on noise-stripped,
  crudely-singularised names, with a small bonus for subset/equal token sets.
  No external fuzzy-match dependency.
- `MatchConfig.min_confidence` (default 0.6) is the **only** acceptance gate
  and is passed in — never hard-coded at a call site.
- Below threshold → `accepted=False`, `food=None`, **WARNING logged** with the
  query, best candidate, and score. Nothing is guessed.
- Accepted but `< log_accepted_below` (default 0.75) → **INFO logged** so
  borderline estimates stay visible (CLAUDE.md Phase 2b rule 2).
- `require_all_macros` (default true) drops candidates missing any macro.

## Serving basis

TheMealDB has **no servings count**. `pct_calories_from_*` are
scale-invariant so they are always produced. `calories_per_serving` needs a
servings number: if the caller doesn't supply one, it is left `None` and the
row is `complete=False` (cannot fully join Phase 3 clustering). **No servings
count is invented.** Phase 3 decides how to handle incomplete rows.

## Recipe-level completeness guard

Skipped ingredients bias the ratios **systematically**, not as noise: the
percentage base excludes the macros of whatever was dropped. Live example —
*Teriyaki Chicken Casserole*: `chicken [2]` (count, no unit) and
`vegetables [1 (12 oz.)]` (unparseable) are both rejected, leaving rice + soy
sauce. The result still sums to ~100% (`P11/C88/F5`) and *looks* valid while
being wrong — the protein/fat anchor is missing from the base.

`compute_recipe_nutrition` computes two completeness sub-metrics per recipe:

| metric | definition |
|---|---|
| `count_completeness` | `matched_ingredient_count / total_ingredient_count` |
| `calorie_completeness` | `matched_calories / (matched_calories + estimated missing calories)` |

Estimated missing calories is a **guard-only heuristic** (never nutrition):
a skipped ingredient that converted to grams and had USDA candidates is
weighted by `grams × best-candidate kcal/100 g`; one that converted with no
candidates by `grams × guard_fallback_kcal_per_g`; one that never converted
by `guard_fallback_grams_per_unquantified × guard_fallback_kcal_per_g`; a
non-quantitative measure ("pinch", "to taste") counts as **0**.

`CompletenessConfig`:

- `min_completeness` — the gate. **Config, never hard-coded at a call site**
  (same rule as `MatchConfig.min_confidence`). Default is **PROVISIONAL**
  (0.70) pending confirmation against the live numbers below.
- `basis` — `"count"` | `"calorie"` | `"min"` (default `"min"`: strict on
  both axes).
- `guard_fallback_kcal_per_g` (2.0), `guard_fallback_grams_per_unquantified`
  (100.0).

**Below threshold → the row is dropped**, not flagged: `pct_calories_from_*`
and `calories_per_serving` are set to `None`, `complete=False`,
`dropped_for_completeness=True`, and a WARNING is logged naming the recipe,
the score, and every rejected ingredient with its reason. Totals
(`total_calories` etc.) are kept for diagnostics but are not clustering
features. Consistent with CLAUDE.md's "dropped, not guessed".

`summarize_completeness(results)` / `log_completeness_summary(results)` give
the batch picture: recipes passed vs dropped-for-completeness vs
no-usable-ingredients, and ingredient-level matched/skipped counts by stage.

### Live skip-rate numbers (3 verification recipes, 2026-09-06)

Ingredient level: **35 ingredients total, 20 matched → 42.9% skip rate.**
Skips by stage: 14 `unit_conversion` (mostly "1 chopped", "3 sprigs",
"1 clove ...", "pinch", "Juice of 1/2"), 1 `ingredient_match`.

Per recipe:

| recipe | matched / total | `count_completeness` | `calorie_completeness` | gate (`min`) | resulting P/C/F |
|---|---|---|---|---|---|
| Teriyaki Chicken Casserole | 7 / 9 | 0.778 | 0.885 | **0.778** | 11 / 88 / 5 *(biased — chicken skipped)* |
| Beef and Mustard Pie | 8 / 15 | 0.533 | 0.811 | **0.533** | 24 / 25 / 49 *(plausible)* |
| Chicken Quinoa Greek Salad | 5 / 11 | 0.455 | 0.687 | **0.455** | 23 / 31 / 48 *(plausible)* |

Drop behaviour by threshold (`min` basis):

| `min_completeness` | recipes dropped (of 3) |
|---|---|
| 0.5 | 1 — Salad |
| 0.6 – 0.7 | 2 — Pie, Salad |
| 0.8 – 0.9 | 3 — all |

**Known limitation of the metric.** Both sub-metrics rank the *actually
biased* Teriyaki recipe (0.78 / 0.89) **above** the two plausible ones,
because Teriyaki skipped few-but-critical ingredients (the chicken) while Pie
and Salad skipped many minor herbs/spices. No count/calorie threshold catches
Teriyaki without also dropping the plausible recipes. Catching the
"skipped a macro anchor" pattern specifically would need a further heuristic
(e.g. flag when a skipped ingredient name looks like a primary protein/fat
source) — not implemented; noted for a follow-up.

**For Phase 3:** at a ~43% ingredient skip rate, a strict completeness gate
(~0.8) drops the large majority of TheMealDB recipes; a lenient one (~0.5)
keeps recipes whose ingredient coverage is under half. Either way the
`usda_estimated` catalog grows slowly — feed this into the Phase 3
minimum-catalog-size threshold for the first K-Means run.

## `nutrition_source` (mandatory)

Every `menu_catalog` row carries `nutrition_source`
(`'spoonacular_computed'` | `'usda_estimated'`), `NOT NULL`, checked
constraint, indexed. Added to `infra/postgres/food_db_schema.sql`.

## Verified rate limit

USDA signed-up key: `X-Ratelimit-Limit: 3600` per hour (verified against a
real response header, not a blog figure). See
`docs/spoonacular-quota.md` sibling note and SKILL.md section 3b.

## Live verification

See `tests/` for unit coverage. A live end-to-end check against 2–3 real
TheMealDB recipes is recorded in the Phase 2b commit / PLAN.md notes —
TheMealDB measures are often non-quantitative ("1 chopped", "handful"), so a
meaningful skip rate per recipe is expected and correct; the computed
macro splits for the resolved ingredients land in a sane range.
