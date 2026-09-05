"""The point-cost model must match what the real API charged (2026-09-05)."""

import pytest

from food_pipeline.cost import estimate_search_cost


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (1, 1.06),
        (10, 1.60),
        (30, 2.80),
        (100, 7.00),
        (0, 1.00),
    ],
)
def test_matches_measured_charges(number, expected):
    assert estimate_search_cost(number) == pytest.approx(expected)


def test_linear_in_number():
    step = estimate_search_cost(21) - estimate_search_cost(20)
    assert step == pytest.approx(0.06)


def test_negative_number_rejected():
    with pytest.raises(ValueError):
        estimate_search_cost(-1)
