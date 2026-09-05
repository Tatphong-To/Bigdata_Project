# PLAN.md — Personalized Food & Nutrition Recommendation System

Working plan. Phases are done **in order**. A task is only checked off (`[x]`)
once it is finished **and** verified for real — never checked in advance.

Hard gates (from `CLAUDE.md` / `AGENTS.md`):
- Fixed pipeline order per request: **Safety filter → Layer A calculator →
  (Layer B cluster_id read) → Layer C ranking**. Never reorder / merge / skip.
- K-Means (Layer B) trains **only in Airflow**, never in the FastAPI request path.
- Safety filter is **rule-based only**, never ML, and runs on **every** request
  (excludes zero items when the user has no restrictions — it does not skip).
- Clustering features come **only** from Spoonacular computed nutrition fields.
  Diet / intolerance tags from any API are **banned** as clustering features or
  training labels (still allowed in the rule-based safety filter).
- No Parquet / `mlruns/` — Postgres only (`food_db`, `airflow_db`, `mlflow_db`).
- API responses always include `excluded_count` and `model_version`.
- All user-facing text: no medical / clinical dietary advice; carry the
  disclaimer and the stated limitations.
- API field names and nutrition formulas: verify against the `food-rec-domain`
  skill (and the primary source link) before writing code — do not guess.

**Phase 5 (Model Service) must not start until Phase 4 (Safety filter) is
complete with all unit tests passing.**

---

## Phase 0 — Data & infra prep  *(needs user confirmation before starting)*

- [x] Register for a free Spoonacular API key and store it locally (not committed)
      — user registered; key is in `.env` (git-ignored, verified untracked).
      3 real API calls succeeded with it.
- [x] Verify the **current** Spoonacular free-tier quota and record it in `docs/`
      — ✅ **verified against real response headers: 50.00 points/day**
      (`X-API-Quota-Left` 48.94 after a 1.06-point call). Old "~150" figure was
      stale — corrected in `SKILL.md`. Config-driven
      (`SPOONACULAR_DAILY_POINT_QUOTA`, default 50). Counter tracks **points**.
      Details in `docs/spoonacular-quota.md`.
- [x] Pull one real Spoonacular recipe response and confirm the actual
      nutrition JSON field names against the `food-rec-domain` skill; write the
      confirmed shape into `docs/api-schemas.md` — ✅ done. Verified:
      `results[]` (not `recipes`), `recipe.nutrition.nutrients[]` as
      `{name, amount, unit, percentOfDailyNeeds}`, exact macro names
      `"Calories"/"Protein"/"Carbohydrates"/"Fat"`, per-serving amounts,
      ingredient names present under `nutrition.ingredients[].name` **without**
      `fillIngredients`. "NOT yet verified" mark removed. `SKILL.md` updated
      with the verified shape.
      **Point-cost formula corrected to reality** (per user instruction — fix
      the formula, not the numbers): measured `cost(n) = 1.000 + 0.060*n`
      (n=1→1.06, n=10→1.60, n=30→2.80, exact fit), vs the stale doc estimate
      `1 + 0.035n`. `SKILL.md` + `docs/spoonacular-quota.md` updated.
      Also found + documented: default Python `User-Agent` is Cloudflare-banned
      (403 error 1010) — extractor must send a browser UA.
- [x] Confirm TheMealDB is reachable with the test key `1` and record its
      response fields; note it carries **no** nutrition data — done, live check
      2026-09-05, schema in `docs/api-schemas.md`.
- [x] Fill in `docker-compose.yml`: `airflow` (init + webserver + scheduler,
      LocalExecutor) and `mlflow` added alongside `postgres`; MLflow backend →
      `mlflow_db`, Airflow metadata → `airflow_db`. `docker compose config`
      passes. Postgres service brought up + schema applied + torn down clean.
      ⚠️ Full airflow/mlflow container boot not yet smoke-tested (large image
      pulls) — verify on first `docker compose up`.
- [x] Create the `food_db` schema — `menu_catalog`, `prediction_log`,
      `extraction_quota` (points counter) in
      `infra/postgres/food_db_schema.sql`. Verified for real: all 3 DBs + 3
      tables created on container init, CHECK constraints reject negative
      calories, valid rows insert.
- [x] `git init` + `.gitignore` (keys/`.env`, `__pycache__`, `data/raw/*`,
      venvs, plus `mlruns/`/`*.parquet` guard) + `requirements.txt` — done,
      repo initialised, files staged.
- [x] `README.md` with the **not medical advice** disclaimer, the catalog
      size / update-cadence note, the safety-filter limitation note, and the
      "cluster labels are exploratory, not clinical" note — in plain language
- [x] `README.md` also states: `prediction_log` stores age / weight / goal
      **with no identifier tied to a real person**, used only for system-quality
      monitoring — not for building long-term user profiles

## Phase 1 — Data extraction

- [x] Shared `food_pipeline` package under `airflow/dags/` (importable by the
      Phase 3 DAG and by tests). Spoonacular extractor paces to the verified
      budget: `cost.estimate_search_cost(n) = 1 + 0.06n`; `QuotaTracker` +
      `PostgresQuotaStore` keep the point counter in `food_db.extraction_quota`
      (atomic `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`, one row/day).
      **Verified live:** a fresh process read `points_used=2.36, request_count=2`
      back from Postgres after a prior run — survives restart, not in-memory.
      Also: 1 req/s rate-limit sleep, browser UA, key redaction, 402→
      `QuotaExhaustedError`, 403/1010→`UserAgentBannedError`.
