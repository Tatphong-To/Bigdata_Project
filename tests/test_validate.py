"""Validator: reject missing / physically-impossible nutrition, with reasons."""

import pytest

from food_pipeline.parse import parse_recipe
from food_pipeline.validate import validate_batch, validate_row


def good_row(**overrides):
    row = {
        "menu_id": "1",
        "name": "Test Dish",
        "servings": 4,
        "calories": 478.31,
        "protein_g": 37.1,
        "carbs_g": 15.21,
        "fat_g": 29.24,
    }
    row.update(overrides)
    return row


def test_real_recipe_passes(spoonacular_search_payload):
    row = parse_recipe(spoonacular_search_payload["results"][0])
    assert validate_row(row) == []


def test_negative_calories_rejected():
    reasons = validate_row(good_row(calories=-5))
    assert any("negative" in r and "calories" in r for r in reasons)


def test_negative_macro_rejected():
    assert any("protein_g" in r and "negative" in r for r in validate_row(good_row(protein_g=-1)))


@pytest.mark.parametrize("field", ["calories", "protein_g", "carbs_g", "fat_g"])
def test_missing_required_numeric_rejected(field):
    reasons = validate_row(good_row(**{field: None}))
    assert any(f"missing {field}" == r for r in reasons)


def test_nan_rejected():
    assert any("finite" in r for r in validate_row(good_row(calories=float("nan"))))


def test_zero_calories_rejected():
    assert "calories is zero" in validate_row(good_row(calories=0))


def test_zero_or_negative_servings_rejected():
    assert any("servings" in r for r in validate_row(good_row(servings=0)))
    assert any("servings" in r for r in validate_row(good_row(servings=-2)))


def test_missing_servings_is_allowed():
    assert validate_row(good_row(servings=None)) == []


def test_missing_name_and_id_rejected():
    reasons = validate_row(good_row(name="  ", menu_id=None))
    assert "missing name" in reasons
    assert "missing menu_id" in reasons


def test_macro_calories_far_above_stated_rejected():
    # per-recipe macros left on a per-serving calorie figure: ~4x mismatch
    reasons = validate_row(good_row(calories=120, protein_g=37, carbs_g=15, fat_g=29))
    assert any("macro-derived calories" in r for r in reasons)


def test_small_macro_calorie_overshoot_tolerated():
    # rounding / fibre can push macro kcal slightly over stated; allow it
    assert validate_row(good_row(calories=470, protein_g=37.1, carbs_g=15.21, fat_g=29.24)) == []


def test_implausibly_high_calories_rejected():
    assert any("implausibly high" in r for r in validate_row(good_row(calories=8000, protein_g=1, carbs_g=1, fat_g=1)))


def test_boolean_is_not_accepted_as_numeric():
    assert any("not numeric" in r for r in validate_row(good_row(calories=True)))


def test_validate_batch_splits_and_reports(caplog):
    rows = [
        good_row(menu_id="1"),
        good_row(menu_id="2", calories=-1),
        good_row(menu_id="3", protein_g=None),
    ]
    with caplog.at_level("WARNING"):
        accepted, rejected = validate_batch(rows)
    assert [r["menu_id"] for r in accepted] == ["1"]
    assert {r.menu_id for r in rejected} == {"2", "3"}
    assert all(r.reasons for r in rejected)
    assert "rejected menu_id=2" in caplog.text
