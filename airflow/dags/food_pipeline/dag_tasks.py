"""The seven Phase 3 pipeline steps as plain, testable callables.

Order is fixed (CLAUDE.md) and encoded in :data:`TASK_SEQUENCE`:

    extract_menus -> validate_nutrition_data -> clean -> compute_nutrition_ratios
    -> train_or_update_kmeans -> assign_cluster_labels -> write_to_menu_catalog

Each step reads/writes a numbered JSON file under a per-run directory and
returns the path it wrote, so the Airflow DAG only shuttles small path strings
through XCom and every step is unit-testable without Airflow.

Heavy dependencies (sklearn, psycopg, mlflow) are imported lazily by the
modules these call, so importing this module — and the DAG that wraps it —
needs only the stdlib.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import catalog_repo, clustering, mlflow_tracking
from .compute_recipe_nutrition import (
    NUTRITION_SOURCE_SPOONACULAR,
    NUTRITION_SOURCE_USDA,
    CompletenessConfig,
    compute_recipe_nutrition,
)
from .config import ExtractConfig
from .extract import run_extraction
from .features import FEATURE_COLUMNS, build_feature_rows
from .ingredient_matcher import MatchConfig
from .quota import QuotaTracker
from .themealdb import TheMealDbClient
from .unit_converter import UnitConverterConfig
from .usda_client import UsdaClient, UsdaConfig
from .validate import validate_batch

logger = logging.getLogger(__name__)

TASK_SEQUENCE: tuple[str, ...] = (
    "extract_menus",
    "validate_nutrition_data",
    "clean",
    "compute_nutrition_ratios",
    "train_or_update_kmeans",
    "assign_cluster_labels",
    "write_to_menu_catalog",
)

# file names each step produces inside the run directory
FILE_EXTRACTED = "01_extracted.json"
FILE_VALIDATED = "02_validated.json"
FILE_REJECTED = "02_rejected.json"
FILE_CLEANED = "03_cleaned.json"
FILE_RATIOS = "04_ratios.json"
FILE_MODEL_PKL = "05_model.pkl"
FILE_MODEL_JSON = "05_model.json"
FILE_SKIP = "05_skip.json"
FILE_ASSIGNMENTS = "06_assignments.json"
FILE_WRITE_SUMMARY = "07_write_summary.json"

_DEFAULT_SPOONACULAR_QUERIES = (
    "chicken", "beef", "salmon", "tofu", "pasta", "salad", "rice bowl",
    "soup", "curry", "stir fry", "breakfast", "vegetarian",
)
_DEFAULT_THEMEALDB_QUERIES = ("chicken", "beef", "pasta")

# TheMealDB has no servings count. Phase 3 assumes a value so usda_estimated
# rows have a calories_per_serving feature. pct_calories_from_* are
# scale-invariant and unaffected. Documented as a Phase 3 limitation.
DEFAULT_ASSUMED_SERVINGS = 4.0


class PipelineSkip(Exception):
    """Raised by a step that should be skipped (and its downstream too).

    The Airflow wrapper turns this into ``AirflowSkipException``.
    """

    def __init__(self, message: str, detail_path: str | None = None) -> None:
        super().__init__(message)
        self.detail_path = detail_path


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _run_path(run_dir: str | Path, name: str) -> Path:
    p = Path(run_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / name


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> str:
    path.write_text(json.dumps(data, default=str), encoding="utf-8")
    return str(path)


def _recipe_nutrition_to_row(rn: Any, meal: Any, assumed_servings: float) -> dict[str, Any]:
    servings = rn.servings or assumed_servings
    return {
        "menu_id": f"themealdb-{rn.meal_id}",
        "source": "themealdb",
        "nutrition_source": NUTRITION_SOURCE_USDA,
        "name": rn.name,
        "servings": servings,
        "calories": (rn.total_calories or 0.0) / servings,
        "protein_g": (rn.total_protein_g or 0.0) / servings,
        "carbs_g": (rn.total_carbs_g or 0.0) / servings,
        "fat_g": (rn.total_fat_g or 0.0) / servings,
        "ingredients": [c.name for c in rn.used],
        "diet_tags": list(meal.tags),
        "completeness": rn.completeness,
        "nutrition_notes": list(rn.notes),
    }


# --------------------------------------------------------------------------
# 1. extract_menus
# --------------------------------------------------------------------------
def extract_menus(
    *,
    run_dir: str | Path,
    include_spoonacular: bool = True,
    include_themealdb_usda: bool = True,
    spoonacular_queries: tuple[str, ...] = _DEFAULT_SPOONACULAR_QUERIES,
    themealdb_queries: tuple[str, ...] = _DEFAULT_THEMEALDB_QUERIES,
    themealdb_recipes_per_query: int = 3,
    assumed_servings: float = DEFAULT_ASSUMED_SERVINGS,
    env: dict[str, str] | None = None,
    quota_store: Any | None = None,
    spoonacular_client: Any | None = None,
    themealdb_client: Any | None = None,
    usda_client: Any | None = None,
) -> str:
    """Pull recipes for the catalog.

    **Spoonacular is the primary source** — quota-paced against the persisted
    daily point budget (~50 pts/day). The daily DAG run relies on this alone
    to grow the ``spoonacular_computed`` catalog toward the "stable" gate
    (500+); the first real run already produced 231 rows, past the 150 gate.

    TheMealDB -> USDA is a **secondary supplement**, kept for later use, not a
    growth target. The Phase 2b code path is unchanged and still available
    (``include_themealdb_usda=True``); the daily DAG wrapper leaves it off.
    When enabled it only contributes rows that pass the completeness guard —
    ``dropped_for_completeness`` rows never reach compute_nutrition_ratios.
    """
    env = env or dict(os.environ)
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {"spoonacular": 0, "themealdb_usda": 0, "themealdb_dropped": 0}

    if include_spoonacular:
        if spoonacular_client is None:
            cfg = ExtractConfig.from_env(env)
            if quota_store is None:
                from .db import PostgresQuotaStore

                quota_store = PostgresQuotaStore()
            tracker = QuotaTracker(quota_store, cfg.daily_point_quota,
                                   safety_margin_points=cfg.safety_margin_points)
            from .spoonacular import SpoonacularClient

            spoonacular_client = SpoonacularClient(cfg, tracker)
        run = run_extraction(
            spoonacular_client,
            queries=list(spoonacular_queries),
            out_dir=Path(run_dir) / "spoonacular_raw",
        )
        for r in run.accepted:
            r = dict(r)
            r.setdefault("source", "spoonacular")
            r["nutrition_source"] = NUTRITION_SOURCE_SPOONACULAR
            rows.append(r)
        counts["spoonacular"] = len(run.accepted)
        logger.info(
            "extract_menus: spoonacular -> %d rows (points %.2f->%.2f, stopped_early=%s)",
            len(run.accepted), run.points_used_before, run.points_used_after,
            run.stopped_early,
        )

    if include_themealdb_usda:
        mealdb = themealdb_client or TheMealDbClient()
        usda = usda_client or UsdaClient(UsdaConfig.from_env(env))
        match_cfg = MatchConfig()
        unit_cfg = UnitConverterConfig()
        comp_cfg = CompletenessConfig()  # provisional default threshold
        seen: set[str] = set()
        for q in themealdb_queries:
            try:
                meals = mealdb.parsed_search(q)
            except Exception as exc:
                logger.warning("extract_menus: themealdb search %r failed: %s", q, exc)
                continue
            for meal in meals[:themealdb_recipes_per_query]:
                if meal.meal_id in seen:
                    continue
                seen.add(meal.meal_id)
                try:
                    rn = compute_recipe_nutrition(
                        meal, usda.search_foods, servings=assumed_servings,
                        match_config=match_cfg, unit_config=unit_cfg,
                        completeness_config=comp_cfg,
                    )
                except Exception as exc:
                    logger.warning(
                        "extract_menus: nutrition for meal %s failed: %s",
                        meal.meal_id, exc,
                    )
                    continue
                if rn.dropped_for_completeness or rn.total_calories is None:
                    counts["themealdb_dropped"] += 1
                    continue
                rows.append(_recipe_nutrition_to_row(rn, meal, assumed_servings))
        counts["themealdb_usda"] = len(rows) - counts["spoonacular"]
        logger.info(
            "extract_menus: themealdb->usda -> %d kept, %d dropped for completeness",
            counts["themealdb_usda"], counts["themealdb_dropped"],
        )

    out = _run_path(run_dir, FILE_EXTRACTED)
    logger.info("extract_menus: %d candidate rows total %s", len(rows), counts)
    return _write_json(out, {"rows": rows, "counts": counts})


# --------------------------------------------------------------------------
# 2. validate_nutrition_data
# --------------------------------------------------------------------------
def validate_nutrition_data(in_path: str | Path, *, run_dir: str | Path) -> str:
    payload = _read_json(in_path)
    rows = payload["rows"]
    accepted, rejected = validate_batch(rows)
    _write_json(
        _run_path(run_dir, FILE_REJECTED),
        [{"menu_id": r.menu_id, "name": r.name, "reasons": list(r.reasons)} for r in rejected],
    )
    logger.info(
        "validate_nutrition_data: %d accepted, %d rejected", len(accepted), len(rejected)
    )
    return _write_json(_run_path(run_dir, FILE_VALIDATED), {"rows": accepted})


# --------------------------------------------------------------------------
# 3. clean
# --------------------------------------------------------------------------
def clean(in_path: str | Path, *, run_dir: str | Path) -> str:
    """Deterministic tidy-up: dedupe by menu_id, drop rows still missing a
    macro/name, normalise ingredient/tag lists, coerce numerics to float."""
    payload = _read_json(in_path)
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    dropped = 0
    for row in payload["rows"]:
        mid = str(row.get("menu_id") or "").strip()
        name = (row.get("name") or "").strip()
        if not mid or not name or mid in seen:
            dropped += 1
            continue
        try:
            row = {
                **row,
                "menu_id": mid,
                "name": name,
                "calories": float(row["calories"]),
                "protein_g": float(row["protein_g"]),
                "carbs_g": float(row["carbs_g"]),
                "fat_g": float(row["fat_g"]),
                "servings": float(row["servings"]) if row.get("servings") is not None else None,
                "ingredients": sorted({str(i).strip().lower() for i in row.get("ingredients", []) if str(i).strip()}),
                "diet_tags": sorted({str(t).strip().lower() for t in row.get("diet_tags", []) if str(t).strip()}),
            }
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
        seen.add(mid)
        cleaned.append(row)
    logger.info("clean: %d rows kept, %d dropped", len(cleaned), dropped)
    return _write_json(_run_path(run_dir, FILE_CLEANED), {"rows": cleaned})


# --------------------------------------------------------------------------
# 4. compute_nutrition_ratios
# --------------------------------------------------------------------------
def compute_nutrition_ratios(in_path: str | Path, *, run_dir: str | Path) -> str:
    """Attach the four Layer B features using ``features.build_feature_rows``
    (unchanged formula). Rows whose ratios can't be computed are dropped."""
    payload = _read_json(in_path)
    rows = payload["rows"]
    feature_rows, dropped = build_feature_rows(rows)
    features_by_id = {f["menu_id"]: f for f in feature_rows}
    out_rows: list[dict[str, Any]] = []
    for row in rows:
        f = features_by_id.get(row["menu_id"])
        if f is None:
            continue
        out_rows.append({**row, **{c: f[c] for c in FEATURE_COLUMNS}})
    logger.info(
        "compute_nutrition_ratios: %d rows with features, %d dropped",
        len(out_rows), len(dropped),
    )
    return _write_json(
        _run_path(run_dir, FILE_RATIOS),
        {"rows": out_rows, "dropped": [{"menu_id": d.menu_id, "reason": d.reason} for d in dropped]},
    )


