# Phase 3 — offline pipeline DAG

`airflow/dags/food_rec_pipeline_dag.py` (DAG id `food_rec_offline_pipeline`).
All step logic lives in `airflow/dags/food_pipeline/dag_tasks.py` so the DAG
file is stdlib-only to import (sklearn / psycopg / mlflow load lazily inside
the tasks) and parses in a bare `apache/airflow:2.10.3` image.

## Task order (fixed — CLAUDE.md, do not reorder / merge / skip)

```
extract_menus
  -> validate_nutrition_data
  -> clean
  -> compute_nutrition_ratios
  -> train_or_update_kmeans      (minimum-catalog-size gate)
  -> assign_cluster_labels
  -> write_to_menu_catalog
```

Each step writes a numbered JSON artifact under
`data/pipeline_runs/<run_id>/` and returns its path; the next step reads it.
`write_to_menu_catalog` has `trigger_rule=none_failed` so it still runs when
the gate skipped train + assign.

| step | reuses | output |
|---|---|---|
| `extract_menus` | Phase 1 `run_extraction` (Spoonacular, persisted point budget) + Phase 2b `compute_recipe_nutrition` (TheMealDB→USDA, completeness guard) | `01_extracted.json` |
| `validate_nutrition_data` | Phase 1 `validate.validate_batch` | `02_validated.json`, `02_rejected.json` |
| `clean` | — (dedupe by `menu_id`, drop blank name/missing macro, lowercase+sort ingredient/tag lists, coerce numerics) | `03_cleaned.json` |
| `compute_nutrition_ratios` | Phase 2 `features.build_feature_rows` (formula unchanged) | `04_ratios.json` |
| `train_or_update_kmeans` | `clustering.train_kmeans` (sklearn `Pipeline(StandardScaler, KMeans)`) + `mlflow_tracking` | `05_model.pkl` + `05_model.json`, or `05_skip.json` |
| `assign_cluster_labels` | the fitted model | `06_assignments.json` |
| `write_to_menu_catalog` | `catalog_repo.upsert_menu_rows` | `07_write_summary.json` |

## Schedule

`@daily`, `catchup=False`, `max_active_runs=1`. This is a **recurring job**,
not a bulk load: `extract_menus` respects the persisted Spoonacular point
budget (`food_db.extraction_quota`), so each run pulls a small batch and the
catalog grows over time. Interval can be tuned later.

## Source priority — Spoonacular-primary

The first real run produced **231 rows from Spoonacular alone** (12 queries,
26.4 of 50 points) and **0 from TheMealDB→USDA** — all 7 TheMealDB recipes
tried were dropped by the Phase 2b completeness guard (a primary ingredient
un-quantifiable in each). 231 already clears both the 150-row gate and the
course's 120-recipe requirement.

Decision: **Spoonacular is the primary source going forward.** The daily
`@daily` DAG run pulls Spoonacular only (`extract_menus(..., include_themealdb_usda=False)`
in the DAG wrapper) and lets the catalog accumulate toward the "stable" gate
(500+) over successive runs. This keeps the catalog free of `usda_estimated`
noise. The TheMealDB→USDA path (Phase 2b) is **not removed** — the code still
works and `dag_tasks.extract_menus` still includes it by default for manual /
opt-in use; it is just no longer a growth target, and the TheMealDB
completeness issue does not need fixing now.

## Minimum-catalog-size gate (`train_or_update_kmeans`)

Counts total distinct rows = existing `menu_catalog` rows **+** this run's
feature rows, deduped by `menu_id` (`spoonacular_computed` and
`usda_estimated` counted together — no source-mix control yet, see below).

| catalog rows | behaviour |
|---|---|
| **< 150** | `train_or_update_kmeans` **and** `assign_cluster_labels` skip. A WARNING is logged (`"catalog has N rows, minimum 150 required to train K-Means — skipping…"`) and `05_skip.json` is written. `write_to_menu_catalog` still runs and persists the new rows **without** `cluster_id` so the catalog can grow toward 150. |
| **150 – 499** | Train. `model_version` gets a `-provisional` suffix, `menu_catalog.model_provisional = true`, MLflow tag `provisional=true`. Downstream (Phase 5) can surface "clusters not yet stable". |
| **>= 500** | Train normally, not provisional. |

