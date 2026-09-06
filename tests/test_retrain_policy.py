"""Phase 7 — retrain trigger + cluster-quality guard (pure logic)."""

import pytest

from food_pipeline.retrain_policy import (
    CLUSTER_QUALITY_SILHOUETTE_DROP,
    RETRAIN_MIN_GROWTH_FRACTION,
    check_cluster_quality,
    evaluate_retrain,
)


# --- retrain trigger ---------------------------------------------------
def test_no_previous_run_always_trains():
    d = evaluate_retrain(335, None)
    assert d.should_retrain is True
    assert "no previous training run" in d.reason
    assert d.growth_fraction is None


def test_growth_below_threshold_skips():
    d = evaluate_retrain(360, 335)  # +7.5%
    assert d.should_retrain is False
    assert d.growth_fraction == pytest.approx((360 - 335) / 335)
    assert "grew only 7.5%" in d.reason
    assert "threshold 20%" in d.reason and "skipping retrain" in d.reason


def test_growth_at_or_above_threshold_trains():
    d = evaluate_retrain(402, 335)  # exactly +20.0%
    assert d.should_retrain is True
    assert d.growth_fraction == pytest.approx(0.20)
    assert "retraining" in d.reason

    d2 = evaluate_retrain(500, 335)  # +49%
    assert d2.should_retrain is True


def test_threshold_is_configurable():
    # same growth, stricter threshold -> skip; looser -> train
    assert evaluate_retrain(360, 335, min_growth_fraction=0.10).should_retrain is False
    assert evaluate_retrain(360, 335, min_growth_fraction=0.05).should_retrain is True


def test_default_threshold_is_20_percent():
    assert RETRAIN_MIN_GROWTH_FRACTION == 0.20


def test_shrinking_catalog_skips():
    d = evaluate_retrain(300, 335)  # negative growth
    assert d.should_retrain is False


# --- cluster-quality check ------------------------------------------
def test_silhouette_drop_beyond_threshold_flags_degraded():
    qc = check_cluster_quality(0.24, 0.30)  # dropped 0.06 > 0.05
    assert qc.degraded is True
    assert "cluster quality may have degraded" in qc.message
    assert "0.3000 -> 0.2400" in qc.message
    assert "No automatic fix" in qc.message


def test_small_silhouette_drop_is_ok():
    qc = check_cluster_quality(0.27, 0.30)  # dropped 0.03 < 0.05
    assert qc.degraded is False


def test_silhouette_improvement_is_ok():
    assert check_cluster_quality(0.34, 0.30).degraded is False


def test_missing_silhouette_skips_check():
    assert check_cluster_quality(None, 0.30).degraded is False
    assert check_cluster_quality(0.30, None).degraded is False
    assert "skipped quality check" in check_cluster_quality(0.3, None).message


def test_quality_threshold_is_configurable():
    assert check_cluster_quality(0.26, 0.30, drop_threshold=0.02).degraded is True
    assert check_cluster_quality(0.26, 0.30, drop_threshold=0.10).degraded is False


def test_default_quality_threshold():
    assert CLUSTER_QUALITY_SILHOUETTE_DROP == 0.05
