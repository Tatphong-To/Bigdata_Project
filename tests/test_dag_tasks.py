"""Task-level tests for the Phase 3 pipeline steps (no Airflow, no network,
no DB). Focus: deterministic order end-to-end + the minimum-catalog-size
gate (skip / provisional / stable)."""

import json
from pathlib import Path

import pytest

from food_pipeline import dag_tasks
from food_pipeline.clustering import KMeansConfig
from food_pipeline.config import ExtractConfig
from food_pipeline.quota import InMemoryQuotaStore, QuotaTracker
from food_pipeline.spoonacular import HttpResponse, SpoonacularClient
from food_pipeline.themealdb import IngredientLine, ParsedMeal
from food_pipeline.usda_client import UsdaFood

NOW = "2026-09-06T12:00:00+00:00"


# --------------------------------------------------------------------- fakes
class FakeTransport:
    def __init__(self, responses):
        self._responses = list(responses)

    def get(self, url, headers, timeout):
        return self._responses.pop(0)


def spoon_recipe(rid, cal=500, p=30, c=40, f=20):
    return {
        "id": rid, "title": f"Dish {rid}", "servings": 4,
        "diets": ["high protein"],
        "nutrition": {
            "nutrients": [
                {"name": "Calories", "amount": cal, "unit": "kcal"},
                {"name": "Protein", "amount": p, "unit": "g"},
                {"name": "Carbohydrates", "amount": c, "unit": "g"},
                {"name": "Fat", "amount": f, "unit": "g"},
            ],
            "ingredients": [{"id": 1, "name": "chicken"}, {"id": 2, "name": "rice"}],
        },
    }


def make_spoon_client(payloads):
    store = InMemoryQuotaStore()
    tracker = QuotaTracker(store, 50.0)
    cfg = ExtractConfig(api_key="SECRET", number_per_query=10)
    responses = [
        HttpResponse(200, {"X-API-Quota-Request": "1.6"}, json.dumps({"results": p}))
        for p in payloads
    ]
    return SpoonacularClient(cfg, tracker, transport=FakeTransport(responses),
                             sleep=lambda s: None, monotonic=lambda: 0.0)


class FakeMealDb:
    def __init__(self, meals_by_query):
        self._m = meals_by_query

    def parsed_search(self, q):
        return self._m.get(q, [])


def parsed_meal(mid, name, lines):
    return ParsedMeal(
        meal_id=mid, name=name, category="Misc", area="Test", instructions="...",
        tags=("dinner",),
        ingredients=tuple(IngredientLine(n, qty, i + 1) for i, (n, qty) in enumerate(lines)),
    )


CHK = UsdaFood(1, "Chicken breast", "Foundation", 120.0, 22.0, 0.0, 2.6)
RICE = UsdaFood(2, "Rice, cooked", "SR Legacy", 130.0, 2.7, 28.0, 0.3)
OIL = UsdaFood(3, "Olive oil", "SR Legacy", 884.0, 0.0, 0.0, 100.0)


def fake_usda(mapping):
    class _U:
        def search_foods(self, name):
            return mapping.get(name, [])
    return _U()


# --------------------------------------------------------------- extract_menus
def test_extract_menus_spoonacular_only(tmp_path):
    client = make_spoon_client([[spoon_recipe(1), spoon_recipe(2)]])
    out = dag_tasks.extract_menus(
        run_dir=tmp_path, include_spoonacular=True, include_themealdb_usda=False,
        spoonacular_queries=("chicken",), spoonacular_client=client,
        quota_store=InMemoryQuotaStore(),
    )
    rows = json.loads(Path(out).read_text())["rows"]
    assert len(rows) == 2
    assert all(r["nutrition_source"] == "spoonacular_computed" for r in rows)
    assert all(r["source"] == "spoonacular" for r in rows)


def test_extract_menus_themealdb_usda_path_and_completeness_drop(tmp_path):
    # good meal: all ingredients quantifiable + matchable
    good = parsed_meal("111", "Chicken Rice Bowl",
                       [("chicken breast", "300 g"), ("rice", "200 g"), ("olive oil", "1 tbsp")])
    # bad meal: only spice resolves -> low completeness -> dropped
    bad = parsed_meal("222", "Mystery Stew",
                      [("beef", "2"), ("carrots", "3 chopped"), ("rice", "50 g")])
    mealdb = FakeMealDb({"chicken": [good], "beef": [bad]})
    usda = fake_usda({
        "chicken breast": [CHK], "rice": [RICE], "olive oil": [OIL], "beef": [],
    })
    out = dag_tasks.extract_menus(
        run_dir=tmp_path, include_spoonacular=False, include_themealdb_usda=True,
        themealdb_queries=("chicken", "beef"), themealdb_recipes_per_query=1,
        themealdb_client=mealdb, usda_client=usda,
    )
    payload = json.loads(Path(out).read_text())
    ids = [r["menu_id"] for r in payload["rows"]]
    assert ids == ["themealdb-111"]  # 222 dropped for completeness
    assert payload["rows"][0]["nutrition_source"] == "usda_estimated"
    assert payload["counts"]["themealdb_dropped"] == 1