Rationale for the numbers: K-Means on a nutrition-ratio space with `k≈6`
needs enough points per cluster for the centroids to be meaningful; **150**
(~25/cluster) is the floor for a non-degenerate first model, **500**
(~80/cluster) is where re-runs start to be stable run-to-run. Both are
constants in `clustering.py` (`MIN_CATALOG_SIZE`, `STABLE_CATALOG_SIZE`) and
can be revised — they were set for the deadline, cluster-quality tuning is
Phase 7.

## K-Means is confined to the DAG

`clustering.py` is the only module that imports `sklearn`, and only inside
`train_kmeans` / `TrainedKMeans.predict`. It is imported **only** by
`dag_tasks.py`. Verified by grep (`grep -rn 'sklearn\|from .clustering'` over
`airflow/`, `model_service/`, `frontend/`): no clustering code anywhere near a
request path. The Phase 5 Model Service will only *read* `cluster_id`.

## MLflow

`mlflow_tracking.log_kmeans_run` logs params (`k`, `seed`, `features`,
`n_init`, `max_iter`, `n_samples`, `catalog_row_count`, `gate`) and metrics
(`inertia`, `silhouette` when computable) to MLflow when `MLFLOW_TRACKING_URI`
is set (backend store = `mlflow_db`). Otherwise it writes
`data/pipeline_runs/<run_id>/models/<model_version>.mlflow-fallback.json` and
logs a line, so the DAG runs without a live MLflow server.
**TODO(Phase 7):** wire the full MLflow setup (registry, run comparison as the
catalog grows, retrain triggers).

## Known limitations (deliberate, deadline scope — not oversights)

1. **No source-mix control in the gate.** The 150-row threshold counts
   `spoonacular_computed` and `usda_estimated` rows together. If
   `usda_estimated` rows — which carry known noise, e.g. the Teriyaki case in
   `docs/phase2b-usda-pipeline.md` where a primary ingredient is dropped and
   the macro base is biased — make up most of the first 150 rows, cluster
   quality will be lower than it looks. **Deferred follow-up:** cap the
   `usda_estimated` share, or gate on `spoonacular_computed` count alone, or
   down-weight `usda_estimated` rows in training. Tracked, not unnoticed.
2. **`assign_cluster_labels` relabels only the current run's rows.** Rows
   already in `menu_catalog` keep their previous `cluster_id` until they are
   re-extracted. A full re-assign pass over the whole catalog after each
   retrain is a follow-up.
3. **Assumed servings for `usda_estimated` rows.** TheMealDB has no servings
   count; `extract_menus` passes `DEFAULT_ASSUMED_SERVINGS = 4` so these rows
   have a `calories_per_serving` feature. `pct_calories_from_*` are
   scale-invariant and unaffected; only the `calories_per_serving` dimension
   is distorted for recipes that don't actually serve 4. Follow-up: source a
   real servings estimate or cluster these rows separately.
4. **XCom carries file paths, not data.** Fine at this scale; revisit if the
   per-run row count grows large.

## Local testing vs. deployment

Apache Airflow 2.x requires Python < 3.13; the dev machine runs 3.13, so
Airflow is not installed in the local `.venv`. Coverage:

- `tests/test_dag_tasks.py` — every step exercised with fakes (no Airflow, no
  network, no DB), including all three gate outcomes and the full
  extract→…→write chain on the skip path.
- `tests/test_clustering.py` — gate boundaries + real K-Means fit/predict.
- `tests/test_dag_structure.py` — `TASK_SEQUENCE` matches the CLAUDE.md order;
  the real `DagBag` parse + edge check runs when Airflow is importable
  (skipped locally, run in the `apache/airflow:2.10.3` container).

Run the container parse check:

```bash
docker run --rm -v "$PWD/airflow/dags:/opt/airflow/dags:ro" \
  apache/airflow:2.10.3-python3.12 \
  python -c "from airflow.models import DagBag; b=DagBag('/opt/airflow/dags', include_examples=False); print(b.import_errors or 'OK'); print(list(b.get_dag('food_rec_offline_pipeline').task_ids))"
```
