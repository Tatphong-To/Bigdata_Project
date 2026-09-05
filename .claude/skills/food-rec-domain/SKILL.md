---
name: food-rec-domain
description: Domain knowledge for the Personalized Food & Nutrition Recommendation project — verified API schemas (Spoonacular, TheMealDB, Open Food Facts), the Mifflin-St Jeor formula and activity multipliers, clustering feature definitions, and known pitfalls. Consult this skill whenever writing data-extraction code, the nutrition-target calculator, the clustering pipeline, or the FastAPI recommendation service for this project, or whenever unsure what fields an API actually returns. Do not guess API response shapes or nutrition formulas from general knowledge — verify against this file first, and if a detail isn't here, check the primary source link before writing code against it.
---

# Food & nutrition recommendation — domain reference

## 1. Primary data source: Spoonacular API

**Use for:** menu/recipe data with computed nutrition, used to build the
clustering catalog (Layer B).

**Free tier limit:** **~50 points/day** (not requests — a *points* budget).
Verified 2026-09-05 against a real response: a call that cost 1.06 points
returned header `X-API-Quota-Left: 48.94`, i.e. a 50.00/day allowance. The
pricing page agrees (50). An earlier note in this file said "~150 requests/day"
— that was stale; corrected here. Still re-verify at
https://spoonacular.com/food-api/pricing and against the account console
before writing quota logic — providers change limits without much notice.

**Quota is spent in points, and reported in response headers** (verified
2026-09-05, names exact):
- `X-API-Quota-Request` — points this one call cost
- `X-API-Quota-Used` — cumulative points used today
- `X-API-Quota-Left` — points remaining today
Over quota → the API returns an error (402 per docs; a banned user-agent gives
a Cloudflare 403 "error 1010" instead — see pitfalls).

**Measured point cost** (verified 2026-09-05 with n=1, 10, 30 calls):
- `GET /recipes/complexSearch` with `addRecipeNutrition=true`, **no**
  `fillIngredients`: `cost(n) = 1.000 + 0.060 * n` points, where `n` = recipes
  returned. (n=1 → 1.06, n=10 → 1.60, n=30 → 2.80 — exact fit.)
- This is **higher** than the public-docs additive description
  (`1 + 0.01/recipe + 0.025/recipe for addRecipeNutrition` ≈ `1 + 0.035n`);
  trust the measured `1 + 0.06n` for budgeting. Re-measure if the API changes.
- Adding `fillIngredients=true` costs more again — not needed here (ingredient
  names already come back under `nutrition.ingredients[]`, see below).
- At 50 points/day, `1 + 0.06n` means one `number=100` call ≈ 7 points, so
  ~40 recipes' worth of headroom per day after a few such pulls.

**Key endpoints:**
- `GET /recipes/complexSearch` — search recipes, supports `diet`, `intolerances`,
  `maxCalories` etc. as query filters.
- `GET /recipes/{id}/nutritionWidget.json` or `addRecipeNutrition=true` on
  search — returns computed nutrition per serving: calories, protein (g),
  carbs (g), fat (g), plus a `diets` array of tags.
- `GET /recipes/{id}/information` — full ingredient list, instructions
  (not needed for the default pipeline — see next point).

**Auth:** requires a free API key as a query param (`apiKey=...`) or header,
obtained by registering at spoonacular.com — this is registration, not a
paid subscription.

**Verified response shape (2026-09-05, `complexSearch?addRecipeNutrition=true`):**
- Top level: `{ "results": [...], "offset", "number", "totalResults" }`
  (the array is `results`, not `recipes`).
- Each recipe: `id` (int), `title`, `servings` (int), booleans `vegan`,
  `vegetarian`, `glutenFree`, `dairyFree`, `lowFodmap`, and `diets` (array of
  strings, e.g. `["dairy free", "fodmap friendly"]`) — **safety-filter use only,
  never a clustering feature**.
- `recipe.nutrition` keys: `nutrients`, `ingredients`, `caloricBreakdown`,
  `weightPerServing`, `properties`, `flavonoids`.
- `recipe.nutrition.nutrients[]`: objects
  `{ "name", "amount", "unit", "percentOfDailyNeeds" }`. Macro rows use the
  exact names **`"Calories"`** (unit `kcal`), **`"Protein"`**, **`"Carbohydrates"`**,
  **`"Fat"`** (units `g`). Note a separate `"Net Carbohydrates"` row exists —
  match `"Carbohydrates"` exactly. Amounts are **per serving**.
- `recipe.nutrition.ingredients[]`: `{ id, name, amount, unit, nutrients[] }` —
  ingredient **names are present here without `fillIngredients`**, so the safety
  filter can read `nutrition.ingredients[].name` and no `/information` call is
  needed by default.
