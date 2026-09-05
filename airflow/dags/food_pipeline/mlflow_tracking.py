"""Track a K-Means training run's params / metrics / tags (Phase 3).

Prefers MLflow (backend store = ``mlflow_db``, per the project's Postgres-only
rule). If MLflow isn't installed or no tracking URI is configured, falls back
to a JSON record on disk + a log line so the DAG still runs.

TODO(Phase 7): wire the full MLflow setup — model registry, run comparison as
the catalog grows, retrain triggers. This module is the minimal hook.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_EXPERIMENT = "food_rec_kmeans"


def _tracking_uri() -> str | None:
    return (os.environ.get("MLFLOW_TRACKING_URI") or "").strip() or None


def log_kmeans_run(
    *,
    model_version: str,
    params: dict[str, Any],
    metrics: dict[str, Any],
    tags: dict[str, Any] | None = None,
    fallback_dir: str | Path = "data/models",
) -> dict[str, Any]:
    """Returns a small dict describing where the run was logged
    (``{"backend": "mlflow"|"fallback", "run_id"|"path": ...}``)."""
    tags = {"model_version": model_version, **(tags or {})}
    uri = _tracking_uri()

    if uri:
        try:
            import mlflow

            mlflow.set_tracking_uri(uri)
            mlflow.set_experiment(os.environ.get("MLFLOW_EXPERIMENT", _DEFAULT_EXPERIMENT))
            with mlflow.start_run(run_name=model_version) as run:
                mlflow.log_params({k: _scalar(v) for k, v in params.items()})
                mlflow.log_metrics({k: float(v) for k, v in metrics.items() if _is_number(v)})
                mlflow.set_tags({k: str(v) for k, v in tags.items()})
            logger.info("mlflow: logged run %s (%s)", run.info.run_id, model_version)
            return {"backend": "mlflow", "run_id": run.info.run_id, "tracking_uri": uri}
        except Exception as exc:  # never let tracking break the DAG
            logger.warning(
                "mlflow logging failed (%s) — falling back to local JSON", exc
            )

    # fallback
    out_dir = Path(fallback_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{model_version}.mlflow-fallback.json"
    record = {
        "logged_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "model_version": model_version,
        "params": params,
        "metrics": metrics,
        "tags": tags,
        "note": "MLflow not configured — TODO(Phase 7) wire mlflow_db backend",
    }
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    logger.info("mlflow fallback: wrote %s", path)
    return {"backend": "fallback", "path": str(path)}


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _scalar(v: Any) -> Any:
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(v, default=str)
    return v
