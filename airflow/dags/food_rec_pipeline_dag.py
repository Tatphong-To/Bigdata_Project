"""Food & nutrition recommendation — offline pipeline DAG (Phase 3).

Fixed order (CLAUDE.md, do not reorder / merge / skip):

    extract_menus -> validate_nutrition_data -> clean -> compute_nutrition_ratios
    -> train_or_update_kmeans -> assign_cluster_labels -> write_to_menu_catalog

K-Means is trained ONLY in ``train_or_update_kmeans`` here. The Model Service
never trains — it reads an already-assigned ``cluster_id``.

Runs daily as a recurring job (not a one-time bulk load) — ``extract_menus``
respects the persisted Spoonacular point budget, so each run pulls a small
batch and the catalog grows over time.

Minimum-catalog-size gate: with < 150 total catalog rows,
``train_or_update_kmeans`` and ``assign_cluster_labels`` skip (logged);
``write_to_menu_catalog`` still runs so the catalog can reach the threshold.
150-499 rows -> model marked provisional; >= 500 -> stable.

All business logic lives in ``food_pipeline.dag_tasks`` (stdlib-only to
import; sklearn / psycopg / mlflow load lazily when a task runs), so this file
parses in a bare Airflow image.
"""

from __future__ import annotations

import datetime as _dt
import re

import pendulum
from airflow.decorators import dag, task
from airflow.exceptions import AirflowSkipException
from airflow.operators.python import get_current_context
from airflow.utils.trigger_rule import TriggerRule

from food_pipeline import dag_tasks

RUN_ROOT = "data/pipeline_runs"


def _run_dir() -> str:
    ctx = get_current_context()
    run_id = re.sub(r"[^0-9A-Za-z._-]", "_", str(ctx["run_id"]))
    return f"{RUN_ROOT}/{run_id}"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


@dag(
    dag_id="food_rec_offline_pipeline",
    description="Extract -> validate -> clean -> ratios -> KMeans -> assign -> write",
    schedule="@daily",
    start_date=pendulum.datetime(2026, 9, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": _dt.timedelta(minutes=5)},
    tags=["food-rec", "layer-b", "kmeans"],
)
def food_rec_offline_pipeline():
    @task(task_id="extract_menus")
    def extract_menus() -> str:
        # Spoonacular-only for the daily run: it alone grows the catalog toward
        # the stable gate and avoids mixing in usda_estimated noise. The
        # TheMealDB -> USDA path (Phase 2b) stays available for opt-in use.
        return dag_tasks.extract_menus(run_dir=_run_dir(), include_themealdb_usda=False)

    @task(task_id="validate_nutrition_data")
    def validate_nutrition_data(in_path: str) -> str:
        return dag_tasks.validate_nutrition_data(in_path, run_dir=_run_dir())

    @task(task_id="clean")
    def clean(in_path: str) -> str:
        return dag_tasks.clean(in_path, run_dir=_run_dir())

    @task(task_id="compute_nutrition_ratios")
    def compute_nutrition_ratios(in_path: str) -> str:
        return dag_tasks.compute_nutrition_ratios(in_path, run_dir=_run_dir())

    @task(task_id="train_or_update_kmeans")
    def train_or_update_kmeans(in_path: str) -> str:
        try:
            return dag_tasks.train_or_update_kmeans(
                in_path, run_dir=_run_dir(), now_iso=_now_iso()
            )
        except dag_tasks.PipelineSkip as skip:
            raise AirflowSkipException(str(skip)) from None

    @task(task_id="assign_cluster_labels")
    def assign_cluster_labels(ratios_path: str, model_descriptor_path: str) -> str:
        return dag_tasks.assign_cluster_labels(
            ratios_path, model_descriptor_path, run_dir=_run_dir()
        )

    @task(task_id="write_to_menu_catalog", trigger_rule=TriggerRule.NONE_FAILED)
    def write_to_menu_catalog(ratios_path: str) -> str:
        return dag_tasks.write_to_menu_catalog(ratios_path, run_dir=_run_dir())

    extracted = extract_menus()
    validated = validate_nutrition_data(extracted)
    cleaned = clean(validated)
    ratios = compute_nutrition_ratios(cleaned)
    model = train_or_update_kmeans(ratios)
    assigned = assign_cluster_labels(ratios, model)
    written = write_to_menu_catalog(ratios)

    # keep the fixed linear order even though write only needs `ratios`
    model >> assigned >> written


dag = food_rec_offline_pipeline()