# ----------------------------------------------- validate / clean / ratios
def _write_extracted(tmp_path, rows):
    p = tmp_path / dag_tasks.FILE_EXTRACTED
    p.write_text(json.dumps({"rows": rows, "counts": {}}))
    return str(p)


def good_row(mid="m1", **kw):
    r = {
        "menu_id": mid, "source": "spoonacular", "nutrition_source": "spoonacular_computed",
        "name": "Dish", "servings": 4,
        "calories": 500.0, "protein_g": 30.0, "carbs_g": 40.0, "fat_g": 20.0,
        "ingredients": ["Chicken", "rice"], "diet_tags": ["High Protein"],
    }
    r.update(kw)
    return r


def test_validate_then_clean_then_ratios_chain(tmp_path):
    rows = [
        good_row("m1"),
        good_row("m2", calories=-5),          # rejected by validate
        good_row("m3", name="  "),            # rejected by validate (missing name)
        good_row("m1"),                       # duplicate -> clean dedupes
        good_row("m4", calories=0),           # passes validate? no: calories 0 -> validate rejects
        good_row("m5", carbs_g=0.0, fat_g=0.0, protein_g=0.0),  # valid but ratios -> 0s
    ]
    ex = _write_extracted(tmp_path, rows)
    v = dag_tasks.validate_nutrition_data(ex, run_dir=tmp_path)
    c = dag_tasks.clean(v, run_dir=tmp_path)
    r = dag_tasks.compute_nutrition_ratios(c, run_dir=tmp_path)

    validated_ids = [x["menu_id"] for x in json.loads(Path(v).read_text())["rows"]]
    assert "m2" not in validated_ids and "m3" not in validated_ids and "m4" not in validated_ids
    assert validated_ids.count("m1") == 2  # dupe still present after validate

    cleaned = json.loads(Path(c).read_text())["rows"]
    cleaned_ids = [x["menu_id"] for x in cleaned]
    assert cleaned_ids.count("m1") == 1           # clean dedupes
    assert set(cleaned_ids) == {"m1", "m5"}
    m1 = next(x for x in cleaned if x["menu_id"] == "m1")
    assert m1["ingredients"] == ["chicken", "rice"]   # lowercased + sorted
    assert m1["diet_tags"] == ["high protein"]

    ratios = json.loads(Path(r).read_text())["rows"]
    assert all(set(dag_tasks.FEATURE_COLUMNS).issubset(x) for x in ratios)
    m1r = next(x for x in ratios if x["menu_id"] == "m1")
    assert m1r["pct_calories_from_protein"] == pytest.approx(30 * 4 / 500)


# ------------------------------------------- minimum-catalog-size gate
def _ratios_file(tmp_path, n, start=0):
    rows = []
    for i in range(n):
        rows.append({
            **good_row(f"new{start + i}"),
            "pct_calories_from_protein": 0.25 + (i % 3) * 0.03,
            "pct_calories_from_carbs": 0.45 - (i % 3) * 0.02,
            "pct_calories_from_fat": 0.30 - (i % 3) * 0.01,
            "calories_per_serving": 400.0 + (i % 5) * 25,
        })
    p = tmp_path / dag_tasks.FILE_RATIOS
    p.write_text(json.dumps({"rows": rows, "dropped": []}))
    return str(p)


def _existing(n):
    def _f():
        return [
            {
                "menu_id": f"old{i}",
                "pct_calories_from_protein": 0.2 + (i % 4) * 0.04,
                "pct_calories_from_carbs": 0.5 - (i % 4) * 0.03,
                "pct_calories_from_fat": 0.3 - (i % 4) * 0.01,
                "calories_per_serving": 350.0 + (i % 6) * 30,
            }
            for i in range(n)
        ]
    return _f


def test_gate_skips_below_150(tmp_path):
    rp = _ratios_file(tmp_path, 20)
    with pytest.raises(dag_tasks.PipelineSkip) as ei:
        dag_tasks.train_or_update_kmeans(
            rp, run_dir=tmp_path, now_iso=NOW, fetch_existing=_existing(50)
        )
    assert "minimum 150" in str(ei.value)
    skip = json.loads(Path(tmp_path / dag_tasks.FILE_SKIP).read_text())
    assert skip["gate"] == "skip" and skip["row_count"] == 70


def test_gate_trains_provisional_150_to_499(tmp_path):
    rp = _ratios_file(tmp_path, 100)
    out = dag_tasks.train_or_update_kmeans(
        rp, run_dir=tmp_path, now_iso=NOW, fetch_existing=_existing(100)
    )
    d = json.loads(Path(out).read_text())
    assert d["gate"] == "provisional"
    assert d["row_count"] == 200
    assert d["provisional"] is True
    assert d["model_version"].endswith("-provisional")
    assert Path(d["model_path"]).exists()
    assert d["tracking"]["backend"] == "fallback"  # no MLflow URI in tests


