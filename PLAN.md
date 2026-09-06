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

- [x] `food_pipeline/features.py` — `compute_feature_row` computes the four
      features from Spoonacular fields only, Atwater factors as named
      constants (4 / 4 / 9). `calories_per_serving = calories` (already per
      serving — not re-divided by `servings`). **Verified** on the 6 real
      Phase 1 staging recipes and hand-checked against recipe 634476
      (P 0.310259 / C 0.127198 / F 0.550187 / cps 478.31).
- [x] Divide-by-zero / missing-value policy = **drop, never impute**:
      `compute_feature_row` returns a string reason for calories
      missing/non-finite/≤0 or any macro missing/non-finite/negative;
      `build_feature_rows` logs one WARNING per drop and returns
      `DroppedRow(menu_id, reason)`. Documented in
      `docs/feature-engineering.md`.
- [x] No source label reaches K-Means: `assert_feature_row_clean` whitelists
      keys to `menu_id` + the 4 features and rejects label-looking keys
      (`diet`, `intoler`, `vegan`, …); `feature_matrix` emits only
      `FEATURE_COLUMNS` (no `menu_id`). Test feeds a staging row carrying
      `diet_tags` / `ingredients` / `raw_payload` and asserts none leak;
      another asserts an injected tag key raises.
- [x] Unit tests: **22** in `test_features.py` (77 total), incl. all four
      formulas hand-computed with round-number inputs, per-macro Atwater
      factor check, real-recipe cross-check, every drop case, and the
      no-tags guarantees. Full suite green.

## Phase 2b — TheMealDB + USDA supplementary nutrition source

*(added after the original plan; do before Phase 3)*

- [x] USDA FoodData Central key registered; in `.env` as `USDA_FDC_API_KEY`
      (`.env.example` placeholder added). Rate limit **verified with the real
      key**: `X-Ratelimit-Limit: 3600`/hour (not the ~1000 blog figure).
      `food-rec-domain` SKILL.md section 3b written with verified schema.
      Committed + pushed as `28359be`.
- [x] `themealdb.py` — client (test key `1`, light retry for the flaky host)
      + `parse_meal`: `strIngredientN`/`strMeasureN` → `(name, quantity_text)`
      pairs, handles `""`/`null` slots, tags parsing.
- [x] `usda_client.py` — `/foods/search`; key from
      `os.environ["USDA_FDC_API_KEY"]`, **raises if unset — no DEMO_KEY
      fallback**. Macros per 100 g by `nutrientNumber` 208/203/204/205 +
      `unitName`; prefers `Foundation`/`SR Legacy` over `Branded`. Retries
      transient 400/5xx/timeout (api.data.gov edge is flaky), surfaces 429.
- [x] `ingredient_matcher.py` — `difflib` + token-Jaccard fuzzy score in
      [0,1]. Threshold is **`MatchConfig.min_confidence` (config, not
      hard-coded)**. Below threshold → dropped + WARNING logged; accepted but
      borderline → INFO logged. `require_all_macros` filters candidates.
- [x] `unit_converter.py` — explicit supported units (mass exact; volume via
      configurable `g_per_ml` default 1.0 + optional per-ingredient density).
      Non-quantitative / count-based / unknown-unit measures → **rejected with
      a reason, never estimated**. Fractions + unicode fractions handled.
- [x] `compute_recipe_nutrition.py` — orchestrates convert → search → match →
      sum. `pct_calories_from_*` via `features.compute_feature_row` (identical
      formula). Tags `nutrition_source='usda_estimated'`. Per-stage skips
      recorded + logged. No servings count invented (`calories_per_serving`
      None → `complete=False`).
- [x] Unit tests for every new module — `test_unit_converter` (45),
      `test_themealdb`, `test_usda_client` (incl. retry/timeout/429),
      `test_ingredient_matcher` (clear / ambiguous+logged / no-match+logged /
      threshold-is-config), `test_compute_recipe_nutrition` (hand-computed
      totals, per-stage skips, no-servings, no-ingredients). **166 total, green.**
