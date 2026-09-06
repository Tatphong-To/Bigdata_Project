# Phase 7 — MLOps (MLflow tracking, retrain trigger, cluster-quality guard)

Builds on Phase 3, which already trains K-Means only in the Airflow
`train_or_update_kmeans` task and logs each run to the MLflow server
(`MLFLOW_TRACKING_URI=http://mlflow:5000`, backend store = `mlflow_db` in
Postgres). Phase 7 fills in what `PLAN.md` still wanted.

## MLflow — what each run now carries

`mlflow_tracking.log_kmeans_run` (called once per trained model):

**Params**

| param | meaning |
|---|---|
| `k`, `requested_k` | clusters used / requested (`k` is capped at `n_samples`) |
| `seed`, `n_init`, `max_iter`, `standardize` | K-Means config |
| `features` | the four Layer B feature columns |
| `n_samples` | rows actually fed to the fit (have all four finite features, deduped) |
| `gate_row_count` | rows the **150 / 500 gate** saw (existing catalog feature-rows + this run's, deduped) |
| **`catalog_row_count`** | `SELECT count(*) FROM menu_catalog` at train time — the true catalog size, can exceed `gate_row_count` when some rows lack usable cluster features |
| `catalog_growth_fraction` | growth vs. the last logged train (see retrain trigger) |
| `gate` | `provisional` \| `stable` (kept in params for history) |

`n_samples`, `gate_row_count` and `catalog_row_count` are logged as three
distinct params so they can be compared even when they coincide (they do
today: 335 / 335 / 335).

**Metrics** — `inertia` (always) and `silhouette` (when `2 <= k < n_samples`,
i.e. every real run). Unchanged from Phase 3, verified still logged.

**Tags** — `gate_tier` (`skip` never reaches logging, so in practice
`provisional` \| `stable`), `provisional` (bool), `model_version`. The Phase 3
tag was named `gate`; it is now `gate_tier` (renamed, not duplicated).

## Retrain trigger — "catalog grew ≥ 20%"

A **second** gate in `train_or_update_kmeans`, *alongside* the Phase 3
150 / 500 gate — it does not replace it. Order:

1. **min-catalog-size gate (Phase 3, unchanged):** `< 150` feature-rows →
   `PipelineSkip` (`skip_gate = "min_catalog_size"`).
2. **retrain trigger (Phase 7):** read the last logged run's
   `catalog_row_count` from MLflow; if the catalog has grown by less than
   **`RETRAIN_MIN_GROWTH_FRACTION` (0.20)** since then →
   `PipelineSkip` with
   `"catalog grew only X% since last train (… threshold 20%), skipping retrain this run"`
   (`skip_gate = "retrain_trigger"`, growth fraction written to `05_skip.json`).
   No prior run on record → always train.
3. fit K-Means.
4. cluster-quality check (below).

### Why 20%

The catalog grows **slowly**: Spoonacular's free tier is ~50 points/day
(`docs/spoonacular-quota.md`), and a manually-triggered daily run adds at
most a couple of hundred recipes — often far fewer once the common queries
are spent (a top-up run on 2026-09-06 added 118, a later one 0). Re-fitting
K-Means for 2–3 new rows is not worth the compute, and every re-fit churns
`cluster_id`, which the Model Service reads on every `/recommend`. 20% growth
is the point where a re-fit can plausibly shift cluster boundaries enough to
matter. It is a tunable constant (`retrain_policy.RETRAIN_MIN_GROWTH_FRACTION`
/ the `min_growth_fraction` arg), **not** a hard architectural rule like the
150 / 500 gate.

## Cluster-quality check

After a successful fit, `retrain_policy.check_cluster_quality` compares this
run's `silhouette` with the **previous run that carried the same
`gate_tier`** (from MLflow). If it dropped by more than
`CLUSTER_QUALITY_SILHOUETTE_DROP` (**0.05** absolute) it logs a **WARNING**:

> cluster quality may have degraded: silhouette 0.30 → 0.24 (down 0.06,
> threshold 0.05). Possible causes: catalog composition changed (e.g. a new
> source mix), or k is no longer well suited to the larger catalog. No
> automatic fix — review k / features.

Advisory only — nothing is changed automatically. No comparable previous
silhouette (first run of a tier, MLflow unreachable) → the check is skipped.

## No `mlruns/`, no Parquet

Verified: no `mlruns/` directory and no `*.parquet` anywhere in the repo or
working tree; `.gitignore` still guards both; the MLflow server runs with
`--backend-store-uri postgresql+psycopg2://…/mlflow_db` (Postgres), and
`mlflow_db.runs` holds the run rows. Model artifacts (the fitted pipeline
pickle) live in the `mlflow_artifacts` Docker volume — an artifact store, not
a tracking file store, and not in the repo.

## Behaviour when MLflow is unreachable

`mlflow_tracking.latest_run_summary` returns `None` (logged at WARNING). The
retrain trigger then treats it as "no previous run" and **trains**; the
cluster-quality check is skipped. Logging itself falls back to
`data/pipeline_runs/<run>/models/<model_version>.mlflow-fallback.json`.

## Tests

- `tests/test_retrain_policy.py` (12) — growth below / at / above 20%, no
  prior run, shrinking catalog, threshold configurable; silhouette drop past
  / under 0.05, improvement, missing values, threshold configurable.
- `tests/test_dag_tasks.py` (+7) — `catalog_row_count` logged distinctly from
  `gate_row_count` / `n_samples`; `gate_tier` tag set (not the old `gate`);
  retrain trigger skips < 20% (with the reason + `05_skip.json`) and trains
  ≥ 20% and when no prior run; the cluster-quality WARNING fires on a drop;
  the min-catalog-size gate still wins when both would skip.
