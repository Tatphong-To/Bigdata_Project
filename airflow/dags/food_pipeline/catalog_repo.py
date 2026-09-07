"""Reads and writes for ``food_db.menu_catalog`` (Phase 3).

Only ``train_or_update_kmeans`` (row count + existing features) and
``write_to_menu_catalog`` (upsert) touch this table in the DAG. ``psycopg`` is
imported lazily so the DAG file and the light task wiring can be imported
without the driver.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from .db import connect
from .features import FEATURE_COLUMNS

logger = logging.getLogger(__name__)

# menu_catalog columns written by the pipeline (raw_payload handled separately)
_UPSERT_COLUMNS = (
    "menu_id", "source", "nutrition_source", "name", "servings",
    "calories", "protein_g", "carbs_g", "fat_g",
    "ingredients", "diet_tags",
    "pct_calories_from_protein", "pct_calories_from_carbs",
    "pct_calories_from_fat", "calories_per_serving",
    "cluster_id", "model_version", "model_provisional",
)


def count_menu_catalog(*, connect_fn=connect) -> int:
    with connect_fn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM menu_catalog")
        return int(cur.fetchone()[0])


def fetch_existing_feature_rows(*, connect_fn=connect) -> list[dict[str, Any]]:
    """menu_id + the four feature columns for every row that already has all
    four (used as extra training data alongside the current run)."""
    cols = ", ".join(FEATURE_COLUMNS)
    where = " AND ".join(f"{c} IS NOT NULL" for c in FEATURE_COLUMNS)
    with connect_fn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT menu_id, {cols} FROM menu_catalog WHERE {where}")
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = {"menu_id": str(r[0])}
        for i, c in enumerate(FEATURE_COLUMNS, start=1):
            d[c] = float(r[i])
        out.append(d)
    return out


def _row_values(row: dict[str, Any]) -> tuple:
    return (
        str(row["menu_id"]),
        row.get("source", "spoonacular"),
        row.get("nutrition_source", "spoonacular_computed"),
        row.get("name"),
        row.get("servings"),
        row.get("calories"),
        row.get("protein_g"),
        row.get("carbs_g"),
        row.get("fat_g"),
        json.dumps(row.get("ingredients", [])),
        json.dumps(row.get("diet_tags", [])),
        row.get("pct_calories_from_protein"),
        row.get("pct_calories_from_carbs"),
        row.get("pct_calories_from_fat"),
        row.get("calories_per_serving"),
        row.get("cluster_id"),
        row.get("model_version"),
        bool(row.get("model_provisional", False)),
    )


def upsert_menu_rows(rows: Iterable[dict[str, Any]], *, connect_fn=connect) -> int:
    rows = list(rows)
    if not rows:
        return 0
    placeholders = ", ".join(["%s"] * len(_UPSERT_COLUMNS))
    col_list = ", ".join(_UPSERT_COLUMNS)
    # The three clustering columns are only overwritten when the incoming row
    # actually carries a cluster assignment. On a run where train +
    # assign_cluster_labels were skipped (the common case with the Phase 7
    # retrain trigger), extract still re-writes the same recipes with
    # cluster_id = NULL — without this, that would blank out the cluster_id /
    # model_version already stored from an earlier successful K-Means run.
    _preserve_when_no_cluster = {
        "cluster_id": "cluster_id = COALESCE(EXCLUDED.cluster_id, menu_catalog.cluster_id)",
        "model_version": "model_version = COALESCE(EXCLUDED.model_version, menu_catalog.model_version)",
        "model_provisional": (
            "model_provisional = CASE WHEN EXCLUDED.cluster_id IS NULL "
            "THEN menu_catalog.model_provisional ELSE EXCLUDED.model_provisional END"
        ),
    }
    updates = ", ".join(
        _preserve_when_no_cluster.get(c, f"{c} = EXCLUDED.{c}")
        for c in _UPSERT_COLUMNS
        if c != "menu_id"
    )
    sql = (
        f"INSERT INTO menu_catalog ({col_list}, updated_at) "
        f"VALUES ({placeholders}, now()) "
        f"ON CONFLICT (menu_id) DO UPDATE SET {updates}, updated_at = now()"
    )
    with connect_fn() as conn, conn.cursor() as cur:
        cur.executemany(sql, [_row_values(r) for r in rows])
    logger.info("upsert_menu_rows: wrote %d rows to menu_catalog", len(rows))
    return len(rows)


def update_cluster_labels(
    assignments: dict[str, int],
    *,
    model_version: str,
    provisional: bool,
    connect_fn=connect,
) -> int:
    """Set cluster_id / model_version / model_provisional on existing rows."""
    if not assignments:
        return 0
    sql = (
        "UPDATE menu_catalog SET cluster_id = %s, model_version = %s, "
        "model_provisional = %s, updated_at = now() WHERE menu_id = %s"
    )
    params = [
        (int(cid), model_version, provisional, str(mid))
        for mid, cid in assignments.items()
    ]
    with connect_fn() as conn, conn.cursor() as cur:
        cur.executemany(sql, params)
    logger.info(
        "update_cluster_labels: labelled %d rows with %s (provisional=%s)",
        len(params), model_version, provisional,
    )
    return len(params)
