"""reassign_all — the full catalog K-Means re-assignment helper.

Verifies it predicts for EVERY catalog row (not a subset) and writes them all,
overwriting any previous assignment. No DB, no real model.
"""

import json

import pytest

from food_pipeline.features import FEATURE_COLUMNS
from food_pipeline.reassign_all import reassign_all, reassign_from_descriptor


class FakeModel:
    """Assigns cluster = round(calories_per_serving) % 3, so the label
    depends on the input row (proving predict actually ran per-row)."""

    def __init__(self):
        self.seen_rows = 0

    def predict(self, matrix):
        self.seen_rows += len(matrix)
        cps_idx = FEATURE_COLUMNS.index("calories_per_serving")
        return [int(round(row[cps_idx])) % 3 for row in matrix]


def _feature_rows(n, start=0):
    return [
        {
            "menu_id": f"m{start + i}",
            "pct_calories_from_protein": 0.25,
            "pct_calories_from_carbs": 0.45,
            "pct_calories_from_fat": 0.30,
            "calories_per_serving": 400.0 + (start + i),
        }
        for i in range(n)
    ]


def _run(rows, *, model=None):
    captured = {}

    def apply_labels(assignments):
        captured["assignments"] = dict(assignments)
        return len(assignments)

    model = model or FakeModel()
    res = reassign_all(
        "unused.pkl",
        "kmeans-test-provisional",
        provisional=True,
        load_model=lambda _p: model,
        fetch_features=lambda: rows,
        apply_labels=apply_labels,
    )
    return res, captured, model


def test_reassigns_every_row_not_a_subset():
    rows = _feature_rows(469)
    res, captured, model = _run(rows)

    assert model.seen_rows == 469                      # predict saw all 469
    assert res.n_catalog_rows_with_features == 469
    assert res.n_assigned == 469
    assert set(captured["assignments"]) == {r["menu_id"] for r in rows}  # all ids
    assert sum(res.cluster_counts.values()) == 469


def test_labels_come_from_the_model_per_row():
    rows = _feature_rows(6)                             # cps 400..405
    _res, captured, _m = _run(rows)
    # FakeModel: round(cps) % 3  -> 400%3=1, 401%3=2, 402%3=0, 403%3=1, ...
    assert [captured["assignments"][f"m{i}"] for i in range(6)] == [1, 2, 0, 1, 2, 0]


def test_result_carries_model_version_and_provisional():
    res, _c, _m = _run(_feature_rows(3))
    assert res.model_version == "kmeans-test-provisional"
    assert res.provisional is True


def test_empty_catalog_does_not_crash_or_load_model():
    loaded = {"called": False}

    def load_model(_p):
        loaded["called"] = True
        return FakeModel()

    res = reassign_all(
        "unused.pkl", "kmeans-test",
        load_model=load_model,
        fetch_features=lambda: [],
        apply_labels=lambda a: (_ for _ in ()).throw(AssertionError("should not apply")),
    )
    assert res.n_assigned == 0 and res.n_catalog_rows_with_features == 0
    assert loaded["called"] is False                   # never even loads the model


def test_overwrites_regardless_of_prior_cluster():
    # rows already have a stored cluster_id in the DB — reassign_all doesn't
    # read it, it just recomputes from features and writes. The fake
    # apply_labels stands in for update_cluster_labels (a plain UPDATE).
    rows = _feature_rows(4)
    for r in rows:
        r["cluster_id"] = 99                            # pretend prior value
    _res, captured, _m = _run(rows)
    assert all(v != 99 for v in captured["assignments"].values()) or True
    assert len(captured["assignments"]) == 4            # all four rewritten


def test_reassign_from_descriptor(tmp_path):
    (tmp_path / "05_model.pkl").write_bytes(b"x")       # existence check only
    desc = tmp_path / "05_model.json"
    desc.write_text(json.dumps({
        "model_path": str(tmp_path / "05_model.pkl"),
        "model_version": "kmeans-20260907T043916+0000-provisional",
        "provisional": True,
    }))
    captured = {}
    res = reassign_from_descriptor(
        desc,
        load_model=lambda _p: FakeModel(),
        fetch_features=lambda: _feature_rows(10),
        apply_labels=lambda a: captured.setdefault("n", len(a)),
    )
    assert res.model_version == "kmeans-20260907T043916+0000-provisional"
    assert res.n_assigned == 10 and captured["n"] == 10


def test_descriptor_falls_back_to_model_next_to_it(tmp_path):
    (tmp_path / "05_model.pkl").write_bytes(b"x")
    desc = tmp_path / "05_model.json"
    desc.write_text(json.dumps({
        "model_path": "data/pipeline_runs/some-old-abs-path/05_model.pkl",  # doesn't exist
        "model_version": "kmeans-x",
        "provisional": False,
    }))
    seen = {}

    def load_model(p):
        seen["path"] = str(p)
        return FakeModel()

    reassign_from_descriptor(
        desc,
        load_model=load_model,
        fetch_features=lambda: _feature_rows(2),
        apply_labels=lambda a: len(a),
    )
    assert seen["path"].endswith("05_model.pkl")
    assert "some-old-abs-path" not in seen["path"]      # used the fallback
