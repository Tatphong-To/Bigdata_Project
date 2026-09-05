"""The DAG's task order is the fixed CLAUDE.md sequence, and (when Airflow is
installed) the real DAG file parses with exactly those tasks + edges."""

import pytest

from food_pipeline.dag_tasks import TASK_SEQUENCE

EXPECTED = (
    "extract_menus",
    "validate_nutrition_data",
    "clean",
    "compute_nutrition_ratios",
    "train_or_update_kmeans",
    "assign_cluster_labels",
    "write_to_menu_catalog",
)


def test_task_sequence_matches_claude_md_order():
    assert TASK_SEQUENCE == EXPECTED


def test_dag_file_parses_and_wires_tasks_in_order():
    # the repo has a top-level airflow/ dir (namespace pkg), so importorskip
    # must probe a real Airflow submodule, not just "airflow".
    pytest.importorskip(
        "airflow.models.dag",
        reason="airflow not installed locally (needs py<3.13); the real DAG "
        "parse runs in the apache/airflow:2.10.3 container",
    )
    import importlib

    dag_module = importlib.import_module("food_rec_pipeline_dag")
    dag = dag_module.dag
    assert dag.dag_id == "food_rec_offline_pipeline"
    assert set(dag.task_ids) == set(EXPECTED)

    # linear dependency chain in the fixed order
    for upstream, downstream in zip(EXPECTED, EXPECTED[1:]):
        assert downstream in dag.get_task(upstream).downstream_task_ids, (
            f"{upstream} -> {downstream} edge missing"
        )
    assert dag.schedule_interval == "@daily"
    from airflow.utils.trigger_rule import TriggerRule

    assert dag.get_task("write_to_menu_catalog").trigger_rule == TriggerRule.NONE_FAILED
