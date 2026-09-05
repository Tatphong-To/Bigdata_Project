"""Layer B feature engineering: the four formulas (hand-checked), the
divide-by-zero / missing-value drop policy, and the guarantee that no
Spoonacular diet/intolerance tag reaches the K-Means feature set."""

import pytest

from food_pipeline.features import (
    FEATURE_COLUMNS,
    DroppedRow,
    assert_feature_row_clean,
    build_feature_rows,
    build_features_from_staging_file,
    compute_feature_row,
    feature_matrix,
)
from food_pipeline.parse import parse_recipe


def staging_row(**overrides):
    row = {
        "menu_id": "1",
        "source": "spoonacular",
        "name": "Test Dish",
        "servings": 4,
        "calories": 400.0,
        "protein_g": 20.0,
        "carbs_g": 40.0,
        "fat_g": 10.0,
        "ingredients": ["chicken", "rice"],
        "diet_tags": ["vegan", "gluten free"],
        "raw_payload": {"id": 1, "diets": ["vegan"]},
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# The four formulas, hand-computed
# --------------------------------------------------------------------------
def test_all_four_formulas_hand_checked():
    # cal=400, p=20g, c=40g, f=10g
    #   protein kcal = 20*4 = 80   -> 80/400  = 0.20
    #   carb    kcal = 40*4 = 160  -> 160/400 = 0.40
    #   fat     kcal = 10*9 = 90   -> 90/400  = 0.225
    #   calories_per_serving = 400
    fr = compute_feature_row(staging_row())
    assert fr["pct_calories_from_protein"] == pytest.approx(0.20)
    assert fr["pct_calories_from_carbs"] == pytest.approx(0.40)
    assert fr["pct_calories_from_fat"] == pytest.approx(0.225)
    assert fr["calories_per_serving"] == pytest.approx(400.0)


def test_calories_per_serving_is_total_calories_not_divided_by_servings():
    # same macro ratios, half the calories, different servings value
    fr = compute_feature_row(
        staging_row(calories=200.0, protein_g=10.0, carbs_g=20.0, fat_g=5.0, servings=8)
    )
    assert fr["pct_calories_from_protein"] == pytest.approx(0.20)
    assert fr["pct_calories_from_carbs"] == pytest.approx(0.40)
    assert fr["pct_calories_from_fat"] == pytest.approx(0.225)
    assert fr["calories_per_serving"] == pytest.approx(200.0)  # NOT 200/8


@pytest.mark.parametrize(
    ("field", "grams", "kcal_per_g", "column"),
    [
        ("protein_g", 30.0, 4, "pct_calories_from_protein"),
        ("carbs_g", 55.0, 4, "pct_calories_from_carbs"),
        ("fat_g", 12.0, 9, "pct_calories_from_fat"),
    ],
)
def test_each_macro_uses_the_right_atwater_factor(field, grams, kcal_per_g, column):
    row = staging_row(calories=1000.0, protein_g=0.0, carbs_g=0.0, fat_g=0.0)
    row[field] = grams
    fr = compute_feature_row(row)
    assert fr[column] == pytest.approx(grams * kcal_per_g / 1000.0)


def test_real_recipe_matches_hand_computation(spoonacular_search_payload):
    row = parse_recipe(spoonacular_search_payload["results"][0])
    fr = compute_feature_row(row)
    # cal=478.31, p=37.1, c=15.21, f=29.24
    assert fr["pct_calories_from_protein"] == pytest.approx(37.1 * 4 / 478.31)
    assert fr["pct_calories_from_carbs"] == pytest.approx(15.21 * 4 / 478.31)
    assert fr["pct_calories_from_fat"] == pytest.approx(29.24 * 9 / 478.31)
    assert fr["calories_per_serving"] == pytest.approx(478.31)
    # sanity: the three shares roughly sum to ~1 for a real recipe
    total = (
        fr["pct_calories_from_protein"]
        + fr["pct_calories_from_carbs"]
        + fr["pct_calories_from_fat"]
    )
    assert total == pytest.approx(0.988, abs=0.01)


# --------------------------------------------------------------------------
# divide-by-zero / missing-value policy: drop, never impute
# --------------------------------------------------------------------------
def test_zero_calories_dropped_not_imputed():
    result = compute_feature_row(staging_row(calories=0))
    assert isinstance(result, str)
    assert "<= 0" in result


def test_negative_calories_dropped():
    assert "<= 0" in compute_feature_row(staging_row(calories=-10.0))


def test_missing_calories_dropped():
    assert compute_feature_row(staging_row(calories=None)) == "calories is missing"


def test_non_finite_calories_dropped():
    assert "finite" in compute_feature_row(staging_row(calories=float("nan")))
    assert "finite" in compute_feature_row(staging_row(calories=float("inf")))


@pytest.mark.parametrize("field", ["protein_g", "carbs_g", "fat_g"])
def test_missing_macro_dropped(field):
    assert compute_feature_row(staging_row(**{field: None})) == f"{field} is missing"


@pytest.mark.parametrize("field", ["protein_g", "carbs_g", "fat_g"])
def test_negative_macro_dropped(field):
    assert "negative" in compute_feature_row(staging_row(**{field: -1.0}))


def test_build_feature_rows_splits_and_logs(caplog):
    rows = [
        staging_row(menu_id="ok"),
        staging_row(menu_id="zerocal", calories=0),
        staging_row(menu_id="nomacro", fat_g=None),
    ]
    with caplog.at_level("WARNING"):
        features, dropped = build_feature_rows(rows)
    assert [f["menu_id"] for f in features] == ["ok"]
    assert {d.menu_id for d in dropped} == {"zerocal", "nomacro"}
    assert all(isinstance(d, DroppedRow) and d.reason for d in dropped)
    assert "dropped menu_id=zerocal" in caplog.text


# --------------------------------------------------------------------------
# no source diet / intolerance tag may reach K-Means
# --------------------------------------------------------------------------
def test_feature_row_contains_only_join_key_and_four_features():
    fr = compute_feature_row(staging_row())  # input HAS diet_tags, ingredients, raw_payload
    assert set(fr) == {"menu_id", *FEATURE_COLUMNS}
    assert "diet_tags" not in fr
    assert "ingredients" not in fr
    assert "raw_payload" not in fr


def test_assert_feature_row_clean_rejects_injected_tag():
    fr = compute_feature_row(staging_row())
    fr["diet_tags"] = ["vegan"]
    with pytest.raises(ValueError, match="disallowed keys|source label"):
        assert_feature_row_clean(fr)


def test_assert_feature_row_clean_rejects_intolerances_key():
    bad = {"menu_id": "1", **{c: 0.1 for c in FEATURE_COLUMNS}, "intolerances": ["dairy"]}
    with pytest.raises(ValueError):
        assert_feature_row_clean(bad)


def test_feature_matrix_has_four_columns_in_order_no_menu_id():
    rows = [staging_row(menu_id="a"), staging_row(menu_id="b", calories=500.0)]
    features, _ = build_feature_rows(rows)
    matrix = feature_matrix(features)
    assert len(matrix) == 2
    assert all(len(r) == 4 for r in matrix)
    # column order == FEATURE_COLUMNS
    assert matrix[0][3] == pytest.approx(400.0)   # calories_per_serving is last
    assert matrix[1][3] == pytest.approx(500.0)


def test_build_features_from_staging_file(tmp_path):
    import json

    staging = {
        "fetched_at": "x",
        "accepted": [staging_row(menu_id="1"), staging_row(menu_id="2", calories=0)],
        "rejected": [],
    }
    p = tmp_path / "staging.json"
    p.write_text(json.dumps(staging), encoding="utf-8")
    features, dropped = build_features_from_staging_file(p)
    assert [f["menu_id"] for f in features] == ["1"]
    assert [d.menu_id for d in dropped] == ["2"]