# --------------------------------------------------------------------------
# 5. train_or_update_kmeans  (minimum-catalog-size gate)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TrainOutcome:
    gate: str
    row_count: int
    model_version: str | None
    provisional: bool
    model_path: str | None
    descriptor_path: str


def train_or_update_kmeans(
    in_path: str | Path,
    *,
    run_dir: str | Path,
    now_iso: str,
    kmeans_config: clustering.KMeansConfig | None = None,
    fetch_existing: Callable[[], list[dict[str, Any]]] | None = None,
) -> str:
    """Gate on total catalog size, then (if allowed) fit K-Means. This is the
    ONLY place K-Means is trained.

      * < 150 rows -> raise PipelineSkip (skips this task AND assign)
      * 150-499    -> train, model_version marked provisional
      * >= 500     -> train, stable
    """
    payload = _read_json(in_path)
    new_rows = payload["rows"]

    fetch_existing = fetch_existing or catalog_repo.fetch_existing_feature_rows
    try:
        existing = fetch_existing()
    except Exception as exc:
        logger.warning("train_or_update_kmeans: could not read existing catalog features (%s) — using this run only", exc)
        existing = []

    merged: dict[str, dict[str, Any]] = {r["menu_id"]: r for r in existing}
    for r in new_rows:
        merged[r["menu_id"]] = r
    training_rows = list(merged.values())
    row_count = len(training_rows)
    gate = clustering.catalog_size_gate(row_count)

    if gate == clustering.GATE_SKIP:
        msg = (
            f"catalog has {row_count} rows, minimum {clustering.MIN_CATALOG_SIZE} "
            f"required to train K-Means — skipping train_or_update_kmeans and "
            f"assign_cluster_labels this run"
        )
        logger.warning("train_or_update_kmeans: %s", msg)
        skip_path = _write_json(
            _run_path(run_dir, FILE_SKIP),
            {"gate": gate, "row_count": row_count, "reason": msg},
        )
        raise PipelineSkip(msg, detail_path=skip_path)

    matrix = [[float(r[c]) for c in FEATURE_COLUMNS] for r in training_rows]
    trained = clustering.train_kmeans(
        matrix, row_count=row_count, now_iso=now_iso, config=kmeans_config
    )

    model_path = _run_path(run_dir, FILE_MODEL_PKL)
    with open(model_path, "wb") as fh:
        pickle.dump(trained._pipeline, fh)

    tracking = mlflow_tracking.log_kmeans_run(
        model_version=trained.model_version,
        params=trained.params,
        metrics=trained.metrics,
        tags={"gate": gate, "provisional": trained.provisional},
        fallback_dir=Path(run_dir) / "models",
    )
    logger.info(
        "train_or_update_kmeans: gate=%s row_count=%d model_version=%s tracking=%s",
        gate, row_count, trained.model_version, tracking,
    )
    return _write_json(
        _run_path(run_dir, FILE_MODEL_JSON),
        {
            "gate": gate,
            "row_count": row_count,
            "model_version": trained.model_version,
            "provisional": trained.provisional,
            "model_path": str(model_path),
            "params": trained.params,
            "metrics": trained.metrics,
            "tracking": tracking,
        },
    )


