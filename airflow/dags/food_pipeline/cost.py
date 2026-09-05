"""Spoonacular point-cost model.

VERIFIED 2026-09-05 against real ``X-API-Quota-Request`` response headers
(see docs/spoonacular-quota.md). Three calls, ``number`` = 1 / 10 / 30, with
``addRecipeNutrition=true`` and no ``fillIngredients``:

    number  ->  points charged
      1     ->  1.06
      10    ->  1.60
      30    ->  2.80

Least-squares fit is exact:  cost(n) = 1.000 + 0.060 * n

This is the number to budget with. It is HIGHER than the additive figure the
public docs imply (~ 1 + 0.035 * n), so trust the measurement. Re-measure and
update this module (and the skill) if Spoonacular changes pricing.
"""

from __future__ import annotations

# cost(n) = SEARCH_BASE_POINTS + SEARCH_PER_RECIPE_POINTS * n
SEARCH_BASE_POINTS = 1.0
SEARCH_PER_RECIPE_POINTS = 0.06


def estimate_search_cost(number: int) -> float:
    """Estimated point cost of one ``complexSearch`` call returning ``number``
    recipes with nutrition attached (no ``fillIngredients``).

    Used to decide *before* a call whether it fits the remaining daily budget.
    The actual charge is read back from the ``X-API-Quota-Request`` header and
    used for the real accounting.
    """
    if number < 0:
        raise ValueError(f"number must be >= 0, got {number}")
    return SEARCH_BASE_POINTS + SEARCH_PER_RECIPE_POINTS * number
