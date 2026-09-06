"""Read-only access to `menu_catalog` + the `prediction_log` insert.

The service NEVER writes cluster assignments or trains anything — it only
reads `cluster_id` / `model_version` that the Airflow pipeline already wrote.
`psycopg` is used via `food_pipeline.db.connect` (a lazy wrapper, no ML).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from food_pipeline.db import connect as _default_connect

ConnectFn = Callable[[], Any]


@dataclass(frozen=True)
class Candidate:
    menu_id: str
    name: str
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    ingredients: tuple[str, ...]
    diet_tags: tuple[str, ...]
    cluster_id: int | None

    def as_ranking_dict(self) -> dict[str, Any]:
        return {
            "menu_id": self.menu_id,
            "name": self.name,
            "calories": self.calories,
            "protein_g": self.protein_g,
            "carbs_g": self.carbs_g,
            "fat_g": self.fat_g,
            "cluster_id": self.cluster_id,
        }


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return [str(v) for v in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def fetch_candidates(*, connect_fn: ConnectFn = _default_connect) -> list[Candidate]:
    sql = (
        "SELECT menu_id, name, calories, protein_g, carbs_g, fat_g, "
        "ingredients, diet_tags, cluster_id "
        "FROM menu_catalog "
        "WHERE calories IS NOT NULL AND protein_g IS NOT NULL "
        "AND carbs_g IS NOT NULL AND fat_g IS NOT NULL "
        "ORDER BY menu_id"
    )
    with connect_fn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    out: list[Candidate] = []
    for r in rows:
        out.append(
            Candidate(
                menu_id=str(r[0]),
                name=str(r[1] or ""),
                calories=float(r[2]),
                protein_g=float(r[3]),
                carbs_g=float(r[4]),
                fat_g=float(r[5]),
                ingredients=tuple(_as_list(r[6])),
                diet_tags=tuple(_as_list(r[7])),
                cluster_id=int(r[8]) if r[8] is not None else None,
            )
        )
    return out


def fetch_cluster_centroids(
    *, connect_fn: ConnectFn = _default_connect
) -> dict[int, dict[str, float]]:
    """cluster_id -> mean per-serving macros over that cluster. Plain AVG,
    not ML — the deterministic 'group profile' the ranker compares against."""
    sql = (
        "SELECT cluster_id, avg(calories), avg(protein_g), avg(carbs_g), avg(fat_g) "
        "FROM menu_catalog WHERE cluster_id IS NOT NULL GROUP BY cluster_id"
    )
    with connect_fn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return {
        int(r[0]): {
            "calories": float(r[1]),
            "protein_g": float(r[2]),
            "carbs_g": float(r[3]),
            "fat_g": float(r[4]),
        }
        for r in rows
    }


def fetch_model_version(
    *, connect_fn: ConnectFn = _default_connect
) -> tuple[str, bool]:
    """The model_version / model_provisional carried by the largest block of
    clustered rows. ('no-model', False) if nothing is clustered yet."""
    sql = (
        "SELECT model_version, model_provisional, count(*) "
        "FROM menu_catalog WHERE cluster_id IS NOT NULL AND model_version IS NOT NULL "
        "GROUP BY model_version, model_provisional ORDER BY count(*) DESC LIMIT 1"
    )
    with connect_fn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    if row is None:
        return "no-model", False
    return str(row[0]), bool(row[1])


def write_prediction_log(
    record: dict[str, Any], *, connect_fn: ConnectFn = _default_connect
) -> None:
    """One anonymous row per request. No name / email / account / IP —
    system-quality monitoring only (README / schema comment)."""
    sql = (
        "INSERT INTO prediction_log ("
        "request_age, request_sex, request_weight_kg, request_height_cm, "
        "request_activity_level, request_goal, request_allergies, request_diet_type, "
        "target_calories, target_protein_g, target_carbs_g, target_fat_g, "
        "recommended_menu_ids, excluded_count, model_version) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    )
    params = (
        record.get("age"),
        record.get("sex"),
        record.get("weight_kg"),
        record.get("height_cm"),
        record.get("activity_level"),
        record.get("goal"),
        json.dumps(record.get("allergies", [])),
        record.get("diet_type"),
        record.get("target_calories"),
        record.get("target_protein_g"),
        record.get("target_carbs_g"),
        record.get("target_fat_g"),
        json.dumps(record.get("recommended_menu_ids", [])),
        record.get("excluded_count"),
        record.get("model_version"),
    )
    with connect_fn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
