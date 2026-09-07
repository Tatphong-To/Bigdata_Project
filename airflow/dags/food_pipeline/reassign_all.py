"""Apply the latest trained K-Means model to EVERY row in menu_catalog.

The DAG's ``assign_cluster_labels`` only predicts on the rows a given run
extracted. If a run trains successfully but extracts nothing new (e.g. the
Spoonacular daily budget is spent), zero rows get a ``cluster_id`` even
though the fit succeeded and was logged to MLflow. This helper does the
catalog-wide assignment as a deliberate **full overwrite** — not the
COALESCE-guarded skip-path upsert (`catalog_repo.upsert_menu_rows`), which
must stay as-is for its own case.

Run this once after every successful ``train_or_update_kmeans``.

It does NOT retrain, does NOT touch the retrain-trigger / gate logic, and
does NOT recompute the ``pct_calories_from_*`` formula — it reads the feature
columns already stored on each row (the same source
``train_or_update_kmeans`` fits on) and calls ``model.predict``.
"""

from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import catalog_repo
from .features import FEATURE_COLUMNS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReassignResult:
    model_version: str
    provisional: bool
    n_catalog_rows_with_features: int
    n_assigned: int
    cluster_counts: dict[int, int] = field(default_factory=dict)


def _default_load_model(path: str | Path) -> Any:
    with open(path, "rb") as fh:
        return pickle.load(fh)


def reassign_all(
    model_path: str | Path,
    model_version: str,
    *,
    provisional: bool = True,
    connect_fn: Callable[[], Any] | None = None,
    load_model: Callable[[str | Path], Any] | None = None,
    fetch_features: Callable[[], list[dict[str, Any]]] | None = None,
    apply_labels: Callable[[dict[str, int]], int] | None = None,
) -> ReassignResult:
    """Predict a cluster for every catalog row that has all four features and
    write it back (overwriting any previous assignment).

    ``fetch_features`` / ``apply_labels`` / ``load_model`` are injectable for
    testing; the defaults hit Postgres via ``catalog_repo``.
    """
    conn_kw = {"connect_fn": connect_fn} if connect_fn is not None else {}
    load_model = load_model or _default_load_model
    fetch_features = fetch_features or (
        lambda: catalog_repo.fetch_existing_feature_rows(**conn_kw)
    )
    apply_labels = apply_labels or (
        lambda assignments: catalog_repo.update_cluster_labels(
            assignments,
            model_version=model_version,
            provisional=provisional,
            **conn_kw,
        )
    )

    rows = list(fetch_features())
    matrix = [[float(r[c]) for c in FEATURE_COLUMNS] for r in rows]

    if matrix:
        import numpy as np

        pipeline = load_model(model_path)
        labels = [int(x) for x in pipeline.predict(np.asarray(matrix, dtype=float))]
    else:
        labels = []

    assignments = {str(r["menu_id"]): lab for r, lab in zip(rows, labels)}
    n_assigned = int(apply_labels(assignments)) if assignments else 0

    counts: dict[int, int] = {}
    for lab in labels:
        counts[lab] = counts.get(lab, 0) + 1

    logger.info(
        "reassign_all: applied %s to %d/%d catalog rows; cluster counts %s",
        model_version, n_assigned, len(rows), counts,
    )
    return ReassignResult(
        model_version=model_version,
        provisional=provisional,
        n_catalog_rows_with_features=len(rows),
        n_assigned=n_assigned,
        cluster_counts=counts,
    )


def reassign_from_descriptor(
    descriptor_path: str | Path, **kwargs: Any
) -> ReassignResult:
    """Read a ``05_model.json`` descriptor (as written by
    ``train_or_update_kmeans``) and reassign the whole catalog from it."""
    d = json.loads(Path(descriptor_path).read_text(encoding="utf-8"))
    model_path = Path(d["model_path"])
    if not model_path.exists():
        # descriptor stores a repo-relative path; try next to the descriptor
        model_path = Path(descriptor_path).parent / model_path.name
    return reassign_all(
        model_path,
        d["model_version"],
        provisional=bool(d.get("provisional", True)),
        **kwargs,
    )


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Full catalog K-Means re-assign")
    ap.add_argument("descriptor", help="path to a 05_model.json")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    res = reassign_from_descriptor(args.descriptor)
    print(res)