- `recipe.nutrition.caloricBreakdown`: `{ percentProtein, percentFat,
  percentCarbs }` — Spoonacular's own macro-calorie split. Cross-checks the
  section-5 formula (matched within ~1% on the sample); still compute our own
  per section 5, use this only as a sanity check.

## 2. Secondary/optional: TheMealDB

**Use for:** supplementary recipe variety only, NOT nutrition data.

**Free access:** no registration needed for the shared test key `1`
(`www.themealdb.com/api/json/v1/1/...`). For production-scale or commercial
use a paid Patreon-tier key is recommended by the provider, but the test
key is sufficient for this project's scope.

**Key endpoints:**
- `search.php?s={name}` — search by name.
- `filter.php?c={category}` / `filter.php?a={cuisine}` — filter.
- Returned fields: `strMeal`, `strCategory`, `strArea` (cuisine),
  `strInstructions`, up to 20 `strIngredientN`/`strMeasureN` pairs (free
  text, not structured quantities), `strMealThumb`.

**Known limitation:** ingredient amounts are free-text strings (e.g. "1 cup",
"200g") with inconsistent units — do not attempt automatic nutrition
computation from this data without treating it as a separate, explicitly
scoped extension (see CLAUDE.md).

## 3. Optional: Open Food Facts

**Use for:** packaged-product nutrition lookup by barcode only, if that
feature is explicitly requested.

**Free access:** completely open, no key, no registration.
`GET world.openfoodfacts.org/api/v2/product/{barcode}.json`

## 4. Nutrition target formula (Layer A) — Mifflin-St Jeor equation

This is a standard, widely-cited equation (Mifflin et al., 1990) — use these
exact constants, don't approximate:

```
BMR (men)   = 10 * weight_kg + 6.25 * height_cm - 5 * age_years + 5
BMR (women) = 10 * weight_kg + 6.25 * height_cm - 5 * age_years - 161
```

**Activity multiplier (TDEE = BMR * multiplier):**
| Activity level | Multiplier |
|---|---|
| Sedentary (little/no exercise) | 1.2 |
| Light (1-3 days/week) | 1.375 |
| Moderate (3-5 days/week) | 1.55 |
| Active (6-7 days/week) | 1.725 |
| Very active (hard exercise + physical job) | 1.9 |

**Goal adjustment (applied after TDEE):**
- Weight loss: TDEE - 500 (roughly 0.5 kg/week loss; never recommend a
  deficit larger than ~20-25% of TDEE without a medical-supervision caveat)
- Maintenance: TDEE unchanged
- Weight gain: TDEE + 300-500

**Macro split (only if user has no specific macro goal):** a common default
is protein 25-30%, carbs 40-50%, fat 25-30% of total calories — but this is
a general default, not a clinical prescription; state it as such.

## 5. Clustering features (Layer B)

Compute per recipe, from Spoonacular's own nutrition fields only:

```
pct_calories_from_protein = (protein_g * 4) / total_calories
pct_calories_from_carbs   = (carbs_g * 4) / total_calories
pct_calories_from_fat     = (fat_g * 9) / total_calories
calories_per_serving      = total_calories
```

(4 kcal/g for protein and carbs, 9 kcal/g for fat — standard Atwater
factors.) These four features feed K-Means. Do not include any `diet` or
`intolerances` tag from Spoonacular as a feature — see CLAUDE.md for why.

## 6. Known pitfalls checklist

- [ ] Did you verify the Spoonacular free-tier quota (~50 **points**/day as of
      2026-09-05) at build time, not just trust this document?
- [ ] Is the persisted counter tracking **points** (`1 + 0.06*n` per
      nutrition-enabled search), not a raw request count?
- [ ] Is the extractor sending a browser-style `User-Agent`? The default
      `Python-urllib/*` UA is banned by Spoonacular's Cloudflare and returns a
      403 "error 1010 / browser_signature_banned" before the request ever
      reaches the API (looks like an auth failure but isn't).
- [ ] Is the Airflow extract task actually pacing requests to stay under
      quota (and the 1 req/s rate limit), with a persisted counter that
      survives DAG restarts (not just an in-memory counter that resets)?
- [ ] Are nutrition ratios computed only from Spoonacular fields, never
      from TheMealDB free-text ingredients, unless that's an explicitly
      scoped extension?
- [ ] Does the safety filter run and get logged (`excluded_count`) on every
      single request, even when the user has no allergies listed (should
      just exclude zero items, not skip the step)?
- [ ] Is K-Means retrained/updated in Airflow only, never inside the FastAPI
      request handler?
- [ ] Does any user-facing text avoid phrasing recommendations as medical
      or clinical advice?