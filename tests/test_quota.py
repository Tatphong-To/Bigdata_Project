"""QuotaTracker: budget maths + the persistence contract that lets it
survive a restart (state read back from the store, not held in memory)."""

import datetime as dt

import pytest

from food_pipeline.quota import (
    InMemoryQuotaStore,
    QuotaExceeded,
    QuotaTracker,
)

DAY = dt.date(2026, 9, 5)


def make(quota=50.0, margin=0.0, day=DAY, store=None):
    store = store or InMemoryQuotaStore()
    tracker = QuotaTracker(
        store, quota, today=lambda: day, safety_margin_points=margin
    )
    return tracker, store


def test_fresh_day_has_full_budget():
    tracker, _ = make(quota=50.0)
    assert tracker.points_used() == 0.0
    assert tracker.remaining() == 50.0


def test_charge_accumulates_and_persists():
    tracker, store = make(quota=50.0)
    tracker.charge(1.06)
    tracker.charge(1.60)
    assert tracker.points_used() == pytest.approx(2.66)
    assert store.get_usage(DAY) == pytest.approx((2.66, 2))


def test_new_tracker_sees_prior_usage_from_store():
    # Simulates a DAG / container restart: same persistent store, brand-new
    # tracker object. Usage must carry over, NOT reset.
    _, store = make(quota=50.0)
    first, _ = make(store=store)
    first.charge(44.0, requests=3)

    after_restart, _ = make(store=store)
    assert after_restart.points_used() == pytest.approx(44.0)
    assert after_restart.remaining() == pytest.approx(6.0)


def test_can_afford_respects_remaining():
    tracker, _ = make(quota=50.0)
    tracker.charge(48.0)
    assert tracker.can_afford(2.0) is True
    assert tracker.can_afford(2.01) is False


def test_safety_margin_shrinks_usable_budget():
    tracker, _ = make(quota=50.0, margin=1.0)
    assert tracker.remaining() == 49.0
    tracker.charge(49.0)
    assert tracker.remaining() == 0.0
    assert tracker.can_afford(0.5) is False


def test_guard_raises_when_call_would_not_fit():
    tracker, _ = make(quota=50.0)
    tracker.charge(49.5)
    tracker.guard(0.5)  # exactly fits — no raise
    with pytest.raises(QuotaExceeded):
        tracker.guard(0.51)


def test_remaining_never_negative_even_if_api_overcharged():
    tracker, _ = make(quota=50.0)
    tracker.charge(55.0)  # real API served it and charged more than expected
    assert tracker.remaining() == 0.0
    assert tracker.can_afford(0.0) is True
    assert tracker.can_afford(0.01) is False


def test_usage_is_per_day():
    store = InMemoryQuotaStore()
    d1 = QuotaTracker(store, 50.0, today=lambda: dt.date(2026, 9, 5))
    d1.charge(50.0)
    d2 = QuotaTracker(store, 50.0, today=lambda: dt.date(2026, 9, 6))
    assert d2.points_used() == 0.0
    assert d2.remaining() == 50.0


def test_rejects_bad_config_and_inputs():
    store = InMemoryQuotaStore()
    with pytest.raises(ValueError):
        QuotaTracker(store, 0)
    tracker, _ = make()
    with pytest.raises(ValueError):
        tracker.charge(-1)
    with pytest.raises(ValueError):
        tracker.can_afford(-1)