- [x] Verified with 3 real TheMealDB recipes (live TheMealDB + live USDA):
      *Beef and Mustard Pie* ≈ 956 kcal/serving, P24/C24/F50% — plausible for
      a beef + puff-pastry pie; *Chicken Quinoa Greek Salad* ≈ 548
      kcal/serving, P23/C31/F48% — plausible. *Teriyaki Chicken Casserole*
      resolved only the sauce + rice (chicken `[2]` and veg `[1 (12 oz.)]`
      correctly rejected as non-convertible) so its number reflects that
      subset. pct sums 97–104% (rounding/fibre). Confidence scoring behaved
      (exact→1.00, "red wine"→"Wine, red" 0.95, "feta"→"Cheese, feta" 0.57
      logged, "plain flour"→pretzel 0.25 rejected+logged). Skip rate is real
      and by design (non-quantitative measures dropped, not guessed) plus some
      transient api.data.gov 400s that outlasted retries that run.
- [x] `nutrition_source` column added to `menu_catalog`
      (`spoonacular_computed` | `usda_estimated`, NOT NULL, CHECK, indexed).
- [x] `CLAUDE.md` + `AGENTS.md` updated: TheMealDB+USDA approved as a
      nutrition source; mandatory `nutrition_source` column + always-log
      low-confidence matches; safety filter / banned-tag rules unchanged.
- [x] Recipe-level **completeness guard** in `compute_recipe_nutrition.py`:
      `count_completeness` (matched/total ingredients) + `calorie_completeness`
      (matched kcal / (matched + guard-estimated missing kcal), non-quantitative
      skips weighted 0). `CompletenessConfig` — `min_completeness` (config, not
      hard-coded; **default PROVISIONAL 0.70, awaiting user confirm**), `basis`
      ("count"/"calorie"/"min", default "min"). Below threshold → row dropped:
      `pct_calories_from_*` + `calories_per_serving` set to `None`,
      `complete=False`, `dropped_for_completeness=True`, WARNING logged with
      recipe id/name/score/rejected ingredients. `summarize_completeness` /
      `log_completeness_summary` for the batch drop-rate + per-stage skip stats.
- [x] 6 new tests (172 total, green): high-completeness passes;
      Teriyaki-like (main ingredient rejected) → dropped, features withheld;
      threshold is config (lenient keeps / strict drops); basis count vs
      calorie vs min; non-quantitative skips don't inflate missing kcal;
      batch summary counts.
- [x] Live numbers (3 recipes): 35 ingredients, 20 matched → **42.9% skip
      rate** (14 unit_conversion, 1 match). count/calorie completeness:
      Teriyaki 0.78/0.89, Pie 0.53/0.81, Salad 0.46/0.69. **Finding:** both
      metrics rank the biased Teriyaki *highest* (it skipped few-but-critical
      items) — no threshold catches it without dropping the plausible recipes.
      Recorded in `docs/phase2b-usda-pipeline.md`.
- [x] User decision: keep `min_completeness=0.70` **PROVISIONAL** (marker
      stays in code), basis `"min"`; the real value is chosen in Phase 3
      alongside the minimum-catalog-size threshold. No macro-anchor heuristic
      now — the "few-but-critical skip" limitation stays documented as a
      follow-up.
- [x] Commit Phase 2b modules separately + push. *(done: `04d7acf`; completeness
      guard follow-up commit after this)*

## Phase 3 — Full Airflow DAG

- [x] DAG `food_rec_offline_pipeline` (`airflow/dags/food_rec_pipeline_dag.py`),
      logic in `food_pipeline/dag_tasks.py`. Tasks in the exact CLAUDE.md order:
      `extract_menus` → `validate_nutrition_data` → `clean` →
      `compute_nutrition_ratios` → `train_or_update_kmeans` →
      `assign_cluster_labels` → `write_to_menu_catalog`. **Verified in a real
      `apache/airflow:2.10.3` container**: parses with `import_errors == {}`,
      all 7 task ids, linear edges intact, `schedule=@daily`,
      `write` `trigger_rule=none_failed`.
- [x] Minimum-catalog-size gate in `train_or_update_kmeans` (constants in
      `clustering.py`): **< 150** → skip train + assign, WARNING logged
      (`"catalog has N rows, minimum 150 required…"`), `05_skip.json` written,
      `write` still persists rows without a cluster; **150–499** → train,
      `model_version` gets `-provisional`, `menu_catalog.model_provisional=true`;
      **≥ 500** → stable. Counts `spoonacular_computed` + `usda_estimated`
      together, deduped by `menu_id` (no source-mix control — documented
      limitation).
