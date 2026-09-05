"""Layer B K-Means: the minimum-catalog-size gate + a real small fit."""

import numpy as np
import pytest

from food_pipeline.clustering import (
    GATE_PROVISIONAL,
    GATE_SKIP,
    GATE_STABLE,
    MIN_CATALOG_SIZE,
    STABLE_CATALOG_SIZE,
    KMeansConfig,
    catalog_size_gate,
    train_kmeans,
)


@pytest.mark.parametrize(
    ("count", "gate"),
    [
        (0, GATE_SKIP),
        (149, GATE_SKIP),
        (150, GATE_PROVISIONAL),
        (499, GATE_PROVISIONAL),
        (500, GATE_STABLE),
        (5000, GATE_STABLE),
    ],
)
def test_catalog_size_gate(count, gate):
    assert catalog_size_gate(count) == gate


def test_gate_boundaries_match_constants():
    assert catalog_size_gate(MIN_CATALOG_SIZE - 1) == GATE_SKIP
    assert catalog_size_gate(MIN_CATALOG_SIZE) == GATE_PROVISIONAL
    assert catalog_size_gate(STABLE_CATALOG_SIZE) == GATE_STABLE


def _synthetic_matrix(n: int, seed: int = 0) -> list[list[float]]:
    rng = np.random.default_rng(seed)
    # three blobs in (pct_p, pct_c, pct_f, cps) space
    blobs = [
        (0.30, 0.45, 0.25, 450),
        (0.15, 0.60, 0.25, 300),
        (0.35, 0.10, 0.55, 600),
    ]
    rows = []
    for i in range(n):
        bp, bc, bf, cps = blobs[i % 3]
        rows.append([
            bp + rng.normal(0, 0.02),
            bc + rng.normal(0, 0.02),
            bf + rng.normal(0, 0.02),
            cps + rng.normal(0, 20),
        ])
    return rows


def test_train_rejects_below_minimum():
    with pytest.raises(ValueError, match="minimum 150"):
        train_kmeans(_synthetic_matrix(10), row_count=10, now_iso="2026-09-06T00:00:00+00:00")


def test_train_provisional_range_marks_model_version():
    m = _synthetic_matrix(200)
    trained = train_kmeans(m, row_count=200, now_iso="2026-09-06T12:00:00+00:00")
    assert trained.provisional is True
    assert trained.model_version.endswith("-provisional")
    assert trained.params["gate"] == GATE_PROVISIONAL
    assert trained.params["catalog_row_count"] == 200
    assert "inertia" in trained.metrics


def test_train_stable_range_not_provisional():
    m = _synthetic_matrix(600)
    trained = train_kmeans(m, row_count=600, now_iso="2026-09-06T12:00:00+00:00")
    assert trained.provisional is False
    assert not trained.model_version.endswith("-provisional")
    assert trained.params["gate"] == GATE_STABLE


def test_train_and_predict_roundtrip():
    m = _synthetic_matrix(300, seed=1)
    trained = train_kmeans(m, row_count=300, now_iso="2026-09-06T00:00:00+00:00")
    labels = trained.predict(m[:10])
    assert len(labels) == 10
    assert all(isinstance(x, int) for x in labels)
    assert trained.predict([]) == []


def test_train_uses_configured_seed_features():
    cfg = KMeansConfig(k=4, seed=7)
    a = train_kmeans(_synthetic_matrix(300), row_count=300, now_iso="2026-09-06T00:00:00+00:00", config=cfg)
    b = train_kmeans(_synthetic_matrix(300), row_count=300, now_iso="2026-09-06T00:00:00+00:00", config=cfg)
    assert a.params["seed"] == 7
    assert a.params["k"] == 4
    # deterministic: same data + seed -> same assignment on a sample
    assert a.predict(_synthetic_matrix(20)) == b.predict(_synthetic_matrix(20))


def test_wrong_feature_width_rejected():
    with pytest.raises(ValueError, match="feature_matrix must be"):
        train_kmeans([[1.0, 2.0, 3.0]] * 200, row_count=200, now_iso="2026-09-06T00:00:00+00:00")