# --------------------------------------------------------------------------
# 6. assign_cluster_labels
# --------------------------------------------------------------------------
def assign_cluster_labels(
    ratios_path: str | Path, model_descriptor_path: str | Path, *, run_dir: str | Path
) -> str:
    """Predict a cluster_id for this run's rows using the freshly-fitted model.

    MVP: only this run's rows are (re)labelled; rows already in the catalog
    keep their previous cluster_id until they are re-extracted. Documented.
    """
    descriptor = _read_json(model_descriptor_path)
    model_path = descriptor["model_path"]
    with open(model_path, "rb") as fh:
        pipeline = pickle.load(fh)

    rows = _read_json(ratios_path)["rows"]
    matrix = [[float(r[c]) for c in FEATURE_COLUMNS] for r in rows]
    if matrix:
        import numpy as np

        labels = [int(x) for x in pipeline.predict(np.asarray(matrix, dtype=float))]
    else:
        labels = []
    assignments = {row["menu_id"]: lab for row, lab in zip(rows, labels)}
    logger.info(
        "assign_cluster_labels: assigned %d rows with %s",
        len(assignments), descriptor["model_version"],
    )
    return _write_json(
        _run_path(run_dir, FILE_ASSIGNMENTS),
        {
            "model_version": descriptor["model_version"],
            "provisional": descriptor["provisional"],
            "assignments": assignments,
        },
    )


