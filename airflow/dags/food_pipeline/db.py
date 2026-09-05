"""Postgres access for the pipeline (food_db).

Only two things live here for Phase 1:
  * :func:`connect` — a psycopg3 connection from the environment;
  * :class:`PostgresQuotaStore` — the persisted point-counter backing
    :class:`food_pipeline.quota.QuotaTracker`.

``psycopg`` is imported lazily so the rest of the package (parser, validator,
cost model) can be imported and unit-tested without the driver installed.
"""

from __future__ import annotations

import datetime as _dt
import os
from collections.abc import Mapping
from typing import Any, Callable


def dsn_from_env(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    for key in ("FOOD_DB_DSN", "AIRFLOW_CONN_FOOD_DB"):
        value = env.get(key, "").strip()
        if value:
            return value
    raise RuntimeError(
        "No food_db DSN in environment (set FOOD_DB_DSN or AIRFLOW_CONN_FOOD_DB)"
    )


def connect(dsn: str | None = None):  # -> psycopg.Connection
    import psycopg

    return psycopg.connect(dsn or dsn_from_env())


class PostgresQuotaStore:
    """``extraction_quota`` table, one row per day. All writes are a single
    atomic ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` so concurrent
    Airflow tasks cannot lose an increment."""

    def __init__(self, connect_fn: Callable[[], Any] | None = None) -> None:
        self._connect = connect_fn or connect

    def get_usage(self, day: _dt.date) -> tuple[float, int]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT points_used, request_count "
                "FROM extraction_quota WHERE quota_date = %s",
                (day,),
            )
            row = cur.fetchone()
        if row is None:
            return 0.0, 0
        return float(row[0]), int(row[1])

    def add_usage(
        self, day: _dt.date, points: float, requests: int
    ) -> tuple[float, int]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extraction_quota
                    (quota_date, points_used, request_count, last_call_at, updated_at)
                VALUES (%s, %s, %s, now(), now())
                ON CONFLICT (quota_date) DO UPDATE SET
                    points_used   = extraction_quota.points_used  + EXCLUDED.points_used,
                    request_count = extraction_quota.request_count + EXCLUDED.request_count,
                    last_call_at  = now(),
                    updated_at    = now()
                RETURNING points_used, request_count
                """,
                (day, points, requests),
            )
            row = cur.fetchone()
        return float(row[0]), int(row[1])
