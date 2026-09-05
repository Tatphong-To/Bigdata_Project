"""run_extraction orchestration: batching, early-stop on the persisted budget,
raw + staging artifacts, parse/validate wiring. Real client, fake transport."""

import datetime as dt
import json

import pytest

from food_pipeline.config import ExtractConfig
from food_pipeline.extract import run_extraction
from food_pipeline.quota import InMemoryQuotaStore, QuotaTracker
from food_pipeline.spoonacular import HttpResponse, SpoonacularClient

DAY = dt.date(2026, 9, 5)
FIXED_NOW = dt.datetime(2026, 9, 5, 12, 0, 0, tzinfo=dt.timezone.utc)


def recipe(rid, *, cal=478.31, protein=37.1, carbs=15.21, fat=29.24, title=None):
    return {
        "id": rid,
        "title": title or f"Recipe {rid}",
        "servings": 4,
        "diets": ["dairy free"],
        "nutrition": {
            "nutrients": [
                {"name": "Calories", "amount": cal, "unit": "kcal"},
                {"name": "Protein", "amount": protein, "unit": "g"},
                {"name": "Carbohydrates", "amount": carbs, "unit": "g"},
                {"name": "Fat", "amount": fat, "unit": "g"},
            ],
            "ingredients": [{"id": 1, "name": "chicken"}],
        },
    }


def resp(recipes, quota_request="2.20"):
    return HttpResponse(
        status=200,
        headers={"X-API-Quota-Request": quota_request},
        body=json.dumps({"results": recipes}),
    )


class FakeTransport:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, headers, timeout):
        self.calls += 1
        return self._responses.pop(0)


def build_client(responses, *, quota=50.0, used=0.0):
    store = InMemoryQuotaStore()
    if used:
        store.add_usage(DAY, used, 0)
    tracker = QuotaTracker(store, quota, today=lambda: DAY)
    cfg = ExtractConfig(api_key="SECRET", number_per_query=20)
    client = SpoonacularClient(
        cfg, tracker, transport=FakeTransport(responses), sleep=lambda s: None,
        monotonic=lambda: 0.0,
    )
    return client, tracker


def test_happy_path_writes_artifacts_and_splits_rows(tmp_path):
    client, tracker = build_client(
        [resp([recipe(1), recipe(2)]), resp([recipe(3), recipe(4, cal=-1)])]
    )
    run = run_extraction(
        client, queries=["chicken", "beef"], out_dir=tmp_path, now=lambda: FIXED_NOW
    )

    assert run.queries_completed == ["chicken", "beef"]
    assert run.stopped_early is False
    assert run.n_accepted == 3  # recipe 4 has negative calories
    assert run.n_rejected == 1
    assert run.rejected[0].menu_id == "4"

    assert run.raw_path.exists() and run.staging_path.exists()
    staging = json.loads(run.staging_path.read_text("utf-8"))
    assert {r["menu_id"] for r in staging["accepted"]} == {"1", "2", "3"}
    assert staging["rejected"][0]["menu_id"] == "4"
    raw = json.loads(run.raw_path.read_text("utf-8"))
    assert raw["queries"] == ["chicken", "beef"]
    assert len(raw["payloads"]) == 2

    # two calls at 2.20 each
    assert tracker.points_used() == pytest.approx(4.40)


def test_stops_early_when_budget_cannot_cover_next_call(tmp_path, caplog):
    # remaining 3.0 -> first call (est 2.2) ok, charged 2.2 -> remaining 0.8
    # -> second call refused before any HTTP
    client, tracker = build_client([resp([recipe(1)])], quota=3.0)
    with caplog.at_level("WARNING"):
        run = run_extraction(
            client, queries=["a", "b", "c"], out_dir=tmp_path, now=lambda: FIXED_NOW
        )
    assert run.stopped_early is True
    assert run.queries_completed == ["a"]
    assert run.n_accepted == 1
    assert client._transport.calls == 1
    assert "stopping early" in caplog.text
    # artifacts still written for the partial run
    assert run.raw_path.exists() and run.staging_path.exists()


def test_dedupes_recipes_seen_in_multiple_queries(tmp_path):
    client, _ = build_client(
        [resp([recipe(1), recipe(2)]), resp([recipe(2), recipe(3)])]
    )
    run = run_extraction(
        client, queries=["a", "b"], out_dir=tmp_path, now=lambda: FIXED_NOW
    )
    assert sorted(r["menu_id"] for r in run.accepted) == ["1", "2", "3"]


def test_no_queries_completed_when_budget_already_exhausted(tmp_path):
    client, _ = build_client([], quota=50.0, used=50.0)
    run = run_extraction(
        client, queries=["a"], out_dir=tmp_path, now=lambda: FIXED_NOW
    )
    assert run.stopped_early is True
    assert run.queries_completed == []
    assert run.n_accepted == 0