# --------------------------------------------------------------------------
# 7. write_to_menu_catalog
# --------------------------------------------------------------------------
def write_to_menu_catalog(
    ratios_path: str | Path,
    *,
    run_dir: str | Path,
    upsert: Callable[..., int] | None = None,
) -> str:
    """Upsert this run's rows into ``food_db.menu_catalog`` with their
    nutrition, ratios and (if training ran) cluster_id / model_version /
    model_provisional. Runs even when train + assign were skipped — the rows
    are written without a cluster so the catalog can grow toward the gate.
    """
    rows = _read_json(ratios_path)["rows"]

    assignments: dict[str, int] = {}
    model_version: str | None = None
    provisional = False
    assign_path = Path(run_dir) / FILE_ASSIGNMENTS
    if assign_path.exists():
        a = _read_json(assign_path)
        assignments = {k: int(v) for k, v in a["assignments"].items()}
        model_version = a["model_version"]
        provisional = bool(a["provisional"])

    catalog_rows = []
    for row in rows:
        cid = assignments.get(row["menu_id"])
        catalog_rows.append({
            **row,
            "cluster_id": cid,
            "model_version": model_version if cid is not None else None,
            "model_provisional": provisional if cid is not None else False,
        })

    upsert = upsert or catalog_repo.upsert_menu_rows
    written = upsert(catalog_rows)
    summary = {
        "rows_written": written,
        "rows_with_cluster": sum(1 for r in catalog_rows if r["cluster_id"] is not None),
        "model_version": model_version,
        "model_provisional": provisional,
        "clustering_ran": bool(assignments),
    }
    logger.info("write_to_menu_catalog: %s", summary)
    return _write_json(_run_path(run_dir, FILE_WRITE_SUMMARY), summary)
