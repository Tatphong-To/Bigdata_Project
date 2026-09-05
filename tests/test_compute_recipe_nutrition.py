"""compute_recipe_nutrition: known-input hand check, per-stage skips recorded,
ratio formula shared with features.py, servings handling, nutrition_source."""

import pytest

from food_pipeline.compute_recipe_nutrition import (
    NUTRITION_SOURCE_USDA,
    CompletenessConfig,
    compute_recipe_nutrition,
    log_completeness_summary,
    summarize_completeness,
)
from food_pipeline.themealdb import IngredientLine, ParsedMeal
from food_pipeline.usda_client import UsdaFood

# don't let the completeness guard interfere with tests that check other things
NO_GUARD = CompletenessConfig(min_completeness=0.0)


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
        completeness_config=NO_GUARD,
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
        r = compute_recipe_nutrition(m, boom, completeness_config=NO_GUARD)
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


# --------------------------------------------------------------------------
# recipe-level completeness guard
# --------------------------------------------------------------------------
RICE = UsdaFood(3, "Rice, cooked", "SR Legacy",
                calories_per_100g=130.0, protein_g_per_100g=2.7,
                carbs_g_per_100g=28.0, fat_g_per_100g=0.3)
BROWN_RICE = UsdaFood(5, "Rice, brown, cooked", "SR Legacy",
                      calories_per_100g=123.0, protein_g_per_100g=2.7,
                      carbs_g_per_100g=25.6, fat_g_per_100g=1.0)
SOY = UsdaFood(4, "Soy sauce", "SR Legacy",
               calories_per_100g=53.0, protein_g_per_100g=8.1,
               carbs_g_per_100g=4.9, fat_g_per_100g=0.6)


def test_high_completeness_recipe_passes():
    # every ingredient resolves -> completeness 1.0, not dropped
    m = meal(("chicken breast", "300 g"), ("rice", "200 g"), ("olive oil", "1 tbsp"))
    r = compute_recipe_nutrition(
        m,
        search_map({"chicken breast": [CHICKEN], "rice": [RICE], "olive oil": [OIL]}),
        servings=2,
    )
    assert r.matched_ingredient_count == 3 and r.total_ingredient_count == 3
    assert r.count_completeness == pytest.approx(1.0)
    assert r.calorie_completeness == pytest.approx(1.0)
    assert r.completeness == pytest.approx(1.0)
    assert r.dropped_for_completeness is False
    assert r.complete is True
    assert r.pct_calories_from_protein is not None


def test_teriyaki_like_recipe_dropped_when_main_ingredient_rejected(caplog):
    # the Teriyaki case: the energy/protein anchor (chicken) can't be
    # quantified ("2"), leaving only rice + soy -> ratios would be biased.
    m = meal(
        ("chicken breasts", "2"),              # count-based -> unit_conversion skip
        ("stir-fry vegetables", "1 (12 oz.)"),  # unparseable -> unit_conversion skip
        ("brown rice", "2 cup"),
        ("soy sauce", "0.5 cup"),
    )
    with caplog.at_level("WARNING"):
        r = compute_recipe_nutrition(
            m,
            search_map({"brown rice": [BROWN_RICE], "soy sauce": [SOY]}),
            servings=4,
        )
    assert r.matched_ingredient_count == 2 and r.total_ingredient_count == 4
    assert r.completeness < 0.70
    assert r.dropped_for_completeness is True
    assert r.complete is False
    # clustering features withheld — dropped, not guessed
    assert r.pct_calories_from_protein is None
    assert r.pct_calories_from_carbs is None
    assert r.pct_calories_from_fat is None
    assert r.calories_per_serving is None
    # totals kept for diagnostics
    assert r.total_calories is not None
    assert any("DROPPED for low completeness" in n for n in r.notes)
    # log names the recipe, the score, and the rejected ingredients
    assert "DROPPED recipe id=52772" in caplog.text
    assert "chicken breasts" in caplog.text and "stir-fry vegetables" in caplog.text


