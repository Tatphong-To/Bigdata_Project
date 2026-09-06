# Personalized Food & Nutrition Recommendation System

Given a person's profile (age, sex, weight, height, activity level, goal) and
their dietary restrictions (allergies, diet type), the system estimates daily
energy and macronutrient targets and recommends recipes from a catalog whose
nutrition profile is close to those targets.

---

## ⚠️ This is not medical or clinical dietary advice

The daily calorie and macronutrient numbers come from the **Mifflin-St Jeor
equation** with a standard activity multiplier. They are **general wellness
estimates, not a clinical prescription**. The recipe recommendations are not
reviewed by a doctor or a registered dietitian.

Do not use this system as a substitute for professional advice — especially if
you have a medical condition, take medication that interacts with diet, are
pregnant, or are under 18. Talk to a qualified health professional before
making significant changes to how you eat.

---

## Architecture — fixed order of operations

Every `/recommend` request runs these stages **in this exact order**:

1. **Safety filter** (hard rule, never ML) — removes every menu item that
   conflicts with the user's allergies / diet before anything else sees the
   candidate pool.
2. **Layer A — Calculator** (no ML) — Mifflin-St Jeor + activity multiplier +
   goal adjustment → daily targets.
3. **Layer B — Clustering** (ML, **offline only**) — K-Means over nutrition-ratio
   features, trained in the Airflow pipeline against the whole catalog. The
   Model Service only *reads* the precomputed `cluster_id`; it never trains.
4. **Layer C — Matching / ranking** (no ML) — ranks the safety-filtered
   candidates by distance to the user's target using the precomputed clusters.

Offline pipeline (Airflow DAG) task order:
`extract_menus → validate_nutrition_data → clean → compute_nutrition_ratios →
train_or_update_kmeans → assign_cluster_labels → write_to_menu_catalog`.

---

## Recipe catalog — size and freshness

- **Primary source:** Spoonacular API (free tier). Recipes come with computed
  per-serving nutrition (calories, protein, carbs, fat).
- The free tier is **quota-limited** (measured in *points per day*, not
  requests — see [`docs/spoonacular-quota.md`](docs/spoonacular-quota.md)). The
  Airflow DAG runs as a **recurring job** that pulls a small batch each run and
  stops when the daily point budget is spent, so the catalog grows gradually
  rather than in one bulk load.
- **Current catalog size:** 0 recipes — the extraction pipeline has not been
  run yet. This section will be updated with the real count and the DAG
  schedule (update cadence) once Phase 1–3 are in place.
- TheMealDB is used only for supplementary recipe variety; it carries **no**
  nutrition data and does not feed the clustering features.

---

## Safety filter — real limitation

The safety filter matches against recipe **ingredient text** and the **known
allergen / diet fields** supplied by the source API. It can miss:

- allergens described in unusual or indirect wording,
- allergens hidden inside compound or processed ingredients (e.g. a sauce that
  contains nuts without saying so),
- ingredients the source data simply lists incompletely.

**If you have a serious food allergy, always check the full ingredient list
yourself before eating anything.** Do not rely on this filter as your only
safeguard.

---

## Cluster labels are exploratory, not clinical

The groups produced by Layer B (K-Means) are **patterns discovered in the
data** — recipes with similar shares of calories from protein / carbs / fat.
They are **not** clinically validated nutrition categories. Do not read a
cluster as a doctor-approved label such as "diabetic-friendly" or
"heart-healthy" unless that specific claim has been independently verified.

---

## Logging and privacy

Each recommendation request is written to a `prediction_log` table for
**system-quality monitoring only**. It stores the request inputs (age, weight,
height, activity level, goal, diet/allergy list) and the returned targets and
menu ids **with no identifier tied to a real person** — no name, email, account,
or IP address. It is **not** a long-term user profile and is not used to track
individuals across requests. Adding a real user-identity or rating system is
out of scope and would require revisiting consent and privacy first.

---

## Data storage

Single Postgres instance, three databases: `food_db` (application data),
`airflow_db` (Airflow metadata), `mlflow_db` (MLflow tracking backend). No
Parquet files, no `mlruns/` directory — Postgres-backed throughout.

---

## Running locally

```bash
cp .env.example .env          # then paste your Spoonacular key into .env
docker compose up -d          # postgres + airflow (8080) + mlflow (5000)
```

- Airflow UI: http://localhost:8080 (admin / admin)
- MLflow UI: http://localhost:5000
- Postgres: localhost:5432 (food_user / food_pass)

`food_db` tables are created automatically on first startup from
[`infra/postgres/food_db_schema.sql`](infra/postgres/food_db_schema.sql).

See [`PLAN.md`](PLAN.md) for the phased build plan and current progress.

---

## Model Service — `POST /recommend`

FastAPI app in [`model_service/`](model_service/). Reads the catalog that the
Airflow pipeline built; it never trains anything.

```bash
# needs food_db reachable — set one of these:
export FOOD_DB_DSN=postgresql://food_user:food_pass@localhost:5433/food_db
.venv/Scripts/python -m uvicorn model_service.main:app --port 8899
# or: fastapi run model_service/main.py
```

Docs at `http://localhost:8899/docs`. Every request runs this **fixed order**
(never reordered / merged / skipped):

1. **Safety filter** (rule-based, always runs — even with no restrictions).
2. **Layer A** — Mifflin-St Jeor daily energy + macro target.
3. **Read** the precomputed `cluster_id` from `menu_catalog` (no K-Means here).
4. **Layer C** — deterministic distance ranking to `daily_target / 3`.

Request:

```jsonc
{
  "profile": {"age": 30, "sex": "male", "weight_kg": 80, "height_cm": 180,
              "activity_level": "moderate", "goal": "maintain"},
  "restrictions": {"allergies": ["peanut", "shellfish"], "diet_type": "vegetarian"},
  "max_results": 10
}
```
`sex`: `male` | `female` (the Mifflin-St Jeor equation has two forms).
`activity_level`: `sedentary` | `light` | `moderate` | `active` | `very_active`.
`goal`: `lose` | `maintain` | `gain`.

Response always includes `daily_target`, `recommendations[]` (`menu_id`,
`name`, `match_score`, `nutrition`), `excluded_count` (how many the safety
filter removed — always present), `model_version` (ends in `-provisional`
while the catalog is in the 150–499 row band), and a `disclaimer`.

Every call writes one **anonymous** row to `prediction_log` (age / weight /
goal / restrictions / targets / recommended ids — no name, email, account, or
IP). For system-quality monitoring only.

### Not medical advice

The `/recommend` response carries the disclaimer in full. The daily targets
are a general wellness estimate from the Mifflin-St Jeor equation, not
clinical advice; the safety filter matches ingredient text + known tags and
can miss allergens phrased unusually — anyone with a serious allergy must
verify ingredients themselves.
