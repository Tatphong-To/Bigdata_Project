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
