# Project rules: Personalized Food & Nutrition Recommendation System

These rules are non-negotiable architectural decisions. Do not silently
deviate from them. If a task seems to require breaking one of these rules,
stop and ask before proceeding.

## Core architecture — order of operations is fixed

Every recommendation request MUST go through these stages in this exact
order. Do not reorder, merge, or skip stages.

1. **Safety filter (hard rule, never ML).** Given the user's allergies and
   dietary restrictions (e.g. vegan, no pork, nut allergy), remove every
   disqualifying menu item from the candidate pool with deterministic
   rule-matching against ingredient lists. This MUST run before anything
   else touches the candidate pool. A clustering or ranking model is never
   allowed to be the sole safeguard against an allergen — it has no
   accuracy guarantee and food allergies can be medically serious.
2. **Layer A (Calculator, no ML).** Compute the user's daily energy and
   macronutrient targets from the Mifflin-St Jeor equation plus an activity
   multiplier (formulas and constants are in the `food-rec-domain` skill —
   don't re-derive or guess them). Pure business logic, deterministic.
3. **Layer B (ML, offline/batch only — runs in Airflow, not per-request).**
   K-Means clustering over nutrition-ratio features (percent of calories
   from protein/carb/fat per serving) computed from Spoonacular's nutrition
   data. Produces a `cluster_id` per menu item, stored in the database.
   This is unsupervised — do not use any diet/allergy tag from the source
   API as a training label or target; the point of clustering here is to
   discover structure without relying on externally-assigned categories.
4. **Layer C (Matching/ranking, no ML).** Given the safety-filtered
   candidates (from stage 1) and the user's target (from stage 2), rank by
   distance to target using the precomputed cluster assignments (from stage
   3). Deterministic ranking logic, not a learned model.

## Why clustering runs offline, not per-request

K-Means is trained in the Airflow pipeline against the full menu catalog,
not re-run for every API call. The Model Service only ever *reads* an
already-assigned `cluster_id` at inference time. If you find yourself
writing clustering code inside the FastAPI request handler, stop — that
belongs in the Airflow DAG instead.

## Data sources — roles are fixed

- **Primary: Spoonacular API.** Free tier, but rate-limited to ~150
  requests/day — this is a real constraint, not a suggestion, and it's the
  reason the Airflow DAG needs to run as a recurring job rather than a
  one-time bulk load. Use it for recipes with computed nutrition data
  (calories, protein, carbs, fat per serving) and diet/allergen metadata.
- **Secondary/optional: TheMealDB.** Free, no key needed. Has recipes and
  ingredient lists but NOT computed nutrition values — do not attempt to
  compute nutrition ratios from TheMealDB's raw ingredient text unless
  explicitly asked to build that as an extension; ingredient-quantity
  parsing and unit conversion is error-prone and out of scope by default.
- **Optional: Open Food Facts.** Free, no key. Packaged-food nutrition by
  barcode — useful only if a future feature needs packaged-product lookup,
  not part of the default recipe pipeline.

## Feature engineering (Layer B clustering)

Allowed features: percent of calories from protein, percent from carbs,
percent from fat, calories per serving, and other ratios derived purely
from Spoonacular's own computed nutrition fields.

Banned: any diet/allergy tag supplied by the source API (vegan, keto,
gluten-free, etc.) as a clustering feature or target — using it would
defeat the purpose of doing unsupervised discovery instead of relying on
someone else's labels. Tags may still be used downstream in the safety
filter (stage 1), which is a different, rule-based use.

## API contract (FastAPI)

`POST /recommend` request must include user profile (age, sex, weight,
height, activity level, goal) and restrictions (allergies, diet type).
Response must always include, at minimum:

```json
{
  "daily_target": {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
  "recommendations": [
    {"menu_id": "string", "name": "string", "match_score": 0.0,
     "nutrition": {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0}}
  ],
  "excluded_count": 0,
  "model_version": "string"
}
```

`excluded_count` (how many candidates the safety filter removed) must
always be present — it's a transparency signal that the filter actually
ran, not a cosmetic field to drop if convenient.

## Airflow DAG task order

extract_menus (respect ~150 req/day Spoonacular quota — do not batch-request
beyond it) → validate_nutrition_data (reject rows with missing or
physically-impossible values, e.g. negative calories) → clean →
compute_nutrition_ratios → train_or_update_kmeans → assign_cluster_labels →
write_to_menu_catalog (Postgres)

## Data storage

Postgres, 3 databases in one instance: `food_db` (real data), `airflow_db`
(Airflow metadata), `mlflow_db` (MLflow tracking backend, if used for
K-Means versioning). Do not reintroduce Parquet files or `mlruns/` — this
project follows the same Postgres-only pattern as before, not the older
file-based approach.

## Documentation obligations — write these in plain language, not a footnote

- **This system does not give medical or clinical dietary advice.** The
  Mifflin-St Jeor calculation and the recommendations are general wellness
  estimates, not a substitute for a doctor or registered dietitian —
  especially for anyone with a medical condition, on medication that
  interacts with diet, pregnant, or under 18.
- The recipe catalog's size and freshness is bounded by Spoonacular's free
  daily quota — state the current catalog size and update cadence.
- The safety filter matches against ingredient text and known allergen
  fields; it can miss allergens described in unusual wording or hidden in
  compound ingredients. State this as a real limitation, not hedging —
  users with serious allergies should still verify ingredients themselves.
- Cluster labels (Layer B) are exploratory groupings discovered by K-Means,
  not clinically validated nutrition categories — don't describe them to
  end users as if they were, e.g., a doctor-approved "diabetic-friendly"
  label unless that claim is independently verified.

## When unsure — ask before

Merging the safety filter with the ranking step, using an external diet tag
as a training label, running clustering inside the request path instead of
Airflow, presenting recommendations as medical advice, or adding a
real user-interaction/rating system (raises data privacy and consent
questions beyond this project's current scope).