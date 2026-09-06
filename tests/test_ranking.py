"""Layer C — deterministic distance ranking, using precomputed clusters."""

import pytest

from model_service.ranking import (
    MEALS_PER_DAY,
    per_meal_target,
    rank_candidates,
)

DAILY = {"calories": 2100.0, "protein_g": 150.0, "carbs_g": 210.0, "fat_g": 70.0}
# per meal (÷3): 700 / 50 / 70 / 23.33
CENTROIDS = {
    0: {"calories": 700.0, "protein_g": 50.0, "carbs_g": 70.0, "fat_g": 23.0},  # ~ target
    1: {"calories": 300.0, "protein_g": 10.0, "carbs_g": 60.0, "fat_g": 5.0},   # small/lean
    2: {"calories": 900.0, "protein_g": 20.0, "carbs_g": 40.0, "fat_g": 60.0},  # fatty
}


def cand(mid, cal, p, c, f, cluster):
    return {"menu_id": mid, "name": f"Dish {mid}", "calories": cal,
            "protein_g": p, "carbs_g": c, "fat_g": f, "cluster_id": cluster}


def test_per_meal_target_divides_by_meals_per_day():
    mt = per_meal_target(DAILY)
    assert mt["calories"] == pytest.approx(2100.0 / MEALS_PER_DAY)
    assert mt["protein_g"] == pytest.approx(50.0)


def test_closest_to_target_ranks_first():
    cands = [
        cand("far", 1500, 5, 200, 90, 2),
        cand("close", 690, 49, 72, 24, 0),   # almost exactly the per-meal target
        cand("mid", 500, 30, 90, 15, 1),
    ]
    res = rank_candidates(cands, DAILY, CENTROIDS, top_n=3)
    assert [s.menu_id for s in res.ranked] == ["close", "mid", "far"]
    assert res.ranked[0].match_score > res.ranked[-1].match_score
    assert all(0.0 < s.match_score <= 1.0 for s in res.ranked)


def test_deterministic_and_ties_break_by_menu_id():
    # two identical-nutrition candidates -> same score -> menu_id order
    a = cand("b_item", 700, 50, 70, 23, 0)
    b = cand("a_item", 700, 50, 70, 23, 0)
    res1 = rank_candidates([a, b], DAILY, CENTROIDS)
    res2 = rank_candidates([b, a], DAILY, CENTROIDS)
    assert [s.menu_id for s in res1.ranked] == ["a_item", "b_item"]
    assert [s.menu_id for s in res2.ranked] == ["a_item", "b_item"]


def test_cluster_assignment_influences_score():
    # same item macros, different cluster_id -> different score (cluster term)
    good_cluster = cand("g", 650, 45, 68, 22, 0)      # cluster 0 ~ target
    bad_cluster = cand("h", 650, 45, 68, 22, 2)       # cluster 2 far from target
    res = rank_candidates([good_cluster, bad_cluster], DAILY, CENTROIDS)
    scores = {s.menu_id: s.score for s in res.ranked}
    assert scores["g"] < scores["h"]


def test_null_cluster_id_is_not_dropped_and_not_errored():
    cands = [
        cand("clustered", 700, 50, 70, 23, 0),
        cand("nocluster", 705, 49, 71, 23, None),   # no cluster_id
    ]
    res = rank_candidates(cands, DAILY, CENTROIDS, top_n=5)
    ids = {s.menu_id for s in res.ranked}
    assert ids == {"clustered", "nocluster"}         # both present
    assert res.null_cluster_count == 1
    nc = next(s for s in res.ranked if s.menu_id == "nocluster")
    assert nc.used_cluster_fallback is True
    assert nc.cluster_id is None


def test_null_cluster_penalty_puts_it_below_equal_clustered_item():
    # near-identical nutrition; the clustered one should win because the
    # null-cluster item carries the fixed penalty on its cluster term
    clustered = cand("c", 700, 50, 70, 23, 0)
    nocluster = cand("n", 700, 50, 70, 23, None)
    res = rank_candidates([clustered, nocluster], DAILY, CENTROIDS)
    assert [s.menu_id for s in res.ranked] == ["c", "n"]


def test_top_n_limits_results():
    cands = [cand(f"m{i}", 500 + i * 20, 30, 60, 20, i % 3) for i in range(20)]
    res = rank_candidates(cands, DAILY, CENTROIDS, top_n=5)
    assert len(res.ranked) == 5


def test_empty_candidates():
    res = rank_candidates([], DAILY, CENTROIDS)
    assert res.ranked == [] and res.null_cluster_count == 0


def test_missing_centroid_falls_back_like_null():
    # cluster_id present but no centroid for it (e.g. cluster emptied)
    res = rank_candidates([cand("x", 700, 50, 70, 23, 99)], DAILY, CENTROIDS)
    assert res.null_cluster_count == 1
    assert res.ranked[0].used_cluster_fallback is True
