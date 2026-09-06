"""Track a K-Means training run's params / metrics / tags, and read back the
last run for the Phase 7 retrain trigger + cluster-quality check.

Prefers MLflow (backend store = ``mlflow_db``, per the project's Postgres-only
rule). If MLflow isn't installed or no tracking URI is configured, logging
falls back to a JSON record on disk and history reads return ``None``
(the caller then treats it as "no prior run").
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


def latest_run_summary(
    *, gate_tier: str | None = None, exclude_run_id: str | None = None
) -> dict[str, Any] | None:
    """Most recent K-Means run from MLflow (optionally filtered to a
    ``gate_tier`` tag, optionally skipping ``exclude_run_id``). Returns
    ``{run_id, catalog_row_count, n_samples, silhouette, gate_tier,
    start_time}`` or ``None`` when MLflow is unavailable / has no matching run.

    Used by the Phase 7 retrain trigger (compare ``catalog_row_count``) and
    the cluster-quality check (compare ``silhouette`` to the last same-tier
    run)."""
    uri = _tracking_uri()
    if not uri:
        return None
    try:
        from mlflow.tracking import MlflowClient

        client = MlflowClient(tracking_uri=uri)
        exp = client.get_experiment_by_name(
            os.environ.get("MLFLOW_EXPERIMENT", _DEFAULT_EXPERIMENT)
        )
        if exp is None:
            return None
        filt = f"tags.gate_tier = '{gate_tier}'" if gate_tier else ""
        runs = client.search_runs(
            [exp.experiment_id],
            filter_string=filt,
            order_by=["attributes.start_time DESC"],
            max_results=10,
        )
        for r in runs:
            if exclude_run_id and r.info.run_id == exclude_run_id:
                continue
            if r.info.lifecycle_stage != "active":
                continue
            p, m, t = r.data.params, r.data.metrics, r.data.tags
            return {
                "run_id": r.info.run_id,
                "catalog_row_count": _int_or_none(p.get("catalog_row_count")),
                "n_samples": _int_or_none(p.get("n_samples")),
                "silhouette": m.get("silhouette"),
                "gate_tier": t.get("gate_tier"),
                "start_time": r.info.start_time,
            }
        return None
    except Exception as exc:  # history is best-effort, never break the DAG
        logger.warning("mlflow: could not read run history (%s)", exc)
        return None


def _int_or_none(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _scalar(v: Any) -> Any:
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(v, default=str)
    return v
