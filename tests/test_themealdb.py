"""themealdb: meal parsing (empty/null ingredient slots, tags) and the client
(meals:null -> [], rate-limit pacing). No network."""

import json

import pytest

from food_pipeline.spoonacular import HttpResponse
from food_pipeline.themealdb import (
    TheMealDbClient,
    TheMealDbError,
    parse_meal,
)


def raw_meal(**overrides):
    m = {
        "idMeal": "52772",
        "strMeal": "Teriyaki Chicken Casserole",
        "strCategory": "Chicken",
        "strArea": "Japanese",
        "strInstructions": "Preheat oven to 350F...",
        "strTags": "Meat,Casserole",
        "strIngredient1": "soy sauce", "strMeasure1": "3/4 cup",
        "strIngredient2": "water", "strMeasure2": "1/2 cup",
        "strIngredient3": "brown sugar", "strMeasure3": "1/4 cup",
        "strIngredient4": "chicken breast", "strMeasure4": "500g",
        "strIngredient5": "", "strMeasure5": "",
        "strIngredient6": None, "strMeasure6": None,
    }
    for i in range(7, 21):
        m[f"strIngredient{i}"] = ""
        m[f"strMeasure{i}"] = ""
    m.update(overrides)
    return m


def test_parse_meal_basic_fields():
    p = parse_meal(raw_meal())
    assert p.meal_id == "52772"
    assert p.name == "Teriyaki Chicken Casserole"
    assert p.category == "Chicken"
    assert p.area == "Japanese"
    assert p.tags == ("meat", "casserole")


def test_parse_meal_ingredient_pairs_stop_at_empty_and_null():
    p = parse_meal(raw_meal())
    assert [(i.name, i.quantity_text) for i in p.ingredients] == [
        ("soy sauce", "3/4 cup"),
        ("water", "1/2 cup"),
        ("brown sugar", "1/4 cup"),
        ("chicken breast", "500g"),
    ]
    assert [i.slot for i in p.ingredients] == [1, 2, 3, 4]


def test_parse_meal_names_lowercased_measures_verbatim():
    p = parse_meal(raw_meal(strIngredient1="Soy Sauce", strMeasure1="  3/4 Cup "))
    assert p.ingredients[0].name == "soy sauce"
    assert p.ingredients[0].quantity_text == "3/4 Cup"  # stripped, not lowercased


def test_parse_meal_ingredient_with_blank_measure_kept():
    p = parse_meal(raw_meal(strMeasure4=""))  # ingredient present, measure blank
    chicken = [i for i in p.ingredients if i.name == "chicken breast"][0]
    assert chicken.quantity_text == ""  # kept; unit converter will reject it later


def test_parse_meal_no_tags():
    p = parse_meal(raw_meal(strTags=None))
    assert p.tags == ()


def test_client_meals_null_returns_empty_list():
    resp = HttpResponse(200, {}, json.dumps({"meals": None}))
    client = TheMealDbClient(transport=_Repeat(resp), sleep=lambda s: None)
    assert client.search("nonexistent dish") == []
    assert client.parsed_search("nonexistent dish") == []


def test_client_search_parses_meals():
    resp = HttpResponse(200, {}, json.dumps({"meals": [raw_meal()]}))
    client = TheMealDbClient(transport=_OneShot(resp), sleep=lambda s: None)
    meals = client.parsed_search("teriyaki")
    assert len(meals) == 1 and meals[0].meal_id == "52772"


def test_client_non_retryable_status_raises_immediately():
    resp = HttpResponse(400, {}, "bad request")
    client = TheMealDbClient(transport=_OneShot(resp), sleep=lambda s: None)
    with pytest.raises(TheMealDbError):
        client.search("x")


def test_client_retries_timeout_then_succeeds():
    ok = HttpResponse(200, {}, json.dumps({"meals": [raw_meal()]}))

    class _FailThenOk:
        def __init__(self):
            self.calls = 0

        def get(self, url, headers, timeout):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("The read operation timed out")
            return ok

    t = _FailThenOk()
    client = TheMealDbClient(transport=t, sleep=lambda s: None, max_retries=2)
    meals = client.search("teriyaki")
    assert t.calls == 2 and len(meals) == 1


def test_client_gives_up_after_retries():
    class _AlwaysDown:
        def __init__(self):
            self.calls = 0

        def get(self, url, headers, timeout):
            self.calls += 1
            raise TimeoutError("timed out")

    t = _AlwaysDown()
    client = TheMealDbClient(transport=t, sleep=lambda s: None, max_retries=2)
    with pytest.raises(TheMealDbError, match="after 3 attempts"):
        client.search("x")
    assert t.calls == 3


def test_client_paces_between_calls():
    sleeps = []
    clock = {"t": 0.0}
    resp = HttpResponse(200, {}, json.dumps({"meals": []}))
    client = TheMealDbClient(
        transport=_Repeat(resp),
        sleep=lambda s: sleeps.append(s),
        monotonic=lambda: clock["t"],
        min_request_interval_s=0.5,
    )
    client.search("a")
    client.search("b")  # no time elapsed on fake clock
    assert sleeps == [pytest.approx(0.5)]


class _OneShot:
    def __init__(self, resp):
        self._resp = resp
        self._used = False

    def get(self, url, headers, timeout):
        assert not self._used, "unexpected second HTTP call"
        self._used = True
        return self._resp


class _Repeat:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url, headers, timeout):
        return self._resp