- [x] `extract_menus` calls Phase 1 `run_extraction` (Spoonacular, persisted
      point budget) + Phase 2b `compute_recipe_nutrition` (TheMealDB→USDA,
      completeness guard; `dropped_for_completeness` rows never pass). DAG
      `schedule=@daily`, `catchup=False`, `max_active_runs=1` — recurring job.
- [x] K-Means runs **only** in `train_or_update_kmeans`. `clustering.py` is the
      sole `sklearn` importer (lazy, inside functions) and is imported **only**
      by `dag_tasks.py`. Grep over `airflow/`, `model_service/`, `frontend/`
      confirms no clustering code near any request path.
- [x] `write_to_menu_catalog` upserts `cluster_id`, `model_version`,
      `model_provisional` (+ nutrition, ratios, `nutrition_source`) per row.
- [x] MLflow params/metrics via `mlflow_tracking` with a JSON fallback when
      `MLFLOW_TRACKING_URI` is unset (`TODO(Phase 7)` for full `mlflow_db`).
- [x] Tests: **+24** (196 pass, 1 skipped) — `test_clustering` (gate
      boundaries + real fit/predict), `test_dag_tasks` (every step with fakes,
      all 3 gate outcomes, full extract→…→write skip-path chain),
      `test_dag_structure` (`TASK_SEQUENCE` == CLAUDE.md order; real DagBag
      parse skipped locally / run in container). 172 prior tests still green.
- [x] `docs/phase3-airflow-dag.md` — DAG, gate (150/500 + rationale), MLflow,
      and 4 named deadline-scope limitations (source-mix, partial re-assign,
      assumed servings, XCom-paths).
- [x] **First real run** (live Spoonacular + TheMealDB + USDA + local
      Postgres): `extract_menus` → **231 rows** (Spoonacular 12 queries ×20
      deduped; 26.40 points spent of 50, persisted counter `2026-09-06`;
      TheMealDB→USDA 0 kept / 7 dropped for completeness). Gate fired =
      **provisional** (231 in 150–499). K-Means k=6 trained
      (`kmeans-…-provisional`, inertia 271.7, silhouette 0.29), 231 rows
      labelled + written, `model_provisional=true`. `menu_catalog` now 231
      rows, all `spoonacular_computed`, all clustered.
- [x] Decision: **Spoonacular is the primary source.** 231 rows already clears
      the 150 gate + the 120-recipe requirement with zero `usda_estimated`
      noise. The daily DAG run now pulls Spoonacular only
      (`extract_menus(..., include_themealdb_usda=False)` in the DAG wrapper)
      and lets `@daily` accumulate toward "stable" (500+). The Phase 2b
      TheMealDB→USDA path is kept (still default-on in `dag_tasks.extract_menus`
      for opt-in use), just no longer a growth target. Recorded in
      `docs/phase3-airflow-dag.md`.

## Phase 4 — Safety filter module  *(gate before Phase 5)*

- [x] `food_pipeline/safety_filter.py` — standalone, imports only
      `logging`/`re`/`dataclasses`/`typing`/`__future__` (nothing from
      `food_pipeline`, no ML/clustering/ranking). An AST test
      (`test_module_has_no_ml_or_pipeline_imports`) enforces it. Pure keyword
      matching over ingredient text + API `diet_tags`.
- [x] Handles named allergies **nut, shellfish, dairy, egg, soy, gluten/wheat,
      fish, sesame** (curated keyword + suppressor lists); avoidances
      **pork, beef, poultry, alcohol, gelatin, honey** incl. "no pork"/"no beef"
      wording; diets **vegan, vegetarian, pescatarian, halal, kosher**. API
      tags only *clear* a diet, never prove a violation; halal/kosher always
      log a partial-determination note; unknown allergen → literal match +
      log; unknown diet → "cannot determine" + log (no guess).
- [x] `apply_safety_filter` returns `SafetyResult(kept, excluded[Exclusion
      (menu_id,name,rule,reason)], undetermined, excluded_count)`. Empty
      restrictions → `excluded_count == 0`, all kept, and it still logs
      `"filter still ran"` (never skipped). Concrete ingredient match excludes
      even when a "free-from" tag says otherwise; no ingredient data →
      `unverifiable:<key>` exclusion for allergy/avoid, `undetermined` (kept) +
      log for diet.
