"""POST /recommend — contract shape, the always-on safety filter,
prediction_log write, and the no-cluster edge case. Fake catalog, no DB."""

import pytest
from fastapi.testclient import TestClient

from model_service.catalog import Candidate
from model_service.main import app, get_catalog
from model_service.schemas import MEDICAL_DISCLAIMER


def _cand(mid, name, cal, p, c, f, ingredients=(), tags=(), cluster=0):
    return Candidate(mid, name, cal, p, c, f, tuple(ingredients), tuple(tags), cluster)


DEFAULT_CATALOG = [
    _cand("1", "Grilled Chicken Bowl", 620, 55, 45, 20, ("chicken", "rice", "broccoli"), (), 0),
    _cand("2", "Peanut Satay Skewers", 700, 40, 30, 40, ("chicken", "peanuts", "soy sauce"), (), 1),
    _cand("3", "Salmon Salad", 480, 38, 12, 30, ("salmon", "greens", "olive oil"), (), 2),
    _cand("4", "Veggie Quinoa Plate", 550, 20, 80, 15, ("quinoa", "chickpeas", "spinach"), ("vegan",), 0),
    _cand("5", "Cheese Ravioli", 780, 25, 90, 34, ("wheat flour", "ricotta", "parmesan"), ("vegetarian",), 1),
    _cand("6", "Unclustered Stew", 600, 35, 55, 22, ("beef", "carrot", "potato"), (), None),
]


class FakeCatalog:
    def __init__(self, candidates=None, model=("kmeans-20260906T090153+0000-provisional", True)):
        self._candidates = list(candidates if candidates is not None else DEFAULT_CATALOG)
        self._model = model
        self.logged: list[dict] = []

    def candidates(self):
        return self._candidates

    def cluster_centroids(self):
        # crude per-cluster means over the fake catalog
        by: dict[int, list[Candidate]] = {}
        for c in self._candidates:
            if c.cluster_id is not None:
                by.setdefault(c.cluster_id, []).append(c)
        return {
            cid: {
                "calories": sum(x.calories for x in xs) / len(xs),
                "protein_g": sum(x.protein_g for x in xs) / len(xs),
                "carbs_g": sum(x.carbs_g for x in xs) / len(xs),
                "fat_g": sum(x.fat_g for x in xs) / len(xs),
            }
            for cid, xs in by.items()
        }

    def model_version(self):
        return self._model

    def log_prediction(self, record):
        self.logged.append(record)


@pytest.fixture
def catalog():
    fake = FakeCatalog()
    app.dependency_overrides[get_catalog] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


client = TestClient(app)

PROFILE = {
    "age": 30, "sex": "male", "weight_kg": 80, "height_cm": 180,
    "activity_level": "moderate", "goal": "maintain",
}


def test_contract_shape(catalog):
    r = client.post("/recommend", json={"profile": PROFILE, "restrictions": {}})
    assert r.status_code == 200
    body = r.json()

    assert set(body) >= {"daily_target", "recommendations", "excluded_count",
                         "model_version", "disclaimer"}
    dt = body["daily_target"]
    assert set(dt) == {"calories", "protein_g", "carbs_g", "fat_g"}
    assert isinstance(dt["calories"], int)
    assert body["daily_target"]["calories"] == 2759  # hand-checked in test_calculator

    assert isinstance(body["recommendations"], list) and body["recommendations"]
    for rec in body["recommendations"]:
        assert set(rec) == {"menu_id", "name", "match_score", "nutrition"}
        assert isinstance(rec["menu_id"], str)
        assert 0.0 < rec["match_score"] <= 1.0
        assert set(rec["nutrition"]) == {"calories", "protein_g", "carbs_g", "fat_g"}

    assert isinstance(body["excluded_count"], int)
    assert body["model_version"].endswith("-provisional")  # catalog is in the provisional band
    assert body["disclaimer"] == MEDICAL_DISCLAIMER


def test_safety_filter_runs_with_empty_restrictions(catalog):
    r = client.post("/recommend", json={"profile": PROFILE, "restrictions": {"allergies": [], "diet_type": None}})
    body = r.json()
    assert body["excluded_count"] == 0                 # nothing excluded ...
    assert len(body["recommendations"]) >= 1           # ... but filter ran, results present
    # and it was logged
    assert catalog.logged and catalog.logged[-1]["excluded_count"] == 0


def test_safety_filter_excludes_on_allergy(catalog):
    r = client.post("/recommend", json={
        "profile": PROFILE,
        "restrictions": {"allergies": ["peanut"], "diet_type": None},
    })
    body = r.json()
    assert body["excluded_count"] >= 1                 # "Peanut Satay Skewers" out
    rec_ids = {x["menu_id"] for x in body["recommendations"]}
    assert "2" not in rec_ids


def test_diet_restriction_excludes(catalog):
    r = client.post("/recommend", json={
        "profile": PROFILE, "restrictions": {"allergies": [], "diet_type": "vegan"},
    })
    body = r.json()
    # only the vegan quinoa plate (id 4) survives a vegan filter of this catalog
    rec_ids = {x["menu_id"] for x in body["recommendations"]}
    assert rec_ids == {"4"}
    assert body["excluded_count"] == 5


def test_combined_allergy_and_diet(catalog):
    r = client.post("/recommend", json={
        "profile": PROFILE,
        "restrictions": {"allergies": ["shellfish", "peanut"], "diet_type": "vegetarian"},
    })
    body = r.json()
    assert body["excluded_count"] >= 1
    assert isinstance(body["recommendations"], list)


def test_prediction_log_has_profile_no_identity(catalog):
    client.post("/recommend", json={
        "profile": PROFILE,
        "restrictions": {"allergies": ["dairy"], "diet_type": None},
    })
    rec = catalog.logged[-1]
    assert rec["age"] == 30 and rec["sex"] == "male" and rec["goal"] == "maintain"
    assert rec["target_calories"] == 2759
    assert rec["allergies"] == ["dairy"]
    assert "recommended_menu_ids" in rec and rec["excluded_count"] >= 1
    # no identity fields
    assert not ({"name", "email", "user_id", "ip", "account"} & set(rec))


def test_unclustered_candidate_still_recommendable(catalog):
    # only the unclustered stew + one clustered item
    fake = FakeCatalog(candidates=[DEFAULT_CATALOG[0], DEFAULT_CATALOG[5]])
    app.dependency_overrides[get_catalog] = lambda: fake
    r = client.post("/recommend", json={"profile": PROFILE, "restrictions": {}})
    app.dependency_overrides.clear()
    body = r.json()
    ids = {x["menu_id"] for x in body["recommendations"]}
    assert "6" in ids                                  # the unclustered row is not dropped
    assert r.status_code == 200


def test_model_version_no_model(catalog):
    fake = FakeCatalog(candidates=[
        Candidate("9", "Orphan", 500, 30, 50, 20, (), (), None)
    ], model=("no-model", False))
    app.dependency_overrides[get_catalog] = lambda: fake
    r = client.post("/recommend", json={"profile": PROFILE, "restrictions": {}})
    app.dependency_overrides.clear()
    assert r.json()["model_version"] == "no-model"


def test_invalid_profile_rejected(catalog):
    bad = {**PROFILE, "goal": "bulk"}
    r = client.post("/recommend", json={"profile": bad, "restrictions": {}})
    assert r.status_code == 422


def test_health():
    assert client.get("/health").json() == {"status": "ok"}
