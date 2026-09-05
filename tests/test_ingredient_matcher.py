"""ingredient_matcher: clear match accepted, ambiguous accepted-but-logged,
no match rejected-and-logged, threshold is config."""

import pytest

from food_pipeline.ingredient_matcher import (
    MatchConfig,
    match_ingredient,
    score_names,
)
from food_pipeline.usda_client import UsdaFood


def food(fdc_id, description, *, macros=True):
    kw = dict(
        calories_per_100g=100.0,
        protein_g_per_100g=5.0,
        carbs_g_per_100g=10.0,
        fat_g_per_100g=2.0,
    )
    if not macros:
        kw["carbs_g_per_100g"] = None
    return UsdaFood(fdc_id=fdc_id, description=description, data_type="Foundation", **kw)


# --- scoring ---------------------------------------------------------
def test_score_exact_after_noise_removal_is_top():
    assert score_names("garlic", "Garlic, raw") >= 0.95
    assert score_names("chicken breast", "Chicken, breast, raw") >= 0.9


def test_score_unrelated_is_low():
    assert score_names("garlic", "Cola, carbonated") < 0.3


def test_score_partial_is_mid_band():
    s = score_names("cream", "Sour cream")
    assert 0.5 < s < 0.8


# --- matching ------------------------------------------------------
def test_clear_match_accepted_not_flagged(caplog):
    cands = [food(1, "Cola"), food(2, "Garlic, raw"), food(3, "Onion, raw")]
    with caplog.at_level("INFO"):
        m = match_ingredient("garlic", cands)
    assert m.accepted and m.food.fdc_id == 2
    assert m.confidence >= 0.95
    assert "LOW-CONFIDENCE" not in caplog.text


def test_ambiguous_match_accepted_but_logged(caplog):
    cfg = MatchConfig(min_confidence=0.55, log_accepted_below=0.9)
    cands = [food(10, "Sour cream"), food(11, "Cola")]
    with caplog.at_level("INFO"):
        m = match_ingredient("cream", cands, config=cfg)
    assert m.accepted and m.food.fdc_id == 10
    assert m.confidence < 0.9
    assert "LOW-CONFIDENCE" in caplog.text
    assert "cream" in caplog.text


def test_no_match_rejected_and_logged(caplog):
    cands = [food(1, "Cola, carbonated"), food(2, "Wheat flour, white")]
    with caplog.at_level("WARNING"):
        m = match_ingredient("fresh quail eggs", cands)
    assert not m.accepted
    assert m.food is None
    assert m.reason and "threshold" in m.reason
    assert m.candidate_description in {"Cola, carbonated", "Wheat flour, white"}
    assert "REJECTED" in caplog.text and "fresh quail eggs" in caplog.text


def test_threshold_is_config_not_hardcoded():
    cands = [food(1, "Sour cream")]
    assert match_ingredient("cream", cands, config=MatchConfig(min_confidence=0.5)).accepted
    assert not match_ingredient("cream", cands, config=MatchConfig(min_confidence=0.95)).accepted


def test_candidates_without_all_macros_filtered_out(caplog):
    cands = [food(1, "Garlic, raw", macros=False)]  # perfect name, missing carbs
    with caplog.at_level("WARNING"):
        m = match_ingredient("garlic", cands)
    assert not m.accepted
    assert "complete macros" in m.reason


def test_require_all_macros_can_be_disabled():
    cands = [food(1, "Garlic, raw", macros=False)]
    m = match_ingredient("garlic", cands, config=MatchConfig(require_all_macros=False))
    assert m.accepted and m.food.fdc_id == 1


def test_empty_candidate_list_rejected(caplog):
    with caplog.at_level("WARNING"):
        m = match_ingredient("garlic", [])
    assert not m.accepted and m.confidence == 0.0
