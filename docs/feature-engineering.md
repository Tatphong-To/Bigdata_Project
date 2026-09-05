# Layer B feature engineering (`compute_nutrition_ratios`)

Implemented in [`airflow/dags/food_pipeline/features.py`](../airflow/dags/food_pipeline/features.py).
Runs offline in the Airflow DAG (Phase 3), between `clean` and
`train_or_update_kmeans`. Input is Phase 1 staging rows; output is the feature
set that K-Means consumes — nothing else.

## The four features

Per recipe, computed **only** from Spoonacular's own computed nutrition fields
(`calories`, `protein_g`, `carbs_g`, `fat_g`, all per serving). Formulas are
verbatim from the `food-rec-domain` skill, section 5:

| feature | formula |
|---|---|
| `pct_calories_from_protein` | `protein_g * 4 / calories` |
| `pct_calories_from_carbs`   | `carbs_g   * 4 / calories` |
| `pct_calories_from_fat`     | `fat_g     * 9 / calories` |
| `calories_per_serving`      | `calories` (already per serving — **not** divided by `servings` again) |

Constants are the standard Atwater factors: **4 kcal/g** for protein and
carbohydrate, **9 kcal/g** for fat. They are named constants in the module
(`PROTEIN_KCAL_PER_G`, `CARB_KCAL_PER_G`, `FAT_KCAL_PER_G`) — do not inline
different numbers.

Verified on real data (2026-09-05, recipe 634476 "Bbq Chicken",
cal 478.31 / P 37.1 / C 15.21 / F 29.24):
`pct_protein = 0.310259`, `pct_carbs = 0.127198`, `pct_fat = 0.550187`,
`calories_per_serving = 478.31`. The three shares sum to ~0.988 (the rest is
fibre / sugar alcohols / rounding), which cross-checks Spoonacular's own
`caloricBreakdown`.

## Divide-by-zero / missing-value policy — **drop, never impute**

`compute_feature_row(row)` returns a feature dict, or a **string reason** when
the row cannot produce a valid feature vector. `build_feature_rows()` collects
the dropped rows as `DroppedRow(menu_id, reason)` and logs one `WARNING` per
drop. No value is ever guessed or filled in.

A row is dropped when:

- `calories` is missing / `None`;
- `calories` is not a finite number (`NaN`, `inf`);
- `calories <= 0` (division by zero or a negative denominator);
- any of `protein_g` / `carbs_g` / `fat_g` is missing / `None`;
- any macro is not finite;
- any macro is negative.

(Phase 1 validation already removes most of these, but the feature step guards
independently — it must be safe to run on any staging data.)

## No externally-assigned label reaches K-Means

CLAUDE.md: the clustering is unsupervised on purpose. A `diet` / `intolerances`
tag, or a diet boolean (`vegan`, `glutenFree`, …), must never be a clustering
feature or target.

Enforcement in code:

- a feature row may contain **exactly** `menu_id` + the four feature columns —
  `assert_feature_row_clean()` raises on any extra key, and also rejects keys
  whose name looks like a source label (`diet`, `intoler`, `vegan`, …);
- `compute_feature_row()` calls that guard before returning;
- `feature_matrix()` (the numeric matrix handed to K-Means) emits only
  `FEATURE_COLUMNS`, in fixed order, with `menu_id` excluded;
- `tests/test_features.py` asserts a staging row carrying `diet_tags`,
  `ingredients`, `raw_payload` (with `diets` inside) produces a feature row
  with none of them, and that injecting a tag key makes the guard raise.

## Handoff

`build_features_from_staging_file(path)` reads a Phase 1 staging file
(`{"accepted": [...]}`) and returns `(feature_rows, dropped)`. How the Phase 3
DAG passes this between tasks (XCom vs. a table) is decided in Phase 3.
