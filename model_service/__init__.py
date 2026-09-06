"""FastAPI recommendation service (Phase 5).

Per-request pipeline, fixed order (CLAUDE.md — never reorder / merge / skip):

    safety filter (Phase 4)  ->  Layer A calculator (Mifflin-St Jeor)
    ->  read precomputed cluster_id  ->  Layer C deterministic ranking

This service **only reads** `cluster_id` from `menu_catalog`. It never trains
K-Means and imports no ML / clustering code — a grep/AST test enforces that.
"""

from __future__ import annotations

import pathlib
import sys

# The safety filter lives in the shared `food_pipeline` package under
# airflow/dags/. Put that on the path so `food_pipeline.safety_filter` (and
# `food_pipeline.db`, a lazy psycopg wrapper) import cleanly. Neither pulls in
# sklearn / clustering.
_DAGS_DIR = pathlib.Path(__file__).resolve().parents[1] / "airflow" / "dags"
if str(_DAGS_DIR) not in sys.path:
    sys.path.insert(0, str(_DAGS_DIR))