- [x] **34 unit tests, all passing** — each named allergen; no-pork/no-beef;
      every diet; `vegan + nut allergy` combined; tag-clears-diet-not-allergen;
      case/wording variants (`peanut`/`Peanuts`/`PEANUT`/`groundnut`/
      `Ground Nuts`/`tree nut`); suppressors (coconut milk, nutmeg, almond
      flour, buckwheat, eggplant); compound-ingredient catches **and** the
      documented misses; unverifiable path; empty restrictions.
- [x] `docs/phase4-safety-filter.md` — decision order, keyword tables, and the
      CLAUDE.md limitation stated plainly (text/known-field matching only, can
      miss unusual wording / compound ingredients; serious-allergy users must
      verify ingredients themselves; not a sole safeguard), with the concrete
      caught-vs-missed examples from the tests.

## Phase 5 — FastAPI model service  *(only after Phase 4 done + tests green)*

- [x] `model_service/` FastAPI app. `POST /recommend` request model:
      `profile` (age, sex, weight_kg, height_cm, activity_level, goal) +
      `restrictions` (allergies list, diet_type) + `max_results`. Pydantic
      `Literal` validation (422 on bad input). `/health` too.
- [x] Fixed pipeline order in `model_service/pipeline.py`: safety filter
      (`food_pipeline.safety_filter`, always runs) → Layer A calculator
      (`model_service/calculator.py` — Mifflin-St Jeor + activity multiplier +
      goal adjustment, constants **verbatim from the food-rec-domain skill**;
      deficit capped at 20% of TDEE, gain +400, macro split 30/40/30 @ 4/4/9)
      → read precomputed `cluster_id` + per-cluster mean macros (`AVG` query,
      no ML) → Layer C deterministic ranking (`model_service/ranking.py` —
      normalised distance to `daily_target / 3`, item term + cluster term,
      ties broken by `menu_id`).
- [x] Response: `daily_target {calories,protein_g,carbs_g,fat_g}`,
      `recommendations[] {menu_id,name,match_score,nutrition{...}}`,
      `excluded_count` (from the real safety filter), `model_version` (carries
      `-provisional` from `menu_catalog.model_provisional`), plus a
      `disclaimer` string on every response.
- [x] Every request writes one anonymous row to `prediction_log`
      (age/sex/weight/height/activity/goal/allergies/diet + targets +
      recommended ids + excluded_count + model_version; no name/email/IP).
      Verified: 8 rows written during live testing.
- [x] Service **only reads** `cluster_id`. `model_service/` imports nothing
      from `food_pipeline.clustering` / `dag_tasks` / `features` and no
      `sklearn`/`numpy`/`pandas` — an AST test
      (`test_model_service_no_ml.py`) enforces it; only `safety_filter` and
      `db` are pulled from `food_pipeline`.
- [x] `cluster_id IS NULL` rows are **not dropped, not errored**: the ranker
      scores them by item distance + a fixed penalty (`used_cluster_fallback`,
      counted + logged). Tested.
- [x] Recipe **name** is fed to the safety filter alongside the ingredient
      list, so "Cheesy Beef Burrito" is caught for `vegetarian` even though
      its ingredient is written "stew meat" (Phase 4 filter logic untouched —
      it just gets more text). Verified live: vegetarian `excluded_count`
      196 → 202, recommendations meat-free.
- [x] Tests: **35 new** (265 total, 1 skipped) — `test_calculator.py` (14,
      hand-computed BMR/TDEE/goal/macros for M/F × activity × goal, deficit
      cap), `test_ranking.py` (9, determinism, cluster influence, null-cluster
      fallback), `test_recommend_api.py` (12, contract shape, safety filter
      runs with empty restrictions → `excluded_count == 0`, runs with
      restrictions → `excluded_count > 0`, prediction_log write, no-cluster
      edge case, `model_version` provisional), `test_model_service_no_ml.py`.
- [x] Medical disclaimer: on every response (`disclaimer` field), in the
      OpenAPI app + endpoint descriptions, and in the README endpoint section.
- [x] **Live-verified** against the real 335-row catalog (231 clustered):
      *male 30/moderate/maintain, no restrictions* → 2759 kcal, excluded 0;
      *female 28/active/lose, peanut+shellfish* → 1889 kcal (deficit capped),
      excluded 86, no shrimp/peanut in recs; *male 45/sedentary/gain,
      vegetarian* → 2312 kcal, excluded 202, all-meatless recs.

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