def test_completeness_threshold_is_config_not_hardcoded():
    m = meal(
        ("chicken breasts", "2"),
        ("brown rice", "2 cup"),
        ("soy sauce", "0.5 cup"),
    )
    sm = search_map({"brown rice": [BROWN_RICE], "soy sauce": [SOY]})
    # lenient threshold -> kept
    lenient = compute_recipe_nutrition(
        m, sm, servings=4, completeness_config=CompletenessConfig(min_completeness=0.1)
    )
    assert lenient.dropped_for_completeness is False
    assert lenient.pct_calories_from_protein is not None
    # strict threshold -> dropped
    strict = compute_recipe_nutrition(
        m, sm, servings=4, completeness_config=CompletenessConfig(min_completeness=0.95)
    )
    assert strict.dropped_for_completeness is True
    assert strict.pct_calories_from_protein is None


def test_completeness_basis_count_vs_calorie():
    # 1 tiny-calorie ingredient skipped, rest fine: count basis penalises,
    # calorie basis barely notices.
    m = meal(
        ("chicken breast", "300 g"),
        ("rice", "300 g"),
        ("parsley", "1 sprig"),  # unit_conversion skip, non-mass
    )
    sm = search_map({"chicken breast": [CHICKEN], "rice": [RICE]})
    by_count = compute_recipe_nutrition(
        m, sm, completeness_config=CompletenessConfig(basis="count", min_completeness=0.0)
    )
    by_cal = compute_recipe_nutrition(
        m, sm, completeness_config=CompletenessConfig(basis="calorie", min_completeness=0.0)
    )
    assert by_count.completeness == pytest.approx(2 / 3)
    assert by_cal.completeness > by_count.completeness  # calorie basis is more forgiving here
    # "min" basis takes the stricter of the two
    by_min = compute_recipe_nutrition(
        m, sm, completeness_config=CompletenessConfig(basis="min", min_completeness=0.0)
    )
    assert by_min.completeness == pytest.approx(min(by_count.completeness, by_cal.completeness))


def test_non_quantitative_skips_do_not_inflate_missing_calories():
    m = meal(("chicken breast", "300 g"), ("salt", "to taste"), ("pepper", "a pinch"))
    r = compute_recipe_nutrition(
        m, search_map({"chicken breast": [CHICKEN]}),
        completeness_config=CompletenessConfig(basis="calorie", min_completeness=0.0),
    )
    # pinch/to taste contribute 0 estimated missing calories
    assert r.calorie_completeness == pytest.approx(1.0)
    assert r.count_completeness == pytest.approx(1 / 3)


def test_batch_completeness_summary(caplog):
    good = meal(("chicken breast", "300 g"), ("rice", "200 g"))
    bad = meal(("chicken breasts", "2"), ("brown rice", "2 cup"), ("soy sauce", "0.5 cup"))
    empty = meal(("salt", "to taste"))
    sm = search_map({
        "chicken breast": [CHICKEN], "rice": [RICE],
        "brown rice": [BROWN_RICE], "soy sauce": [SOY],
    })
    results = [
        compute_recipe_nutrition(good, sm, servings=2),
        compute_recipe_nutrition(bad, sm, servings=4),
        compute_recipe_nutrition(empty, sm),
    ]
    s = summarize_completeness(results)
    assert s.total_recipes == 3
    assert s.dropped_for_completeness == 1
    assert s.dropped_no_ingredients == 1
    assert s.passed == 1
    assert s.total_ingredients == 2 + 3 + 1
    assert s.matched_ingredients == 2 + 2 + 0
    assert 0.0 < s.ingredient_skip_rate < 1.0
    assert s.skipped_by_stage.get("unit_conversion", 0) >= 2

    with caplog.at_level("INFO"):
        log_completeness_summary(results)
    assert "dropped for completeness" in caplog.text
