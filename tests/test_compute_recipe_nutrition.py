"""compute_recipe_nutrition: known-input hand check, per-stage skips recorded,
ratio formula shared with features.py, servings handling, nutrition_source."""

import pytest

from food_pipeline.compute_recipe_nutrition import (
    NUTRITION_SOURCE_USDA,
    compute_recipe_nutrition,
)
from food_pipeline.themealdb import IngredientLine, ParsedMeal
from food_pipeline.usda_client import UsdaFood


def meal(*lines: tuple[str, str]) -> ParsedMeal:
    return ParsedMeal(
        meal_id="52772",
        name="Test Meal",
        category="Chicken",
        area="Japanese",
        instructions="...",
        ingredients=tuple(
            IngredientLine(name=n, quantity_text=q, slot=i + 1)
            for i, (n, q) in enumerate(lines)
        ),
    )


CHICKEN = UsdaFood(1, "Chicken, breast, raw", "Foundation",
                   calories_per_100g=120.0, protein_g_per_100g=22.5,
                   carbs_g_per_100g=0.0, fat_g_per_100g=2.6)
OIL = UsdaFood(2, "Olive oil", "SR Legacy",
               calories_per_100g=884.0, protein_g_per_100g=0.0,
               carbs_g_per_100g=0.0, fat_g_per_100g=100.0)


def search_map(mapping):
    def _search(name: str):
        return mapping.get(name, [])
    return _search


def test_hand_computed_totals_no_servings():
    m = meal(("chicken breast", "200 g"), ("olive oil", "1 tbsp"))
    r = compute_recipe_nutrition(
        m, search_map({"chicken breast": [CHICKEN], "olive oil": [OIL]})
    )
    # 200 g chicken -> factor 2.0 ; 1 tbsp oil = 14.78676478125 g -> factor 0.1478676...
    f_oil = 14.78676478125 / 100.0
    exp_cal = 120 * 2.0 + 884 * f_oil
    exp_pro = 22.5 * 2.0 + 0.0
    exp_fat = 2.6 * 2.0 + 100.0 * f_oil
    assert r.total_calories == pytest.approx(exp_cal)
    assert r.total_protein_g == pytest.approx(exp_pro)
    assert r.total_carbs_g == pytest.approx(0.0)
    assert r.total_fat_g == pytest.approx(exp_fat)

    # ratios via the same formula as features.py
    assert r.pct_calories_from_protein == pytest.approx(exp_pro * 4 / exp_cal)
    assert r.pct_calories_from_carbs == pytest.approx(0.0)
    assert r.pct_calories_from_fat == pytest.approx(exp_fat * 9 / exp_cal)

    assert r.nutrition_source == NUTRITION_SOURCE_USDA
    assert r.calories_per_serving is None
    assert r.complete is False
    assert any("no servings" in n for n in r.notes)
    assert r.n_used == 2 and r.n_skipped == 0


def test_servings_gives_calories_per_serving_and_complete():
    m = meal(("chicken breast", "200 g"), ("olive oil", "1 tbsp"))
    r = compute_recipe_nutrition(
        m, search_map({"chicken breast": [CHICKEN], "olive oil": [OIL]}), servings=2
    )
    assert r.calories_per_serving == pytest.approx(r.total_calories / 2)
    assert r.complete is True
    # ratios identical to the no-servings case (scale-invariant)
    r0 = compute_recipe_nutrition(
        m, search_map({"chicken breast": [CHICKEN], "olive oil": [OIL]})
    )
    assert r.pct_calories_from_protein == pytest.approx(r0.pct_calories_from_protein)
    assert r.pct_calories_from_fat == pytest.approx(r0.pct_calories_from_fat)


def test_skips_recorded_per_stage():
    m = meal(
        ("chicken breast", "200 g"),        # ok
        ("salt", "to taste"),               # unit_conversion skip
        ("unicorn meat", "100 g"),          # usda_search skip (no results)
        ("water", "1 cup"),                 # ingredient_match skip (bad candidate)
    )
    r = compute_recipe_nutrition(
        m,
        search_map({
            "chicken breast": [CHICKEN],
            "unicorn meat": [],
            "water": [UsdaFood(9, "Cola, carbonated", "Branded",
                               calories_per_100g=41.0, protein_g_per_100g=0.0,
                               carbs_g_per_100g=10.6, fat_g_per_100g=0.0)],
        }),
    )
    stages = {s.name: s.stage for s in r.skipped}
    assert stages == {
        "salt": "unit_conversion",
        "unicorn meat": "usda_search",
        "water": "ingredient_match",
    }
    assert r.n_used == 1
    assert any("skipped" in n for n in r.notes)


def test_no_ingredients_resolved_returns_none_nutrition():
    m = meal(("salt", "to taste"), ("pepper", "a pinch"))
    r = compute_recipe_nutrition(m, search_map({}))
    assert r.total_calories is None
    assert r.pct_calories_from_protein is None
    assert r.complete is False
    assert r.n_used == 0 and r.n_skipped == 2
    assert any("no ingredients resolved" in n for n in r.notes)


def test_usda_search_exception_skips_one_ingredient_not_recipe(caplog):
    def boom(name):
        if name == "chicken breast":
            return [CHICKEN]
        raise RuntimeError("network down")

    m = meal(("chicken breast", "200 g"), ("olive oil", "1 tbsp"))
    with caplog.at_level("WARNING"):
        r = compute_recipe_nutrition(m, boom)
    assert r.n_used == 1
    assert [s.stage for s in r.skipped] == ["usda_search"]
    assert "network down" in caplog.text


def test_used_contributions_carry_confidence_and_fdc_id():
    m = meal(("chicken breast", "200 g"))
    r = compute_recipe_nutrition(m, search_map({"chicken breast": [CHICKEN]}))
    c = r.used[0]
    assert c.fdc_id == 1
    assert 0.0 <= c.confidence <= 1.0
    assert c.calories == pytest.approx(240.0)
