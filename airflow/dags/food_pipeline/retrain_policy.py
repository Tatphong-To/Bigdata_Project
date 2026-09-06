"""Phase 7 — retrain trigger + cluster-quality guard.

These are ADDITIONS that sit *alongside* the Phase 3 minimum-catalog-size gate
(150 / 500 in ``clustering.py``), not a replacement. Order inside
``train_or_update_kmeans``:

    1. minimum-catalog-size gate  (Phase 3)  — < 150 rows -> skip
    2. retrain trigger            (this)     — catalog grew < 20% since the
                                               last logged train -> skip
    3. fit K-Means
    4. cluster-quality check      (this)     — silhouette dropped vs the last
                                               same-tier run -> WARNING (no fix)

Why 20%: the catalog grows slowly — Spoonacular's free tier is ~50 points/day,
so a manually-triggered daily run adds on the order of a couple of hundred
recipes at most, often far fewer once the common queries are exhausted.
Re-fitting K-Means for 2-3 new rows is not worth the compute or the churn in
``cluster_id`` (which the Model Service reads on every request). 20% growth is
the default point where a re-fit can plausibly move cluster boundaries enough
to matter. It is a constant here, tune-able, not a hard architectural rule.
"""

from __future__ import annotations

from dataclasses import dataclass

# catalog must grow by at least this fraction since the last logged train
RETRAIN_MIN_GROWTH_FRACTION = 0.20

# silhouette drop (absolute) vs. the previous same-tier run that trips a WARNING
CLUSTER_QUALITY_SILHOUETTE_DROP = 0.05


@dataclass(frozen=True)
class RetrainDecision:
    should_retrain: bool
    reason: str
    growth_fraction: float | None  # None when there is no prior run to compare


def evaluate_retrain(
    current_catalog_rows: int,
    last_trained_catalog_rows: int | None,
    *,
    min_growth_fraction: float = RETRAIN_MIN_GROWTH_FRACTION,
) -> RetrainDecision:
    """Decide whether to re-fit given how much the catalog has grown."""
    if last_trained_catalog_rows is None or last_trained_catalog_rows <= 0:
        return RetrainDecision(
            True,
            "no previous training run on record — training",
            None,
        )
    growth = (current_catalog_rows - last_trained_catalog_rows) / last_trained_catalog_rows
    if growth < min_growth_fraction:
        return RetrainDecision(
            False,
            (
                f"catalog grew only {growth * 100:.1f}% since last train "
                f"({last_trained_catalog_rows} -> {current_catalog_rows} rows; "
                f"threshold {min_growth_fraction * 100:.0f}%), skipping retrain "
                f"this run"
            ),
            growth,
        )
    return RetrainDecision(
        True,
        (
            f"catalog grew {growth * 100:.1f}% since last train "
            f"({last_trained_catalog_rows} -> {current_catalog_rows} rows), "
            f"retraining"
        ),
        growth,
    )


@dataclass(frozen=True)
class QualityCheck:
    degraded: bool
    message: str


def check_cluster_quality(
    current_silhouette: float | None,
    previous_silhouette: float | None,
    *,
    drop_threshold: float = CLUSTER_QUALITY_SILHOUETTE_DROP,
) -> QualityCheck:
    """Compare this run's silhouette with the previous same-tier run. Returns
    ``degraded=True`` with a message to log at WARNING when it fell by more
    than ``drop_threshold``. Never fixes anything — advisory only."""
    if current_silhouette is None or previous_silhouette is None:
        return QualityCheck(False, "no comparable previous silhouette — skipped quality check")
    delta = current_silhouette - previous_silhouette
    if delta < -drop_threshold:
        return QualityCheck(
            True,
            (
                f"cluster quality may have degraded: silhouette "
                f"{previous_silhouette:.4f} -> {current_silhouette:.4f} "
                f"(down {(-delta):.4f}, threshold {drop_threshold:.2f}). "
                f"Possible causes: catalog composition changed (e.g. a new "
                f"source mix), or k is no longer well suited to the larger "
                f"catalog. No automatic fix — review k / features."
            ),
        )
    return QualityCheck(
        False,
        f"silhouette {previous_silhouette:.4f} -> {current_silhouette:.4f} (delta {delta:+.4f}) — ok",
    )