- [x] `run_extraction` fetches recipes with per-serving nutrition + ingredient
      names (from `nutrition.ingredients[]`, no `fillIngredients`) + diet tags;
      writes raw payloads to `data/raw/<ts>_spoonacular_raw.json` and a parsed
      staging file `<ts>_staging.json` (interim hand-off, git-ignored; Phase 3
      picks the real inter-task mechanism). Stops early the moment the next
      call won't fit the budget. **Verified live** against real Spoonacular:
      2 queries completed, 3rd skipped on budget, 6 recipes staged with the
      exact `menu_catalog` columns.
- [x] `validate.validate_batch` rejects missing / non-finite / negative values,
      zero calories, non-positive servings, implausibly high calories/macros,
      and macro-derived calories far above stated (per-serving/per-recipe
      mismatch). Every rejection carries a reason and is logged.
- [x] Unit tests: **55 passing** (`.venv`, pytest 9.1.1) — `test_cost`,
      `test_quota` (incl. restart-survival + per-day rollover), `test_validate`,
      `test_parse` (real fixture, exact `"Carbohydrates"` vs `"Net
      Carbohydrates"`), `test_spoonacular_client` (quota gate, header-cost
      accounting, rate limit, error mapping, key redaction), `test_extract`
      (batching, early-stop, dedupe).

## Phase 2 — Feature engineering

- [ ] Compute, per recipe, from Spoonacular fields only (formulas from the
      `food-rec-domain` skill):
      `pct_calories_from_protein = protein_g*4 / total_calories`,
      `pct_calories_from_carbs = carbs_g*4 / total_calories`,
      `pct_calories_from_fat = fat_g*9 / total_calories`,
      `calories_per_serving = total_calories`
- [ ] Guard against divide-by-zero / missing calories; decide + document how
      such rows are handled (dropped, not imputed silently)
- [ ] Explicit assertion / test that no `diet` or `intolerances` tag is present
      in the clustering feature set
- [ ] Unit tests on the four formulas with known inputs

## Phase 3 — Full Airflow DAG

- [ ] DAG tasks in the exact `CLAUDE.md` order:
      `extract_menus` → `validate_nutrition_data` → `clean` →
      `compute_nutrition_ratios` → `train_or_update_kmeans` →
      `assign_cluster_labels` → `write_to_menu_catalog` (Postgres)
- [ ] Minimum-catalog-size threshold before the first `train_or_update_kmeans`
      run (e.g. do not train with fewer than 100 recipes in the catalog). Below
      the threshold, the DAG skips this task (and the downstream cluster steps)
      with an explicit log line stating the current count and why it skipped
- [ ] `extract_menus` respects the persisted quota; DAG scheduled as a
      recurring job (not a one-time bulk load)
- [ ] `train_or_update_kmeans` runs K-Means **only here**; grep/verify no
      clustering code exists in the FastAPI request path
- [ ] `write_to_menu_catalog` writes `cluster_id` + `model_version` per item
- [ ] DAG parse test + task-level unit tests (mock the API); document a runbook
      in `docs/`

## Phase 4 — Safety filter module  *(gate before Phase 5)*

- [ ] Standalone module (no import of any ML / clustering / ranking code),
      pure rule-based matching against ingredient text + known allergen fields
- [ ] Handles: named allergies (nut, shellfish, dairy, egg, soy, gluten/wheat,
      fish, sesame…), "no pork" / no-beef style exclusions, and diet types
      (vegan, vegetarian, pescatarian, halal, kosher where determinable)
- [ ] Returns filtered candidates **and** the excluded count/list; excludes
      zero when restrictions are empty (never skips the step)
- [ ] Unit tests covering multiple allergy/diet combos, empty restrictions,
      compound-ingredient near-misses, and case/wording vari. **All passing.**
- [ ] `docs/` note restating the limitation: matches on text/known fields, can
      miss unusual wording or hidden compound ingredients — users with serious
      allergies must still verify themselves

## Phase 5 — FastAPI model service  *(only after Phase 4 done + tests green)*

- [ ] `POST /recommend` request model: profile (age, sex, weight, height,
      activity level, goal) + restrictions (allergies, diet type)
- [ ] Pipeline in fixed order: safety filter (Phase 4) → Layer A calculator
      (Mifflin-St Jeor + activity multiplier + goal adjustment, constants from
      the `food-rec-domain` skill) → read precomputed `cluster_id` → Layer C
      deterministic ranking by distance to target
- [ ] Response always includes `daily_target`, `recommendations[]` (menu_id,
      name, match_score, nutrition), `excluded_count`, `model_version`
- [ ] Every request writes to `prediction_log`
- [ ] Service only **reads** `cluster_id` — no K-Means anywhere in the handler
- [ ] Tests: calculator values vs hand-computed, contract shape, safety filter
      actually invoked (excluded_count present even with no restrictions)
- [ ] Endpoint responses / docs carry the medical disclaimer

## Phase 6 — Frontend

- [ ] Form: user profile + dietary restrictions
- [ ] Results view: daily target, recommended menus, **count filtered out**,
      and a clearly visible medical disclaimer
- [ ] Cluster groupings, if shown, labelled as exploratory — not clinical
- [ ] Calls the FastAPI service; handles the empty-recommendations case

## Phase 7 — MLOps (supporting)

- [ ] MLflow (backend store `mlflow_db`) tracks each K-Means version: params
      (k, features, seed), metrics (inertia, silhouette), catalog size at train
      time
- [ ] Cluster-quality check as the catalog grows; periodic retrain trigger in
      Airflow with rationale documented (slow catalog growth due to quota)
- [ ] No `mlruns/` directory — confirm MLflow uses Postgres only