def test_gate_trains_stable_500_plus(tmp_path):
    rp = _ratios_file(tmp_path, 300)
    out = dag_tasks.train_or_update_kmeans(
        rp, run_dir=tmp_path, now_iso=NOW, fetch_existing=_existing(400)
    )
    d = json.loads(Path(out).read_text())
    assert d["gate"] == "stable"
    assert d["row_count"] == 700
    assert d["provisional"] is False
    assert not d["model_version"].endswith("-provisional")


def test_gate_dedupes_overlapping_menu_ids(tmp_path):
    # new rows reuse old ids -> combined count must not double-count
    rp = _ratios_file(tmp_path, 80, start=0)  # ids new0..new79
    exist = lambda: [  # noqa: E731
        {"menu_id": f"new{i}", "pct_calories_from_protein": 0.25,
         "pct_calories_from_carbs": 0.45, "pct_calories_from_fat": 0.30,
         "calories_per_serving": 420.0}
        for i in range(80)
    ]
    with pytest.raises(dag_tasks.PipelineSkip) as ei:
        dag_tasks.train_or_update_kmeans(rp, run_dir=tmp_path, now_iso=NOW, fetch_existing=exist)
    assert "80 rows" in str(ei.value)  # 80 unique, not 160


# ------------------------------------------- assign + write
def test_assign_then_write_attaches_cluster(tmp_path):
    rp = _ratios_file(tmp_path, 160)
    model_out = dag_tasks.train_or_update_kmeans(
        rp, run_dir=tmp_path, now_iso=NOW, fetch_existing=lambda: []
    )
    assign_out = dag_tasks.assign_cluster_labels(rp, model_out, run_dir=tmp_path)
    a = json.loads(Path(assign_out).read_text())
    assert len(a["assignments"]) == 160
    assert a["provisional"] is True

    captured = {}
    def fake_upsert(rows):
        captured["rows"] = list(rows)
        return len(captured["rows"])

    w = dag_tasks.write_to_menu_catalog(rp, run_dir=tmp_path, upsert=fake_upsert)
    summary = json.loads(Path(w).read_text())
    assert summary["rows_written"] == 160
    assert summary["rows_with_cluster"] == 160
    assert summary["model_provisional"] is True
    assert all(r["cluster_id"] is not None for r in captured["rows"])
    assert all(r["model_version"].endswith("-provisional") for r in captured["rows"])


def test_write_runs_without_clustering_when_gate_skipped(tmp_path):
    # simulate the skip path: ratios exist, no model/assignments file
    rp = _ratios_file(tmp_path, 30)
    captured = {}

    def fake_upsert(rows):
        captured["rows"] = list(rows)
        return len(captured["rows"])

    w = dag_tasks.write_to_menu_catalog(rp, run_dir=tmp_path, upsert=fake_upsert)
    summary = json.loads(Path(w).read_text())
    assert summary["rows_written"] == 30
    assert summary["rows_with_cluster"] == 0
    assert summary["clustering_ran"] is False
    assert all(r["cluster_id"] is None for r in captured["rows"])
    assert all(r["model_version"] is None for r in captured["rows"])


# ------------------------------------------- full deterministic chain
def test_full_pipeline_order_skip_path(tmp_path):
    """extract -> validate -> clean -> ratios -> (train skips) -> write."""
    rows = [good_row(f"m{i}", calories=400 + i * 5) for i in range(12)]
    ex = _write_extracted(tmp_path, rows)
    v = dag_tasks.validate_nutrition_data(ex, run_dir=tmp_path)
    c = dag_tasks.clean(v, run_dir=tmp_path)
    r = dag_tasks.compute_nutrition_ratios(c, run_dir=tmp_path)
    with pytest.raises(dag_tasks.PipelineSkip):
        dag_tasks.train_or_update_kmeans(r, run_dir=tmp_path, now_iso=NOW, fetch_existing=lambda: [])

    def fake_upsert(rows):
        return len(list(rows))

    w = dag_tasks.write_to_menu_catalog(r, run_dir=tmp_path, upsert=fake_upsert)
    summary = json.loads(Path(w).read_text())
    assert summary["rows_written"] == 12
    assert summary["clustering_ran"] is False
    # every numbered artifact was produced in order
    for fname in (dag_tasks.FILE_EXTRACTED, dag_tasks.FILE_VALIDATED,
                  dag_tasks.FILE_CLEANED, dag_tasks.FILE_RATIOS,
                  dag_tasks.FILE_SKIP, dag_tasks.FILE_WRITE_SUMMARY):
        assert (tmp_path / fname).exists(), fname
