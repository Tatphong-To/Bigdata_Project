"""Regression test for the cluster_id-wipe bug.

`upsert_menu_rows` is called by `write_to_menu_catalog` on every DAG run,
including runs where train + assign_cluster_labels were skipped (the common
case with the Phase 7 retrain trigger). On those runs the re-extracted
recipes carry `cluster_id = NULL`; the old `SET cluster_id = EXCLUDED.cluster_id`
blanked out the cluster assignment stored from an earlier successful K-Means
run. The fix: only overwrite cluster_id / model_version / model_provisional
when the incoming row actually has a cluster.

Runs against the local Postgres (port 5433 / $FOOD_DB_DSN). Skipped if it is
not reachable. Only touches a single sentinel `menu_id`, cleaned up after.
"""

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from food_pipeline import catalog_repo

_DSN = os.environ.get(
    "FOOD_DB_DSN", "postgresql://food_user:food_pass@localhost:5433/food_db"
)
_SENTINEL = "__test_upsert_preserve_cluster__"


def _connect():
    return psycopg.connect(_DSN, connect_timeout=3)


@pytest.fixture
def db():
    try:
        conn = _connect()
    except Exception as exc:  # no local Postgres -> skip, don't fail the suite
        pytest.skip(f"local Postgres not reachable ({exc})")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM menu_catalog WHERE menu_id = %s", (_SENTINEL,))
    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM menu_catalog WHERE menu_id = %s", (_SENTINEL,))
        conn.close()


def _row(**over):
    r = {
        "menu_id": _SENTINEL, "source": "spoonacular",
        "nutrition_source": "spoonacular_computed", "name": "Sentinel Dish",
        "servings": 4, "calories": 500.0, "protein_g": 30.0, "carbs_g": 40.0,
        "fat_g": 20.0, "ingredients": ["x"], "diet_tags": [],
        "pct_calories_from_protein": 0.24, "pct_calories_from_carbs": 0.32,
        "pct_calories_from_fat": 0.36, "calories_per_serving": 500.0,
        "cluster_id": None, "model_version": None, "model_provisional": False,
    }
    r.update(over)
    return r


def _read(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cluster_id, model_version, model_provisional, calories, name "
            "FROM menu_catalog WHERE menu_id = %s", (_SENTINEL,),
        )
        return cur.fetchone()


def test_skip_path_upsert_keeps_existing_cluster(db):
    # a row that WAS clustered by an earlier successful K-Means run
    catalog_repo.upsert_menu_rows(
        [_row(cluster_id=7, model_version="kmeans-earlier-provisional",
              model_provisional=True)],
        connect_fn=_connect,
    )
    assert _read(db)[:3] == (7, "kmeans-earlier-provisional", True)

    # a later run where train + assign were SKIPPED: same recipe re-extracted,
    # no cluster info, but nutrition/name refreshed
    catalog_repo.upsert_menu_rows(
        [_row(cluster_id=None, model_version=None, model_provisional=False,
              calories=612.0, name="Sentinel Dish v2")],
        connect_fn=_connect,
    )
    cluster_id, model_version, provisional, calories, name = _read(db)

    # the bug: these three used to become NULL / false
    assert cluster_id == 7
    assert model_version == "kmeans-earlier-provisional"
    assert provisional is True
    # everything else still updates normally
    assert float(calories) == pytest.approx(612.0)
    assert name == "Sentinel Dish v2"


def test_upsert_still_overwrites_cluster_when_new_one_is_given(db):
    catalog_repo.upsert_menu_rows(
        [_row(cluster_id=7, model_version="kmeans-old", model_provisional=True)],
        connect_fn=_connect,
    )
    # a run that DID retrain -> new assignment must replace the old one
    catalog_repo.upsert_menu_rows(
        [_row(cluster_id=2, model_version="kmeans-new", model_provisional=False)],
        connect_fn=_connect,
    )
    assert _read(db)[:3] == (2, "kmeans-new", False)


def test_first_insert_of_unclustered_row_is_fine(db):
    catalog_repo.upsert_menu_rows([_row()], connect_fn=_connect)  # cluster_id None
    cluster_id, model_version, provisional, *_ = _read(db)
    assert cluster_id is None and model_version is None and provisional is False
