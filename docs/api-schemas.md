# API response schemas — verification notes

Verify field names against a **real response** before hardcoding a parser
(per the `food-rec-domain` skill). This file records what has been confirmed.

---

## 1. Spoonacular — ✅ verified 2026-09-05 against a real response

Confirmed with a live `complexSearch?query=chicken&number=1&addRecipeNutrition=true`
call (API key from `.env`). Raw sample saved at
`data/raw/spoonacular_sample_complexsearch.json` (git-ignored). Recipe used:
id `634476` "Bbq Chicken", `servings: 4`.

### `GET https://api.spoonacular.com/recipes/complexSearch?query=...&number=N&addRecipeNutrition=true&apiKey=...`

> No `fillIngredients` — not needed (ingredient names come back anyway, below).
> Send a browser-style `User-Agent` or Cloudflare returns 403 error 1010.

**Verified top-level shape:**

```jsonc
{
  "results": [
    {
      "id": 634476,                         // int
      "title": "Bbq Chicken",
      "servings": 4,                         // int
      "readyInMinutes": 45,
      "vegan": false, "vegetarian": false,   // booleans present at recipe level
      "glutenFree": false, "dairyFree": true, "lowFodmap": true,
      "diets": ["dairy free", "fodmap friendly"],   // array<string> — SAFETY FILTER ONLY, never a clustering feature
      "nutrition": {
        "nutrients": [
          { "name": "Calories",      "amount": 478.31, "unit": "kcal", "percentOfDailyNeeds": 23.92 },
          { "name": "Fat",           "amount": 29.24,  "unit": "g",    "percentOfDailyNeeds": 44.98 },
          { "name": "Carbohydrates", "amount": 15.21,  "unit": "g",    "percentOfDailyNeeds": 5.07  },
          { "name": "Net Carbohydrates", "amount": 15.03, "unit": "g", "percentOfDailyNeeds": 5.47  },
          { "name": "Protein",       "amount": 37.1,   "unit": "g",    "percentOfDailyNeeds": 74.2  }
          // 32 nutrient rows total (vitamins, minerals, sugar, cholesterol, ...)
        ],
        "ingredients": [
          { "id": 19334, "name": "brown sugar", "amount": 0.75, "unit": "tablespoons", "nutrients": [ /* per-ingredient */ ] }
          // names: brown sugar, catsup, chicken pieces, mustard, soy sauce, worcestershire sauce
        ],
        "caloricBreakdown":  { "percentProtein": 31.41, "percentFat": 55.7, "percentCarbs": 12.89 },
        "weightPerServing":  { "amount": 235, "unit": "g" },
        "properties": [ /* Glycemic Index, Glycemic Load, ... */ ],
        "flavonoids": [ /* ... */ ]
      }
    }
  ],
  "offset": 0,
  "number": 1,
  "totalResults": 100
}
```

**Confirmed parser rules:**
- Array is **`results`** (not `recipes`). Also `offset`, `number`, `totalResults`.
- Nutrition values live in **`recipe.nutrition.nutrients[]`** as
  `{name, amount, unit, percentOfDailyNeeds}` objects — NOT top-level keys.
  Match exact names: **`"Calories"`** (`kcal`), **`"Protein"`**,
  **`"Carbohydrates"`**, **`"Fat"`** (`g`). ⚠️ A distinct **`"Net
  Carbohydrates"`** row exists — match `"Carbohydrates"` exactly, not a
  prefix/substring.
- `amount` values are **per serving** (478 kcal/serving for a 4-serving recipe;
  matches `caloricBreakdown` and is a plausible single portion).
- **Ingredient names are available without `fillIngredients`**:
  `recipe.nutrition.ingredients[].name`. Use these for the safety filter — no
  `GET /recipes/{id}/information` call needed by default.
- Diet/allergen signals: `recipe.diets[]` plus booleans `vegan`, `vegetarian`,
  `glutenFree`, `dairyFree`, `lowFodmap` at recipe level.
- `recipe.nutrition.caloricBreakdown` (`percentProtein/Fat/Carbs`) is
  Spoonacular's own macro-calorie split — matched the section-5 skill formula
  within ~1% on the sample. Compute our own per the skill; use this as a check.

### Quota / point cost
Verified — see `docs/spoonacular-quota.md`. Headers: `X-API-Quota-Request`,
`X-API-Quota-Used`, `X-API-Quota-Left`. Measured cost `1.000 + 0.060 * n`.

---

## 2. TheMealDB — ✅ verified 2026-09-05

`GET https://www.themealdb.com/api/json/v1/1/search.php?s=Arrabiata`
(test key `1`, no registration) — reachable, returns HTTP 200 JSON.

**Top-level:** `{ "meals": [ ... ] }` (or `{ "meals": null }` when nothing matches).

**Meal object fields:**
- Identifiers/meta: `idMeal`, `strMeal`, `strMealAlternate`, `dateModified`
- Classification: `strCategory`, `strArea`, `strTags`, `strCountry`
- Content: `strInstructions`, `strMealThumb`, `strYoutube`, `strSource`,
  `strImageSource`, `strCreativeCommonsConfirmed`
- Ingredients: `strIngredient1` … `strIngredient20`
- Measures: `strMeasure1` … `strMeasure20` (free-text, e.g. "1 cup", "200g")

**Nutrition fields:** ❌ NONE. No calories / protein / carbs / fat anywhere.

**Consequence:** TheMealDB is usable only for supplementary recipe/ingredient
variety. It CANNOT feed the Layer B clustering features (which require
Spoonacular's computed per-serving nutrition). Do not parse nutrition from
`strMeasureN` text — out of scope per `CLAUDE.md`.

---

## 3. Open Food Facts — not used by default

`GET https://world.openfoodfacts.org/api/v2/product/{barcode}.json` — open, no
key. Only relevant if a packaged-product lookup feature is explicitly added.
Not part of the recipe pipeline.
