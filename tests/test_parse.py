"""Parser: exact field mapping against a real Spoonacular response."""

import pytest

from food_pipeline.parse import parse_recipe, parse_search_results


def test_parses_real_recipe(spoonacular_search_payload):
    rows = parse_search_results(spoonacular_search_payload)
    assert len(rows) == 1
    row = rows[0]
    assert row["menu_id"] == "634476"  # string, per the API contract
    assert row["source"] == "spoonacular"
    assert row["name"] == "Bbq Chicken"
    assert row["servings"] == 4
    assert row["calories"] == pytest.approx(478.31)
    assert row["protein_g"] == pytest.approx(37.1)
    assert row["fat_g"] == pytest.approx(29.24)


def test_carbohydrates_is_exact_match_not_net_carbohydrates(spoonacular_search_payload):
    row = parse_recipe(spoonacular_search_payload["results"][0])
    # "Carbohydrates" = 15.21, "Net Carbohydrates" = 15.03 — must pick the former
    assert row["carbs_g"] == pytest.approx(15.21)


def test_ingredient_names_lowercased_from_nutrition_block(spoonacular_search_payload):
    row = parse_recipe(spoonacular_search_payload["results"][0])
    assert row["ingredients"] == [
        "brown sugar",
        "catsup",
        "chicken pieces",
        "dijon mustard",
        "soy sauce",
        "worcestershire sauce",
    ]


def test_diet_tags_from_diets_array_and_booleans(spoonacular_search_payload):
    row = parse_recipe(spoonacular_search_payload["results"][0])
    assert "dairy free" in row["diet_tags"]
    assert "fodmap friendly" in row["diet_tags"]
    assert "low fodmap" in row["diet_tags"]  # from lowFodmap: true
    assert "vegan" not in row["diet_tags"]
    assert "vegetarian" not in row["diet_tags"]


def test_missing_nutrient_becomes_none_not_error():
    raw = {"id": 1, "title": "x", "servings": 2, "nutrition": {"nutrients": []}}
    row = parse_recipe(raw)
    assert row["calories"] is None
    assert row["protein_g"] is None
    assert row["ingredients"] == []
    assert row["diet_tags"] == []


def test_missing_id_yields_none_menu_id():
    row = parse_recipe({"title": "x", "nutrition": {}})
    assert row["menu_id"] is None


def test_search_results_dedupes_by_menu_id():
    payload = {
        "results": [
            {"id": 1, "title": "a", "nutrition": {}},
            {"id": 1, "title": "a again", "nutrition": {}},
            {"id": 2, "title": "b", "nutrition": {}},
        ]
    }
    rows = parse_search_results(payload)
    assert [r["menu_id"] for r in rows] == ["1", "2"]


def test_raw_payload_retained(spoonacular_search_payload):
    row = parse_recipe(spoonacular_search_payload["results"][0])
    assert row["raw_payload"]["id"] == 634476
