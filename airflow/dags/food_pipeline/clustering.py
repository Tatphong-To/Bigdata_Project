"""K-Means over the Layer B nutrition-ratio features (Phase 3).

This is the ONLY place K-Means runs. It is imported lazily from inside the
Airflow task ``train_or_update_kmeans`` — never from the Model Service or any
request path (CLAUDE.md). A grep check enforces this.

Unsupervised: the feature matrix is only the four numbers from
``features.feature_matrix`` (pct protein/carb/fat, calories per serving). No
diet/allergy tag is ever a feature or target.

Minimum-catalog-size gate (Phase 3):
  * < 150 rows  -> "skip"        : do not train; skip assign_cluster_labels
  * 150-499     -> "provisional" : train, but mark the model provisional
  * >= 500      -> "stable"      : train normally
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from .features import FEATURE_COLUMNS

logger = logging.getLogger(__name__)

MIN_CATALOG_SIZE = 150
STABLE_CATALOG_SIZE = 500

GATE_SKIP = "skip"
GATE_PROVISIONAL = "provisional"
GATE_STABLE = "stable"


def catalog_size_gate(row_count: int) -> str:
    if row_count < MIN_CATALOG_SIZE:
        return GATE_SKIP
    if row_count < STABLE_CATALOG_SIZE:
        return GATE_PROVISIONAL
    return GATE_STABLE


@dataclass(frozen=True)
class KMeansConfig:
    k: int = 6
    seed: int = 42
    n_init: int = 10
    max_iter: int = 300
    features: tuple[str, ...] = FEATURE_COLUMNS
    # standardise features before fitting so calories_per_serving (0..1000+)
    # doesn't dominate the pct_* features (0..1)
    standardize: bool = True


@dataclass
class TrainedKMeans:
    model_version: str
    provisional: bool
    config: KMeansConfig
    n_samples: int
    params: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    _pipeline: object = None  # sklearn estimator (Pipeline or KMeans)

    def predict(self, feature_matrix: Sequence[Sequence[float]]) -> list[int]:
        if self._pipeline is None:
            raise RuntimeError("model has no fitted pipeline")
        import numpy as np

        if len(feature_matrix) == 0:
            return []
        return [int(x) for x in self._pipeline.predict(np.asarray(feature_matrix, dtype=float))]


def _make_model_version(now_iso: str, provisional: bool) -> str:
    stamp = now_iso.replace(":", "").replace("-", "")
    base = f"kmeans-{stamp}"
    return f"{base}-provisional" if provisional else base


def train_kmeans(
    feature_matrix: Sequence[Sequence[float]],
    *,
    row_count: int,
    now_iso: str,
    config: KMeansConfig | None = None,
) -> TrainedKMeans:
    """Fit K-Means on ``feature_matrix`` (one row per menu item, columns in
    ``FEATURE_COLUMNS`` order). ``row_count`` is the catalog size used for the
    gate (may differ from ``len(feature_matrix)`` only if some rows lack
    features — normally equal)."""
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    cfg = config or KMeansConfig()
    gate = catalog_size_gate(row_count)
    if gate == GATE_SKIP:
        raise ValueError(
            f"catalog has {row_count} rows, minimum {MIN_CATALOG_SIZE} required "
            f"to train K-Means — caller must not train"
        )
    provisional = gate == GATE_PROVISIONAL

    x = np.asarray(feature_matrix, dtype=float)
    if x.ndim != 2 or x.shape[1] != len(cfg.features):
        raise ValueError(
            f"feature_matrix must be (n, {len(cfg.features)}); got {x.shape}"
        )
    n_samples = x.shape[0]
    # k cannot exceed the number of distinct-ish samples
    k = min(cfg.k, max(2, n_samples))

    steps = []
    if cfg.standardize:
        steps.append(("scale", StandardScaler()))
    steps.append((
        "kmeans",
        KMeans(n_clusters=k, random_state=cfg.seed, n_init=cfg.n_init, max_iter=cfg.max_iter),
    ))
    pipeline = Pipeline(steps)
    pipeline.fit(x)

    km: object = pipeline.named_steps["kmeans"]
    labels = km.labels_
    params = {
        "k": k,
        "requested_k": cfg.k,
        "seed": cfg.seed,
        "n_init": cfg.n_init,
        "max_iter": cfg.max_iter,
        "features": list(cfg.features),
        "standardize": cfg.standardize,
        "n_samples": n_samples,
        "catalog_row_count": row_count,
        "gate": gate,
    }
    metrics = {"inertia": float(km.inertia_)}
    try:
        from sklearn.metrics import silhouette_score

        if 2 <= k < n_samples:
            x_for_sil = pipeline.named_steps["scale"].transform(x) if cfg.standardize else x
            metrics["silhouette"] = float(silhouette_score(x_for_sil, labels))
    except Exception as exc:  # silhouette is best-effort
        logger.warning("silhouette_score failed: %s", exc)

    model_version = _make_model_version(now_iso, provisional)
    logger.info(
        "train_kmeans: fitted k=%d on %d samples (gate=%s, model_version=%s); "
        "inertia=%.3f silhouette=%s",
        k, n_samples, gate, model_version, metrics["inertia"],
        metrics.get("silhouette"),
    )
    return TrainedKMeans(
        model_version=model_version,
        provisional=provisional,
        config=cfg,
        n_samples=n_samples,
        params=params,
        metrics=metrics,
        _pipeline=pipeline,
    )
